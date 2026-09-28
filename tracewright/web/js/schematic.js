// The live schematic: KiCad's own plot of each sheet (SVG), with clickable symbols over it, find,
// and the review flags.
import { h, clear, api, toast, menu } from "./util.js";
import { icon } from "./icons.js";
import { FlagLayer, FlagTool, flagEditor } from "./review.js";

export const PAPER = { A5: [210, 148], A4: [297, 210], A3: [420, 297], A2: [594, 420], A1: [841, 594], A0: [1189, 841],
  A: [279.4, 215.9], B: [431.8, 279.4], C: [558.8, 431.8], D: [863.6, 558.8], E: [1117.6, 863.6],
  USLetter: [279.4, 215.9], USLegal: [355.6, 215.9], USLedger: [431.8, 279.4] };
const NS = "http://www.w3.org/2000/svg";

export class SchematicView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.sheets = []; this.cur = null; this.vb = null; this.sel = new Set(); this.hl = null;
    this.build();
    this.load();
  }

  build() {
    this.tabs = h("div.hudbox.sheet-tabs");
    this.svg = document.createElementNS(NS, "svg");
    this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    this.svg.setAttribute("class", "sheet");
    Object.assign(this.svg.style, { position: "absolute", inset: 0, width: "100%", height: "100%", cursor: "grab" });
    this.img = document.createElementNS(NS, "image");
    this.overlay = document.createElementNS(NS, "g");
    this.paper = document.createElementNS(NS, "rect");
    this.paper.setAttribute("fill", "#fbfaf6");
    this.shadow = document.createElementNS(NS, "rect");
    this.shadow.setAttribute("fill", "rgba(0,0,0,.18)");
    this.svg.append(this.shadow, this.paper, this.img, this.overlay);
    this.tip = h("div.vtip", { style: { display: "none" } });
    this.info = h("div.coords", "");
    this.selBar = h("div.selbar", { style: { display: "none" } });
    this.loading = h("div.vbanner", { style: { display: "none" } });
    this.findIn = h("input", { placeholder: "Find a symbol", spellcheck: false });
    this.findRes = h("div.findres", { style: { display: "none" } });
    this.flagBtn = h("button.tbtn", { onclick: () => this.flags.toggle(), "data-tip": "Flag an issue", "data-kbd": "c" }, icon("flag", 15));
    this.hudTc = h("div.hud.tc");
    this.el.appendChild(h("div.viewer.paper", this.svg,
      h("div.hud.tl", this.tabs),
      h("div.hud.tr", h("div.hudbox", h("div.findbox", icon("search", 13), this.findIn, this.findRes), h("div.tsep"), this.flagBtn, h("div.tsep"),
        h("button.tbtn", { "data-tip": "Zoom out", onclick: () => this.zoom(1.4) }, icon("zoom-out", 15)),
        h("button.tbtn", { "data-tip": "Zoom in", onclick: () => this.zoom(1 / 1.4) }, icon("zoom-in", 15)),
        h("button.tbtn", { "data-tip": "Fit the sheet", "data-kbd": "f", onclick: () => this.fit() }, icon("scan", 15)))),
      this.hudTc, h("div.hud.bl", h("div.hudbox", this.info)), h("div.hud.br", this.selBar), this.tip, this.loading));
    this.viewer = this.el.firstChild;
    this.wireFind();
    this.flagLayer = new FlagLayer(this.viewer, this.ws.review, {
      view: "schematic", sheet: () => this.cur, project: (x, y) => this.toScreen(x, y),
      onPin: (f, pin) => { this.flagLayer.mark(f.id); flagEditor(pin, this.ws, { flag: f, onDone: () => this.flagLayer.mark(null) }); } });
    this.flags = new FlagTool(this.ws, {
      view: "schematic", surface: this.svg, layer: this.flagLayer, hud: this.hudTc,
      toWorld: (px, py) => this.vb ? this.toPage(px, py) : null,
      context: (w) => this.context(w), snapshot: (w) => this.snapshot(w),
      onChange: (on) => { this.viewer.classList.toggle("tool-flag", on); this.flagBtn.classList.toggle("on", on); this.svg.style.cursor = on ? "crosshair" : "grab"; } });
    this.mouse();
    this.keys = (e) => {
      if (!this.el.classList.contains("on") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (document.querySelector(".modal-bg, .popover, .palette-bg")) return;
      const k = e.key.toLowerCase();
      if (k === "f") this.fit();
      else if (k === "c") this.flags.toggle();
      else if (e.key === "Escape") { if (this.flags.active) this.flags.toggle(false); else this.clearSel(); }
      else return;
      e.preventDefault();
    };
    document.addEventListener("keydown", this.keys);
  }

  destroy() { document.removeEventListener("keydown", this.keys); this.flagLayer.destroy(); }
  shown() {
    if (this.stale) this.load(); else if (this.vb) this.applyVB();
    if (this.pendingSheet && this.sheets.length) { const np = this.pendingSheet; this.pendingSheet = null; if (np !== this.cur) this.showSheet(np); }
    if (this.pendingZoom && this.sheets.length) { this.pendingZoom = false; requestAnimationFrame(() => this.zoomSel()); }
  }

  // parts picked in another view (board, 3D, BOM): select them here too, on their sheet
  probe(refs, source, fly) {
    if (!this.sheets.length) { this.pendingProbe = refs; return; }
    this.sel = new Set(refs || []);
    const visible = fly || this.el.classList.contains("on");
    const target = this.sel.size ? this.sheets.find((s) => s.symbols.some((y) => this.sel.has(y.ref))) : null;
    if (target && target.name_path !== this.cur) { if (visible) this.showSheet(target.name_path); else this.pendingSheet = target.name_path; }
    this.drawOverlay();
    this.renderSel(true);
    if (!this.sel.size) return;
    if (visible) requestAnimationFrame(() => this.zoomSel()); else this.pendingZoom = true;
  }

  zoomSel() {
    const s = this.sheet();
    const syms = s ? s.symbols.filter((y) => this.sel.has(y.ref)) : [];
    if (!syms.length) return;
    const x0 = Math.min(...syms.map((y) => y.bbox[0])), y0 = Math.min(...syms.map((y) => y.bbox[1]));
    const x1 = Math.max(...syms.map((y) => y.bbox[2])), y1 = Math.max(...syms.map((y) => y.bbox[3]));
    const m = 25;
    this.setVB(x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m, true);
  }
  reload() { if (!this.el.classList.contains("on")) { this.stale = true; return; } this.load(true); }
  focusFind() { this.findIn.focus(); this.findIn.select(); }

  async load(keepView) {
    let d;
    try { d = await api(`/api/projects/${encodeURIComponent(this.pid)}/schematic`); } catch (e) { toast("Schematic: " + e.message, "error"); return; }
    this.stale = false;
    if (d.empty) {
      clear(this.loading); this.loading.style.display = "flex";
      this.loading.appendChild(h("div", h("div.eicon", icon("waypoints", 22)), h("h3", "No schematic yet"),
        h("p", "Ask Claude to draw it, or draw it in KiCad."),
        h("button.btn.primary", { onclick: () => this.ws.ask("Let's capture the schematic. Start with the power sheet and show me each block as you add it.") }, "Start the schematic")));
      return;
    }
    this.loading.style.display = "none";
    this.version = d.version;
    this.sheets = d.sheets;
    if (!this.cur || !this.sheets.find((s) => s.name_path === this.cur)) this.cur = this.sheets[0].name_path;
    this.renderTabs();
    this.showSheet(this.cur, keepView);
    if (this.pendingProbe) { const r = this.pendingProbe; this.pendingProbe = null; this.probe(r, "pending"); }
  }

  // One tab per sheet while they fit on a line; otherwise the current sheet with a menu and arrows,
  // so the tabs never cover the drawing.
  renderTabs() {
    clear(this.tabs);
    const flagsOn = (s) => this.ws.review.forView("schematic", s.name_path).filter((f) => f.status === "open" || f.status === "sent").length;
    const name = (s) => s.name_path === "/" ? "Root" : s.name;
    const count = (n) => n ? h("span.count", { style: { height: "15px", minWidth: "15px", fontSize: "10px" } }, String(n)) : null;
    this.tabs.classList.toggle("compact", !!this.compactTabs);
    if (this.compactTabs) {
      const i = Math.max(0, this.sheets.findIndex((s) => s.name_path === this.cur));
      const s = this.sheets[i];
      const go = (k) => { const t = this.sheets[(k + this.sheets.length) % this.sheets.length]; if (t) this.showSheet(t.name_path); };
      const others = this.sheets.reduce((a, x) => a + (x === s ? 0 : flagsOn(x)), 0);
      this.tabs.append(
        h("button.tbtn", { onclick: () => go(i - 1), "data-tip": "Previous sheet" }, icon("chevron-left", 14)),
        h("button.tbtn.sheet-pick", { onclick: (e) => menu(e.currentTarget, this.sheets.map((x) => ({ label: name(x), hint: flagsOn(x) ? `${flagsOn(x)} flags` : x.file,
          checked: x.name_path === this.cur, run: () => this.showSheet(x.name_path) }))), "data-tip": `${s ? s.file : ""} (sheet ${i + 1} of ${this.sheets.length})` },
          h("span.ellipsis", s ? name(s) : ""), count(s ? flagsOn(s) : 0), h("span.n", `${i + 1}/${this.sheets.length}`), icon("chevron-down", 12)),
        h("button.tbtn", { onclick: () => go(i + 1), "data-tip": "Next sheet" }, icon("chevron-right", 14)),
        others ? h("span.count.muted", { "data-tip": "Flags on the other sheets" }, String(others)) : null);
      return;
    }
    for (const s of this.sheets) {
      this.tabs.appendChild(h("button.tbtn" + (s.name_path === this.cur ? ".on" : ""), { onclick: () => this.showSheet(s.name_path), "data-tip": s.file },
        name(s), count(flagsOn(s))));
    }
    requestAnimationFrame(() => this.fitTabs());
  }

  fitTabs() {
    if (this.compactTabs || !this.tabs.isConnected) return;
    const r = this.tabs.getBoundingClientRect(), v = this.viewer.getBoundingClientRect();
    if (!r.width || !v.width) return;
    if (r.height > 44 || r.width > v.width - 380) { this.compactTabs = true; this.renderTabs(); }
    if (!this.tabsRO) {
      let lastW = v.width;
      this.tabsRO = new ResizeObserver(() => {
        const w = this.viewer.getBoundingClientRect().width;
        if (Math.abs(w - lastW) < 40) return;
        lastW = w; this.compactTabs = false; this.renderTabs();
      });
      this.tabsRO.observe(this.viewer);
    }
  }

  sheet() { return this.sheets.find((s) => s.name_path === this.cur); }

  showSheet(np, keepView) {
    const prev = this.cur;
    this.cur = np;
    this.renderTabs();
    const s = this.sheet();
    let [W, H] = PAPER[s.paper] || [420, 297];
    if (s.portrait) [W, H] = [H, W];
    this.W = W; this.H = H;
    for (const r of [this.paper, this.shadow]) { r.setAttribute("width", W); r.setAttribute("height", H); }
    this.shadow.setAttribute("x", 1.2); this.shadow.setAttribute("y", 1.6);
    this.img.setAttribute("width", W); this.img.setAttribute("height", H);
    this.src = `/api/projects/${encodeURIComponent(this.pid)}/schematic/svg?sheet=${encodeURIComponent(np)}&v=${this.version}_${Date.now()}`;
    this.img.setAttribute("href", this.src);
    this.imgCache = null;
    this.drawOverlay();
    if (!keepView || prev !== np || !this.vb) this.fit();
    else this.applyVB();
    this.flagLayer.render();
  }

  drawOverlay() {
    const s = this.sheet();
    while (this.overlay.firstChild) this.overlay.removeChild(this.overlay.firstChild);
    this.boxes = [];
    for (const sym of s.symbols) {
      if (sym.power) continue;
      const [x0, y0, x1, y1] = sym.bbox;
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", x0 - 0.5); r.setAttribute("y", y0 - 0.5); r.setAttribute("width", x1 - x0 + 1); r.setAttribute("height", y1 - y0 + 1);
      r.setAttribute("rx", 0.8);
      const on = this.sel.has(sym.ref), lit = this.hl && this.hl.has(sym.ref);
      r.setAttribute("fill", lit ? "rgba(255,200,60,.28)" : on ? "rgba(91,155,248,.14)" : "rgba(0,0,0,0)");
      r.setAttribute("stroke", lit ? "rgba(230,150,0,.95)" : on ? "rgba(91,155,248,.9)" : "rgba(0,0,0,0)");
      r.setAttribute("stroke-width", lit ? 0.6 : 0.4);
      r.style.cursor = "pointer";
      r.addEventListener("mouseenter", (e) => { if (this.flags.active) return; if (!on && !lit) r.setAttribute("stroke", "rgba(91,155,248,.5)"); this.showTip(e, sym); });
      r.addEventListener("mouseleave", () => { if (!this.sel.has(sym.ref) && !(this.hl && this.hl.has(sym.ref))) r.setAttribute("stroke", "rgba(0,0,0,0)"); this.tip.style.display = "none"; });
      r.addEventListener("click", (e) => { if (this.flags.active) return; e.stopPropagation(); this.toggle(sym.ref, e.shiftKey); });
      this.overlay.appendChild(r);
      this.boxes.push({ sym, r });
    }
    for (const ch of s.children || []) {
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", ch.at[0]); r.setAttribute("y", ch.at[1]); r.setAttribute("width", ch.size[0]); r.setAttribute("height", ch.size[1]);
      r.setAttribute("fill", "rgba(0,0,0,0)"); r.setAttribute("stroke", "rgba(0,0,0,0)");
      r.style.cursor = "zoom-in";
      r.addEventListener("dblclick", (e) => { e.stopPropagation(); const t = this.sheets.find((x) => x.path === ch.path); if (t) this.showSheet(t.name_path); });
      r.addEventListener("mouseenter", (e) => { if (this.flags.active) return; r.setAttribute("stroke", "rgba(91,155,248,.6)"); this.showTip(e, null, `<b>${escH(ch.name)}</b><br><span class="k">${escH(ch.file)} · double-click to open</span>`); });
      r.addEventListener("mouseleave", () => { r.setAttribute("stroke", "rgba(0,0,0,0)"); this.tip.style.display = "none"; });
      this.overlay.appendChild(r);
    }
  }

  showTip(e, sym, html) {
    const r = this.viewer.getBoundingClientRect();
    this.tip.innerHTML = html || `<b>${escH(sym.ref)}</b> ${escH(sym.val)}<br><span class="k">${escH(sym.lib.split(":").pop())}${sym.fp ? " · " + escH(sym.fp.split(":").pop()) : ""}</span>` +
      (sym.fields && (sym.fields.MPN || sym.fields.LCSC) ? `<br><span class="k">${escH(sym.fields.MPN || "")} ${escH(sym.fields.LCSC || "")}</span>` : "");
    this.tip.style.display = "block";
    this.tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 260) + "px";
    this.tip.style.top = Math.min(e.clientY - r.top + 12, r.height - 70) + "px";
  }

  toggle(ref, add) {
    if (!add) this.sel.clear();
    if (this.sel.has(ref)) this.sel.delete(ref); else this.sel.add(ref);
    this.drawOverlay();
    this.renderSel();
  }

  clearSel() { this.sel.clear(); this.drawOverlay(); this.renderSel(); }

  renderSel(quiet) {
    const items = [...this.sel].map((r) => ({ ref: r, kind: "symbol", sheet: this.cur }));
    if (!quiet) this.ws.select(items, "schematic");
    clear(this.selBar);
    this.selBar.style.display = items.length ? "flex" : "none";
    if (!items.length) return;
    const refs = [...this.sel];
    const label = refs.join(", ");
    this.selBar.append(icon("waypoints", 14), h("span.sl", label.length > 60 ? label.slice(0, 60) + "..." : label),
      h("button.tbtn", { onclick: () => this.ws.ask(`About ${label} on the ${this.cur} sheet: `), "data-tip": "Ask Claude about the selection" }, icon("message-square", 14), h("span", "Ask")),
      h("button.tbtn", { "data-tip": "Flag the selection", onclick: () => {
        const b = this.selBox();
        if (b) this.flags.create({ x: b[0], y: b[1], region: b, refs });
      } }, icon("flag", 14), h("span", "Flag")),
      h("button.tbtn", { onclick: () => { this.ws.show("board"); this.ws.view("board").probe(refs, "schematic", true); }, "data-tip": "Show on the board" }, icon("circuit-board", 14)),
      h("button.tbtn", { onclick: () => { this.ws.show("bom"); this.ws.view("bom").probe(refs, "schematic"); }, "data-tip": "Show in the BOM" }, icon("list", 14)),
      h("button.tbtn", { onclick: () => this.clearSel(), "data-tip": "Clear the selection" }, icon("x", 14)));
  }

  selBox() {
    const syms = this.sheet().symbols.filter((y) => this.sel.has(y.ref));
    if (!syms.length) return null;
    return [Math.min(...syms.map((y) => y.bbox[0])) - 2, Math.min(...syms.map((y) => y.bbox[1])) - 2,
            Math.max(...syms.map((y) => y.bbox[2])) + 2, Math.max(...syms.map((y) => y.bbox[3])) + 2];
  }

  highlight(e) {
    const refs = e.refs || [];
    if (!refs.length && !e.sheet) return;
    let target = e.sheet ? this.sheets.find((s) => s.name_path === e.sheet) : null;
    if (!target) target = this.sheets.find((s) => s.symbols.some((y) => refs.includes(y.ref)));
    if (!target) return;
    this.hl = new Set(refs);
    if (target.name_path !== this.cur) this.showSheet(target.name_path);
    this.drawOverlay();
    const syms = target.symbols.filter((y) => refs.includes(y.ref));
    if (syms.length) {
      const x0 = Math.min(...syms.map((y) => y.bbox[0])), y0 = Math.min(...syms.map((y) => y.bbox[1]));
      const x1 = Math.max(...syms.map((y) => y.bbox[2])), y1 = Math.max(...syms.map((y) => y.bbox[3]));
      const m = 25;
      this.setVB(x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m, true);
    }
    clearTimeout(this.hlT);
    this.hlT = setTimeout(() => { this.hl = null; this.drawOverlay(); }, 12000);
  }

  focus(w) {
    if (w.sheet && w.sheet !== this.cur && this.sheets.find((s) => s.name_path === w.sheet)) this.showSheet(w.sheet);
    if (w.x !== undefined) this.setVB(w.x - 30, w.y - 20, 60, 40, true);
    if (w.ref) this.highlight({ refs: [w.ref] });
  }

  focusFlag(f) {
    const w = f.where || {};
    const go = () => {
      if (w.sheet && w.sheet !== this.cur && this.sheets.find((s) => s.name_path === w.sheet)) this.showSheet(w.sheet);
      if (w.region) this.setVB(w.region[0] - 15, w.region[1] - 12, w.region[2] - w.region[0] + 30, w.region[3] - w.region[1] + 24, true);
      else if (w.x !== undefined) this.setVB(w.x - 40, w.y - 26, 80, 52, true);
      this.flagLayer.mark(f.id);
      setTimeout(() => this.flagLayer.mark(null), 1600);
    };
    if (this.sheets.length) go(); else setTimeout(go, 600);
  }

  // ------------------------------------------------------------------ flags: what is there, a picture of it
  context(w) {
    const s = this.sheet();
    const out = { ...w, sheet: this.cur };
    if (!s) return out;
    const syms = s.symbols.filter((y) => !y.power);
    if (w.region) {
      const [x0, y0, x1, y1] = w.region;
      out.refs = syms.filter((y) => { const cx = (y.bbox[0] + y.bbox[2]) / 2, cy = (y.bbox[1] + y.bbox[3]) / 2; return cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1; }).map((y) => y.ref).slice(0, 16);
    } else {
      const hit = syms.filter((y) => w.x >= y.bbox[0] - 1 && w.x <= y.bbox[2] + 1 && w.y >= y.bbox[1] - 1 && w.y <= y.bbox[3] + 1)
        .sort((a, b) => (a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]) - (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]));
      if (hit.length) out.refs = [hit[0].ref];
    }
    return out;
  }

  async snapshot(w) {
    if (!this.imgCache) {
      this.imgCache = await new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = this.src; });
    }
    const im = this.imgCache, W = 800, H = 520;
    let box;
    if (w.region) { const m = 10; box = [w.region[0] - m, w.region[1] - m, w.region[2] + m, w.region[3] + m]; }
    else { const span = Math.min(220, Math.max(50, this.vb ? this.vb[2] * 0.6 : 90)); box = [w.x - span / 2, w.y - span / 3, w.x + span / 2, w.y + span / 3]; }
    const s = Math.min(W / (box[2] - box[0]), H / (box[3] - box[1]));
    const ox = W / 2 - (box[0] + box[2]) / 2 * s, oy = H / 2 - (box[1] + box[3]) / 2 * s;
    const c = document.createElement("canvas");
    c.width = W; c.height = H;
    const g = c.getContext("2d");
    g.fillStyle = "#fbfaf6"; g.fillRect(0, 0, W, H);
    g.drawImage(im, ox, oy, this.W * s, this.H * s);
    g.strokeStyle = "rgba(214,98,34,1)";
    if (w.region) {
      g.setLineDash([7, 5]); g.lineWidth = 2.5;
      g.strokeRect(w.region[0] * s + ox, w.region[1] * s + oy, (w.region[2] - w.region[0]) * s, (w.region[3] - w.region[1]) * s);
    } else {
      const sx = w.x * s + ox, sy = w.y * s + oy;
      g.lineWidth = 3; g.beginPath(); g.arc(sx, sy, 16, 0, Math.PI * 2); g.stroke();
    }
    return c.toDataURL("image/png");
  }

  // ------------------------------------------------------------------ find
  wireFind() {
    let items = [], sel = 0;
    const draw = () => {
      clear(this.findRes);
      this.findRes.style.display = items.length ? "" : "none";
      items.forEach((it, i) => this.findRes.appendChild(h("div.fr" + (i === sel ? ".on" : ""), { onmousedown: (e) => { e.preventDefault(); pick(i); } },
        icon("microchip", 13), h("span", it.ref), h("span.k", `${it.val} · ${it.sheetName}`))));
    };
    const pick = (i) => {
      const it = items[i];
      if (!it) return;
      items = []; draw(); this.findIn.value = ""; this.findIn.blur();
      this.sel = new Set([it.ref]);
      this.highlight({ refs: [it.ref], sheet: it.sheet });
      this.renderSel();
    };
    this.findIn.addEventListener("input", () => {
      const q = this.findIn.value.trim().toLowerCase();
      sel = 0;
      if (!q) { items = []; draw(); return; }
      items = [];
      for (const s of this.sheets) for (const y of s.symbols) {
        if (y.power) continue;
        if (y.ref.toLowerCase().startsWith(q) || (y.val || "").toLowerCase().includes(q)) items.push({ ref: y.ref, val: y.val || "", sheet: s.name_path, sheetName: s.name_path === "/" ? "root" : s.name });
      }
      items.sort((a, b) => (a.ref.toLowerCase() === q ? -1 : 0) - (b.ref.toLowerCase() === q ? -1 : 0) || a.ref.localeCompare(b.ref, undefined, { numeric: true }));
      items = items.slice(0, 10);
      draw();
    });
    this.findIn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
      else if (e.key === "Escape") { items = []; draw(); this.findIn.blur(); }
    });
    this.findIn.addEventListener("blur", () => setTimeout(() => { items = []; draw(); }, 120));
  }

  // ------------------------------------------------------------------ view box
  fit() { this.setVB(-6, -6, this.W + 12, this.H + 12); }

  setVB(x, y, w, hh, animate) {
    const r = this.viewer.getBoundingClientRect();
    const asp = r.width / Math.max(r.height, 1);
    if (w / hh < asp) { const nw = hh * asp; x -= (nw - w) / 2; w = nw; } else { const nh = w / asp; y -= (nh - hh) / 2; hh = nh; }
    const to = [x, y, w, hh];
    if (!animate || !this.vb) { this.vb = to; this.applyVB(); return; }
    const from = this.vb.slice(), t0 = performance.now();
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / 380), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      this.vb = from.map((v, i) => v + (to[i] - v) * e);
      this.applyVB();
      if (k < 1) requestAnimationFrame(step);
    };
    step();
  }

  applyVB() {
    this.svg.setAttribute("viewBox", this.vb.join(" "));
    const s = this.sheet();
    const r = this.viewer.getBoundingClientRect();
    const zoom = r.width ? Math.round(100 * (r.width / this.vb[2]) / (r.width / (this.W + 12))) : 100;
    clear(this.info).append(h("span", h("b", s ? (s.name_path === "/" ? "Root" : s.name) : "")), h("span", s ? s.file : ""), h("span", `${zoom}%`));
    this.flagLayer.update();
  }

  toPage(px, py) { const r = this.svg.getBoundingClientRect(), [x, y, w, hh] = this.vb; return [x + px / r.width * w, y + py / r.height * hh]; }
  toScreen(x, y) {
    if (!this.vb) return null;
    const r = this.svg.getBoundingClientRect();
    if (!r.width) return null;
    const [vx, vy, vw, vh] = this.vb;
    return [(x - vx) / vw * r.width, (y - vy) / vh * r.height];
  }

  zoom(f, cx, cy) {
    const [x, y, w, hh] = this.vb;
    cx = cx ?? x + w / 2; cy = cy ?? y + hh / 2;
    const nw = Math.min(Math.max(w * f, 8), this.W * 3), k = nw / w;
    this.vb = [cx - (cx - x) * k, cy - (cy - y) * k, nw, hh * k];
    this.applyVB();
  }

  mouse() {
    const s = this.svg;
    const pt = (e) => { const r = s.getBoundingClientRect(); return this.toPage(e.clientX - r.left, e.clientY - r.top); };
    s.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (e.ctrlKey || !e.shiftKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)) { const [cx, cy] = pt(e); this.zoom(Math.exp(e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)), cx, cy); }
      else { const [x, y, w, hh] = this.vb, r = s.getBoundingClientRect(); this.vb = [x + e.deltaX / r.width * w, y + e.deltaY / r.height * hh, w, hh]; this.applyVB(); }
    }, { passive: false });
    let drag = null;
    s.addEventListener("mousedown", (e) => {
      if (this.flags.down(e)) return;
      drag = { x: e.clientX, y: e.clientY, vb: this.vb.slice(), moved: false };
    });
    addEventListener("mousemove", (e) => {
      if (!drag) return;
      const r = s.getBoundingClientRect();
      const dx = (e.clientX - drag.x) / r.width * drag.vb[2], dy = (e.clientY - drag.y) / r.height * drag.vb[3];
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) { drag.moved = true; s.style.cursor = "grabbing"; }
      if (drag.moved) { this.vb = [drag.vb[0] - dx, drag.vb[1] - dy, drag.vb[2], drag.vb[3]]; this.applyVB(); }
    });
    addEventListener("mouseup", (e) => {
      if (drag && !drag.moved && e.target === s) this.clearSel();
      if (drag) s.style.cursor = this.flags.active ? "crosshair" : "grab";
      drag = null;
    });
    new ResizeObserver(() => {
      const w = this.viewer.getBoundingClientRect().width;
      this.viewer.classList.toggle("narrow", w < 860); this.viewer.classList.toggle("narrower", w < 640);
      if (this.vb) this.applyVB();
    }).observe(this.el);
  }
}

function escH(s) { return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
