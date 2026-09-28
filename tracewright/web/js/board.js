// The live board: a canvas view of the .kicad_pcb that follows every change -- Claude's placement
// (animated), the router (net by net), the user's moves in KiCad, highlights, notes and findings --
// with a layers panel, find, a measuring tool and the review flags.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { FlagLayer, FlagTool, flagEditor } from "./review.js";

const COL = {
  "F.Cu": [226, 64, 64], "B.Cu": [64, 110, 232], "In1.Cu": [214, 170, 48], "In2.Cu": [70, 180, 100], "In3.Cu": [180, 100, 210],
  "In4.Cu": [60, 180, 180], pad: [201, 171, 92], padB: [150, 132, 90], via: [196, 200, 210], silkF: [236, 236, 236],
  silkB: [200, 150, 230], edge: [238, 214, 72], sel: [91, 155, 248], hl: [255, 226, 110], kicad: [67, 194, 131],
};
const rgba = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const ease = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

// A net's kind in words, for tooltips and the spotlight card.
export function netKindText(t, short = (n) => n) {
  if (!t) return "";
  if (t.kind === "ground") return "Ground";
  if (t.kind === "power") return t.voltage != null ? `Supply, ${t.voltage} V` : "Supply";
  if (t.kind === "pair") return `Differential pair${t.iface ? ` (${({ usb: "USB", hdmi: "HDMI", mipi: "MIPI", eth: "Ethernet", lvds: "LVDS", pcie: "PCIe", sata: "SATA", clk: "clock", hs: "high speed" })[t.iface] || t.iface})` : ""}${t.pair ? ` with ${short(t.pair)}` : ""}`;
  if (t.kind === "clock") return "Clock";
  if (t.kind === "fast") return "Fast interface line";
  if (t.kind === "unconnected") return "Not connected";
  return "Signal";
}

export class BoardView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.data = null; this.scale = 10; this.ox = 0; this.oy = 0; this.side = "F";
    this.vis = { "F.Cu": true, "B.Cu": true, inner: true, zones: true, silk: true, labels: true, unrouted: true, findings: true,
                 fab: false, courtyard: false, notes: true, vias: true, flags: true };
    this.sel = new Set(); this.selNet = null; this.kicadSel = new Set();
    this.hl = null; this.anim = {}; this.override = {}; this.live = []; this.notes = []; this.findings = [];
    this.tool = "select"; this.measure = null;
    // the copper inspector: copper coloured by net, one layer at a time, a net in the spotlight
    this.panel = localStorage.getItem("tw.board.panel") || "layers"; this.solo = null; this.spot = null; this.showIslands = true; this.netQ = "";
    this.build();
    this.load(true);
  }

  build() {
    this.canvas = h("canvas");
    this.canvas.__view = this;                         // for debugging from the console
    this.ctx = this.canvas.getContext("2d");
    this.tip = h("div.vtip", { style: { display: "none" } });
    this.coords = h("div.coords", "");
    this.layerBox = h("div.hudbox.col.lpanel");
    this.selBar = h("div.selbar", { style: { display: "none" } });
    this.flash = h("div.flash", { style: { display: "none" } });
    this.banner = h("div.vbanner", { style: { display: "none" } });
    this.progress = h("div.hudbox", { style: { display: "none", padding: "0 10px", height: "32px", fontSize: "12px", gap: "8px" } });
    this.measureEl = h("div.measure", { style: { display: "none" } });
    this.sideBtn = h("button.tbtn", { onclick: () => this.setSide(), "data-tip": "Flip board", "data-kbd": "b" }, icon("layers", 14), h("span", "Top"));
    this.findIn = h("input", { placeholder: "Find a part or net", spellcheck: false });
    this.findRes = h("div.findres", { style: { display: "none" } });
    const find = h("div.findbox", icon("search", 13), this.findIn, this.findRes);
    this.wireFind();
    this.toolBtns = {
      select: h("button.tbtn.on", { onclick: () => this.setTool("select"), "data-tip": "Select and pan" }, icon("mouse-pointer-2", 15)),
      measure: h("button.tbtn", { onclick: () => this.setTool(this.tool === "measure" ? "select" : "measure"), "data-tip": "Measure", "data-kbd": "m" }, icon("ruler", 15)),
      flag: h("button.tbtn", { onclick: () => this.flags.toggle(), "data-tip": "Flag an issue", "data-kbd": "c" }, icon("flag", 15)),
    };
    const tools = h("div.hudbox", this.sideBtn, h("div.tsep"), find, h("div.tsep"), this.toolBtns.select, this.toolBtns.measure, this.toolBtns.flag, h("div.tsep"),
      h("button.tbtn", { "data-tip": "Zoom out", onclick: () => this.zoomAt(this.w / 2, this.h / 2, 1 / 1.4) }, icon("zoom-out", 15)),
      h("button.tbtn", { "data-tip": "Zoom in", onclick: () => this.zoomAt(this.w / 2, this.h / 2, 1.4) }, icon("zoom-in", 15)),
      h("button.tbtn", { "data-tip": "Fit the board", "data-kbd": "f", onclick: () => this.fit(true) }, icon("scan", 15)), h("div.tsep"),
      h("button.tbtn", { "data-tip": "Timelapse", "data-kbd": "mod+shift+t", onclick: () => this.ws.timelapse() }, icon("play", 15)));
    this.hudTc = h("div.hud.tc");
    this.el.appendChild(h("div.viewer", this.canvas,
      h("div.hud.tl", this.layerBox), h("div.hud.tr", tools), this.hudTc,
      h("div.hud.bl", h("div.hudbox", this.coords), this.progress),
      h("div.hud.br", this.selBar), this.tip, this.flash, this.banner, this.measureEl));
    this.viewer = this.el.firstChild;
    this.flagLayer = new FlagLayer(this.viewer, this.ws.review, {
      view: "board", project: (x, y) => this.data ? this.toScreen(x, y) : null, showDone: () => this.vis.flags !== false,
      onPin: (f, pin) => { this.flagLayer.mark(f.id); flagEditor(pin, this.ws, { flag: f, onDone: () => this.flagLayer.mark(null) }); } });
    this.flags = new FlagTool(this.ws, {
      view: "board", surface: this.canvas, layer: this.flagLayer, hud: this.hudTc,
      toWorld: (px, py) => this.data ? this.toWorld(px, py) : null,
      context: (w) => this.context(w), snapshot: (w) => this.snapshot(w),
      onChange: (on) => { this.viewer.classList.toggle("tool-flag", on); this.toolBtns.flag.classList.toggle("on", on); if (on) this.setTool("select", true); } });
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.viewer);
    this.mouse();
    this.keys = (e) => {
      if (!this.el.classList.contains("on") || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (document.querySelector(".modal-bg, .popover, .palette-bg")) return;
      const k = e.key.toLowerCase();
      if (k === "f") this.fit(true);
      else if (k === "b") this.setSide();
      else if (k === "c") this.flags.toggle();
      else if (k === "m") this.setTool(this.tool === "measure" ? "select" : "measure");
      else if (e.key === "[" || e.key === "]") this.stepLayer(e.key === "]" ? 1 : -1);
      else if (k === "n") this.setPanel(this.panel === "copper" ? "layers" : "copper");
      else if (e.key === "Escape") {
        if (this.flags.active) this.flags.toggle(false);
        else if (this.tool !== "select") this.setTool("select");
        else if (this.spot) this.setSpot(null);
        else { this.sel.clear(); this.selNet = null; this.updateSel(); this.hl = null; this.dirty(); }
      } else return;
      e.preventDefault();
    };
    document.addEventListener("keydown", this.keys);
  }

  destroy() { document.removeEventListener("keydown", this.keys); this.ro.disconnect(); cancelAnimationFrame(this.raf); this.flagLayer.destroy(); }
  shown() {
    this.resize();
    if (this.stale) this.load();
    if (this.pendingFly && this.data) { this.pendingFly = false; setTimeout(() => this.flySel(), 30); }
  }

  // parts picked in another view (schematic, 3D, BOM): select them here too
  probe(refs, source, fly) {
    if (!this.data) { this.pendingSel = refs; return; }
    this.sel = new Set((refs || []).filter((r) => this.byRef[r]));
    this.selNet = null;
    this.renderSelBar();
    this.dirty();
    if (!this.sel.size) return;
    if (fly || this.el.classList.contains("on")) this.flySel(); else this.pendingFly = true;
  }

  flySel() {
    const box = this.selectionBox();
    if (!box) return;
    const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
    const w = Math.max(box[2] - box[0], 14), hh = Math.max(box[3] - box[1], 10);
    this.flyTo([cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2], 420, 0.7);
  }
  focusFind() { this.findIn.focus(); this.findIn.select(); }

  setTool(t, quiet) {
    this.tool = t;
    for (const [k, b] of Object.entries(this.toolBtns)) if (k !== "flag") b.classList.toggle("on", k === t);
    this.viewer.classList.toggle("tool-measure", t === "measure");
    if (t !== "measure") { this.measure = null; this.measureEl.style.display = "none"; this.dirty(); }
    if (t !== "select" && !quiet && this.flags.active) this.flags.toggle(false);
  }

  async load(first) {
    let d;
    try { d = await api(`/api/projects/${encodeURIComponent(this.pid)}/board`); }
    catch (e) { toast("Board: " + e.message, "error"); return; }
    this.stale = false;
    if (d.empty) { this.data = null; this.showBanner(); this.dirty(); this.flagLayer.update(); return; }
    this.banner.style.display = "none";
    const firstData = !this.data;
    this.data = d;
    this.override = {};
    this.live = [];
    this.prepare();
    if (firstData && this.panel === "copper" && !this.solo) this.solo = this.copper[0];
    if (this.spot && !(d.nets || []).includes(this.spot)) this.spot = null;
    this.renderLayers();
    if (first || firstData) this.fit(false);
    this.showCoords(null);
    this.loadExtras();
    if (this.pendingSel) { const r = this.pendingSel; this.pendingSel = null; this.probe(r, "pending"); }
    this.dirty();
  }

  async loadExtras() {
    try { const a = await api(`/api/projects/${encodeURIComponent(this.pid)}/annotations`); this.notes = a || []; } catch {}
    try {                                                   // what kind each net is: tags and filters in the Copper panel
      const n = await api(`/api/projects/${encodeURIComponent(this.pid)}/nets`);
      this.netKinds = n.nets || {};
      if (this.panel === "copper") this.renderLayers();
    } catch {}
    try {
      const c = await api(`/api/projects/${encodeURIComponent(this.pid)}/checks`);
      this.findings = [];
      for (const ch of c.checks || []) for (const f of ch.findings || []) {
        const w = f.where || {};
        if (w.x !== undefined && w.x !== null && !w.sheet) this.findings.push({ x: w.x, y: w.y, sev: f.severity, msg: f.message });
      }
    } catch {}
    this.dirty();
  }

  reload(e) {
    if (!this.el.classList.contains("on")) { this.stale = true; return; }
    if (Object.keys(this.anim).length) { this.pendingReload = true; return; }
    clearTimeout(this.reloadT);
    this.reloadT = setTimeout(() => this.load(), 150);
  }

  showBanner() {
    clear(this.banner);
    this.banner.style.display = "flex";
    this.banner.appendChild(h("div", h("div.eicon", icon("circuit-board", 22)), h("h3", "No board yet"),
      h("p", "Claude creates the board from the schematic."),
      h("button.btn.primary", { onclick: () => this.ws.ask("Create the board from the schematic (sync_board), then propose an outline, mounting holes and connector positions.") }, "Start the layout")));
  }

  // ------------------------------------------------------------------ geometry caches
  prepare() {
    const d = this.data;
    this.copper = d.copper || ["F.Cu", "B.Cu"];
    const byW = {};
    for (const t of d.tracks) {
      const [x1, y1, x2, y2, w, l] = t;
      const k = l + "|" + w;
      if (!byW[k]) byW[k] = { l, w, p: new Path2D() };
      const p = byW[k].p;
      p.moveTo(x1, y1);
      if (t.length > 7) p.lineTo(t[7], t[8]);
      p.lineTo(x2, y2);
    }
    this.trackPaths = Object.values(byW);
    this.zonePaths = {};
    this.rulePaths = [];
    for (const z of d.zones) {
      if (z.rule) {
        const p = new Path2D();
        for (const o of z.o) poly(p, o);
        this.rulePaths.push({ p, z });
        continue;
      }
      for (const [l, polys] of Object.entries(z.f || {})) {
        const p = this.zonePaths[l] || (this.zonePaths[l] = new Path2D());
        for (const pl of polys) poly(p, pl);
      }
    }
    this.fps = d.footprints.map((f) => this.fpCache(f));
    this.byRef = {};
    for (const f of this.fps) this.byRef[f.ref] = f;
    const edge = new Path2D();
    for (const l of d.outline) poly(edge, l);
    for (const l of d.outline_open || []) { edge.moveTo(l[0][0], l[0][1]); for (const q of l.slice(1)) edge.lineTo(q[0], q[1]); }
    this.edge = edge;
    this.boardFill = new Path2D();
    if (d.outline.length) poly(this.boardFill, d.outline[0]);
    const silk = { F: new Path2D(), B: new Path2D() };
    for (const s of d.shapes) {
      const k = s.l.startsWith("F.Silk") ? "F" : s.l.startsWith("B.Silk") ? "B" : null;
      if (k) shapePath(silk[k], s);
    }
    this.boardSilk = silk;
    this.prepareCopper();
  }

  // ------------------------------------------------------------------ the copper inspector: nets, pours, islands
  prepareCopper() {
    const d = this.data;
    this.netColor = netColors(d.nets || []);
    const byNet = {};
    for (const t of d.tracks) {
      const k = t[5] + "|" + t[6] + "|" + t[4];
      if (!byNet[k]) byNet[k] = { l: t[5], net: t[6], w: t[4], p: new Path2D() };
      const p = byNet[k].p;
      p.moveTo(t[0], t[1]);
      if (t.length > 7) p.lineTo(t[7], t[8]);
      p.lineTo(t[2], t[3]);
    }
    this.trackNetPaths = Object.values(byNet);
    this.pours = [];
    this.zoneNetPaths = {};
    for (const z of d.zones) {
      if (z.rule) continue;
      for (const [l, polys] of Object.entries(z.f || {})) {
        if (!polys.length) continue;
        const byL = this.zoneNetPaths[l] || (this.zoneNetPaths[l] = {});
        const p = byL[z.net] || (byL[z.net] = new Path2D());
        let area = 0, bb = [Infinity, Infinity, -Infinity, -Infinity];
        for (const pl of polys) { poly(p, pl); area += Math.abs(polyArea(pl)); bb = growBox(bb, pl); }
        this.pours.push({ net: z.net, layer: l, name: z.name || "", pri: z.pri || 0, polys, area, bbox: bb, outline: z.o || [] });
      }
    }
    this.pours.sort((a, b) => layerIndex(this.copper, a.layer) - layerIndex(this.copper, b.layer) || b.area - a.area);
    this.islands = findIslands(this.pours, this.fps, d.vias, d.tracks);
    this.netStats = null;
  }

  stats(net) {
    const d = this.data, s = { pads: 0, vias: 0, tracks: {}, pour: {}, parts: new Set() };
    for (const f of this.fps) for (const p of f.pads) if (p.net === net) { s.pads++; s.parts.add(f.ref); }
    for (const v of d.vias) if (v[4] === net) s.vias++;
    for (const t of d.tracks) if (t[6] === net) s.tracks[t[5]] = (s.tracks[t[5]] || 0) + (t.length > 7 ? Math.hypot(t[7] - t[0], t[8] - t[1]) + Math.hypot(t[2] - t[7], t[3] - t[8]) : Math.hypot(t[2] - t[0], t[3] - t[1]));
    for (const p of this.pours) if (p.net === net) s.pour[p.layer] = (s.pour[p.layer] || 0) + p.area;
    return s;
  }

  setPanel(p) {
    this.panel = p;
    localStorage.setItem("tw.board.panel", p);
    if (p !== "copper") this.solo = null;
    else if (!this.solo && this.copper) this.solo = this.side === "F" ? this.copper[0] : this.copper[this.copper.length - 1];   // one layer: each pour in its net's colour
    this.renderLayers();
    this.dirty();
  }

  // [ and ]: the next copper layer (then all of them) in the copper inspector
  stepLayer(dir) {
    if (this.panel !== "copper" || !this.copper) return;
    const seq = [...this.copper, null];
    const i = seq.indexOf(this.solo);
    this.solo = seq[(i + dir + seq.length) % seq.length];
    this.renderLayers();
    this.dirty();
    this.flash.textContent = this.solo ? `${this.solo} only` : "Every copper layer";
    this.flash.style.display = "block";
    clearTimeout(this.flashT); this.flashT = setTimeout(() => this.flash.style.display = "none", 1400);
  }

  setSpot(net, fly) {
    this.spot = net || null;
    if (this.panel !== "copper" && net) { this.panel = "copper"; localStorage.setItem("tw.board.panel", "copper"); }
    this.renderLayers();
    if (net && fly) {
      const pts = [];
      for (const t of this.data.tracks) if (t[6] === net) pts.push([t[0], t[1]], [t[2], t[3]]);
      for (const p of this.pours) if (p.net === net) pts.push([p.bbox[0], p.bbox[1]], [p.bbox[2], p.bbox[3]]);
      for (const f of this.fps) for (const p of f.pads) if (p.net === net) pts.push([p.x, p.y]);
      if (pts.length) this.flyTo(padBox([Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])), Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))], 2), 420, 0.82);
    }
    this.dirty();
  }

  // the pour under a point, top visible layer first
  pickPour(x, y) {
    if (!this.pours) return null;
    const layers = this.visibleCopper().slice().reverse();
    for (const l of layers) for (const p of this.pours) {
      if (p.layer !== l || x < p.bbox[0] || x > p.bbox[2] || y < p.bbox[1] || y > p.bbox[3]) continue;
      for (const pl of p.polys) if (inPoly(x, y, pl)) return p;
    }
    return null;
  }

  visibleCopper() {
    const order = this.side === "F" ? [...this.copper].reverse() : [...this.copper];
    return order.filter((l) => (this.solo ? l === this.solo : this.layerOn(l)));
  }

  fpCache(f) {
    const padF = new Path2D(), padB = new Path2D(), silkF = new Path2D(), silkB = new Path2D(), fab = new Path2D(), cy = new Path2D();
    const holes = [];
    for (const p of f.pads) {
      const onF = p.l.includes("F.Cu"), onB = p.l.includes("B.Cu");
      for (const pl of p.p) { if (onF) poly(padF, pl); if (onB) poly(padB, pl); }
      if (p.d) holes.push([p.x, p.y, Math.min(p.d[0], p.d[1]) / 2]);
    }
    for (const g of f.g) {
      if (g.l === "F.SilkS" || g.l === "F.Silkscreen") shapePath(silkF, g);
      else if (g.l === "B.SilkS" || g.l === "B.Silkscreen") shapePath(silkB, g);
      else if (g.l.endsWith("Fab")) shapePath(fab, g);
    }
    for (const l of f.cy || []) poly(cy, l);
    return { ...f, padF, padB, silkF, silkB, fabP: fab, cyP: cy, holes, area: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]) };
  }

  // ------------------------------------------------------------------ camera
  resize() {
    const r = this.viewer.getBoundingClientRect();
    if (!r.width) return;
    const dpr = window.devicePixelRatio || 1;
    if (this.w && this.fitted) { this.ox += (r.width - this.w) / 2; this.oy += (r.height - this.h) / 2; }   // keep the view centred
    this.w = r.width; this.h = r.height; this.dpr = dpr;
    this.canvas.width = Math.round(r.width * dpr); this.canvas.height = Math.round(r.height * dpr);
    this.layerBox.style.maxHeight = Math.max(160, r.height - 70) + "px";
    this.viewer.classList.toggle("narrow", r.width < 860);
    this.viewer.classList.toggle("narrower", r.width < 640);
    const fold = r.width < 700;                        // a narrow view folds the side panel away (a click opens it)
    if (fold !== !!this.autoFold) { this.autoFold = fold; this.forceOpen = false; if (this.data) this.renderLayers(); }
    if (!this.fitted && this.data) this.fit(false);
    this.dirty();
  }

  fit(animate) {
    if (!this.data || !this.w) return;
    const [x0, y0, x1, y1] = this.data.bbox;
    this.flyTo([x0, y0, x1, y1], animate ? 350 : 0, 0.88);
    this.fitted = true;
  }

  // the part of the view the side panel covers (so a fitted board sits beside it)
  inset() {
    if (!this.layerBox.isConnected || localStorage.getItem("tw.layers.collapsed") !== "0" || (this.autoFold && !this.forceOpen)) return 0;
    const r = this.layerBox.getBoundingClientRect(), v = this.viewer.getBoundingClientRect();
    return r.width ? Math.max(0, r.right - v.left + 6) : 0;
  }

  flyTo([x0, y0, x1, y1], ms = 400, fill = 0.8) {
    const inset = Math.min(this.inset(), this.w * 0.4);
    const w = this.w - inset;
    const s = Math.min(w * fill / Math.max(x1 - x0, 1e-3), this.h * fill / Math.max(y1 - y0, 1e-3));
    const tgt = { scale: Math.min(Math.max(s, 0.5), 400), cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
    const mid = inset + w / 2;
    const from = { scale: this.scale, cx: (mid - this.ox) / this.scale, cy: (this.h / 2 - this.oy) / this.scale };
    const t0 = performance.now();
    const step = () => {
      const k = ms ? Math.min(1, (performance.now() - t0) / ms) : 1;
      const e = ease(k);
      const sc = Math.exp(Math.log(from.scale) + (Math.log(tgt.scale) - Math.log(from.scale)) * e);
      const cx = from.cx + (tgt.cx - from.cx) * e, cy = from.cy + (tgt.cy - from.cy) * e;
      this.scale = sc; this.ox = mid - cx * sc; this.oy = this.h / 2 - cy * sc;
      this.dirty();
      if (k < 1) requestAnimationFrame(step);
    };
    step();
  }

  zoomAt(px, py, f) {
    const s = Math.min(Math.max(this.scale * f, 0.5), 600);
    const k = s / this.scale;
    this.ox = px - (px - this.ox) * k; this.oy = py - (py - this.oy) * k; this.scale = s;
    this.dirty();
  }

  toWorld(px, py) { return [(px - this.ox) / this.scale, (py - this.oy) / this.scale]; }
  toScreen(x, y) { return [x * this.scale + this.ox, y * this.scale + this.oy]; }

  // ------------------------------------------------------------------ input
  mouse() {
    const c = this.canvas;
    let drag = null;
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (e.ctrlKey || Math.abs(e.deltaY) > Math.abs(e.deltaX) * 0.5 && !e.shiftKey) {
        const r = c.getBoundingClientRect();
        this.zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)));
      } else { this.ox -= e.deltaX; this.oy -= e.deltaY; this.dirty(); }
    }, { passive: false });
    c.addEventListener("mousedown", (e) => {
      if (this.flags.down(e)) return;
      const r = c.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      if (this.tool === "measure" && e.button === 0) {
        const [x, y] = this.snapPoint(...this.toWorld(px, py));
        this.measure = { a: [x, y], b: [x, y], on: true };
        this.dirty();
        return;
      }
      drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy, moved: false, px, py, shift: e.shiftKey };
    });
    addEventListener("mousemove", (e) => {
      const r = c.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      if (this.measure && this.measure.on) { this.measure.b = this.snapPoint(...this.toWorld(px, py)); this.dirty(); return; }
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) { drag.moved = true; c.classList.add("panning"); this.tip.style.display = "none"; }
        if (drag.moved) { this.ox = drag.ox + dx; this.oy = drag.oy + dy; this.dirty(); }
        return;
      }
      if (px < 0 || py < 0 || px > this.w || py > this.h || !this.el.classList.contains("on")) { this.tip.style.display = "none"; return; }
      const [x, y] = this.toWorld(px, py);
      this.showCoords(x, y);
      if (!this.flags.active && this.tool === "select") this.hover(px, py, x, y); else this.tip.style.display = "none";
    });
    addEventListener("mouseup", (e) => {
      if (this.measure && this.measure.on) { this.measure.on = false; this.dirty(); return; }
      if (!drag) return;
      c.classList.remove("panning");
      if (!drag.moved && e.target === c) this.click(drag.px, drag.py, drag.shift);
      drag = null;
    });
    c.addEventListener("dblclick", (e) => {
      if (this.flags.active || this.tool !== "select") return;
      const r = c.getBoundingClientRect();
      const [x, y] = this.toWorld(e.clientX - r.left, e.clientY - r.top);
      const f = this.pickFp(x, y);
      if (f) this.flyTo(padBox(f.bbox, 2), 350, 0.6);
    });
    c.addEventListener("mouseleave", () => { this.tip.style.display = "none"; });
  }

  showCoords(x, y) {
    const s = this.data && this.data.summary;
    clear(this.coords).append(...[x === null ? null : h("span", h("b", `${x.toFixed(2)}, ${y.toFixed(2)}`), " mm"),
      s ? h("span", `${s.size_mm[0]} × ${s.size_mm[1]} mm`) : null, h("span", `${Math.round(this.scale * 10)}%`)].filter(Boolean));
  }

  // the measuring tool snaps to pad centres, vias and track ends near the cursor
  snapPoint(x, y) {
    if (!this.data) return [x, y];
    const tol = 6 / this.scale;
    let best = null;
    const tryPt = (px, py) => { const d = Math.hypot(px - x, py - y); if (d < tol && (!best || d < best[0])) best = [d, px, py]; };
    for (const f of this.fps) for (const p of f.pads) tryPt(p.x, p.y);
    for (const v of this.data.vias) tryPt(v[0], v[1]);
    for (const t of this.data.tracks) { tryPt(t[0], t[1]); tryPt(t[2], t[3]); }
    return best ? [best[1], best[2]] : [x, y];
  }

  // find: parts by reference or value, nets by name
  wireFind() {
    let items = [], sel = 0;
    const draw = () => {
      clear(this.findRes);
      this.findRes.style.display = items.length ? "" : "none";
      items.forEach((it, i) => this.findRes.appendChild(h("div.fr" + (i === sel ? ".on" : ""), { onmousedown: (e) => { e.preventDefault(); pick(i); } },
        icon(it.net ? "cable" : "microchip", 13), h("span", it.label), h("span.k", it.desc || ""))));
    };
    const pick = (i) => {
      const it = items[i];
      if (!it) return;
      items = []; draw(); this.findIn.value = ""; this.findIn.blur();
      if (it.net) { this.selNet = it.net; this.sel.clear(); this.updateSel(); this.highlight({ nets: [it.net] }); }
      else { this.sel = new Set([it.ref]); this.selNet = null; this.updateSel(); this.highlight({ refs: [it.ref] }); }
    };
    this.findIn.addEventListener("input", () => {
      const q = this.findIn.value.trim().toLowerCase();
      sel = 0;
      if (!q || !this.data) { items = []; draw(); return; }
      const fps = this.fps.filter((f) => f.ref.toLowerCase().startsWith(q) || (f.val || "").toLowerCase().includes(q))
        .sort((a, b) => (a.ref.toLowerCase() === q ? -1 : 0) - (b.ref.toLowerCase() === q ? -1 : 0) || a.ref.localeCompare(b.ref, undefined, { numeric: true }))
        .slice(0, 8).map((f) => ({ ref: f.ref, label: f.ref, desc: f.val }));
      const nets = (this.data.nets || []).filter((n) => n && n.split("/").pop().toLowerCase().includes(q)).slice(0, 6).map((n) => ({ net: n, label: n.split("/").pop(), desc: "net" }));
      items = [...fps, ...nets]; draw();
    });
    this.findIn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
      else if (e.key === "Escape") { items = []; draw(); this.findIn.blur(); }
    });
    this.findIn.addEventListener("blur", () => setTimeout(() => { items = []; draw(); }, 120));
  }

  pickFp(x, y) {
    if (!this.data) return null;
    let best = null;
    for (const f of this.fps) {
      const p = this.pose(f);
      const [lx, ly] = p ? invPose(p, f, x, y) : [x, y];
      const b = f.bbox;
      if (lx >= b[0] && lx <= b[2] && ly >= b[1] && ly <= b[3]) {
        const sideOk = f.side === this.side ? 0 : 1;
        if (!best || sideOk < best[0] || sideOk === best[0] && f.area < best[1].area) best = [sideOk, f];
      }
    }
    return best ? best[1] : null;
  }

  pickPad(f, x, y) {
    for (const p of f.pads) for (const pl of p.p) if (inPoly(x, y, pl)) return p;
    return null;
  }

  pickTrack(x, y) {
    if (!this.data) return null;
    const tol = 3 / this.scale;
    let best = null;
    for (const t of this.data.tracks) {
      if (!this.layerOn(t[5])) continue;
      const d = segDist(x, y, t[0], t[1], t[2], t[3]) - t[4] / 2;
      if (d < tol && (!best || d < best[0])) best = [d, { kind: "track", net: t[6], layer: t[5], w: t[4] }];
    }
    for (const v of this.data.vias) {
      const d = Math.hypot(x - v[0], y - v[1]) - v[2] / 2;
      if (d < tol && (!best || d < best[0])) best = [d, { kind: "via", net: v[4], d: v[2], drill: v[3] }];
    }
    return best ? best[1] : null;
  }

  // what a flag points at: the part (and pad net) under it, or the parts and nets inside its area
  context(w) {
    const out = { ...w, side: this.side };
    if (!this.data) return out;
    if (w.region) {
      const [x0, y0, x1, y1] = w.region;
      const refs = this.fps.filter((f) => { const cx = (f.bbox[0] + f.bbox[2]) / 2, cy = (f.bbox[1] + f.bbox[3]) / 2; return cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1; })
        .sort((a, b) => b.area - a.area).map((f) => f.ref);
      const nets = new Set();
      for (const t of this.data.tracks) if (t[6] && (inBox(t[0], t[1], w.region) || inBox(t[2], t[3], w.region))) nets.add(t[6]);
      out.refs = refs.slice(0, 16);
      out.nets = [...nets].slice(0, 8);
      return out;
    }
    const f = this.pickFp(w.x, w.y);
    if (f) {
      out.refs = [f.ref];
      const pad = this.pickPad(f, w.x, w.y);
      if (pad && pad.net) out.nets = [pad.net];
      out.side = f.side;
    } else {
      const t = this.pickTrack(w.x, w.y);
      if (t && t.net) { out.nets = [t.net]; if (t.layer) out.layer = t.layer; }
    }
    return out;
  }

  // a crisp picture of the flagged spot for Claude: the board drawn again around it, with the marker
  async snapshot(w) {
    if (!this.data) return null;
    const W = 720, H = 480;
    let box;
    if (w.region) {
      const [x0, y0, x1, y1] = w.region, m = Math.max(2, (x1 - x0 + y1 - y0) * 0.15);
      box = [x0 - m, y0 - m, x1 + m, y1 + m];
    } else {
      const span = Math.min(60, Math.max(10, (this.w / this.scale) * 0.55));
      box = [w.x - span / 2, w.y - span / 3, w.x + span / 2, w.y + span / 3];
    }
    const off = document.createElement("canvas");
    off.width = W; off.height = H;
    const save = { canvas: this.canvas, ctx: this.ctx, w: this.w, h: this.h, dpr: this.dpr, scale: this.scale, ox: this.ox, oy: this.oy, hl: this.hl };
    try {
      this.canvas = off; this.ctx = off.getContext("2d"); this.w = W; this.h = H; this.dpr = 1; this.hl = null;
      const s = Math.min(W / (box[2] - box[0]), H / (box[3] - box[1]));
      this.scale = s; this.ox = W / 2 - (box[0] + box[2]) / 2 * s; this.oy = H / 2 - (box[1] + box[3]) / 2 * s;
      this.drawing = true;
      this.draw();
      const c = this.ctx;
      c.setTransform(1, 0, 0, 1, 0, 0);
      if (w.region) {
        const [ax, ay] = this.toScreen(w.region[0], w.region[1]), [bx, by] = this.toScreen(w.region[2], w.region[3]);
        c.setLineDash([7, 5]); c.lineWidth = 2.5; c.strokeStyle = "rgba(255,170,110,1)"; c.strokeRect(ax, ay, bx - ax, by - ay); c.setLineDash([]);
      } else {
        const [sx, sy] = this.toScreen(w.x, w.y);
        c.lineWidth = 3; c.strokeStyle = "rgba(255,170,110,1)";
        c.beginPath(); c.arc(sx, sy, 16, 0, Math.PI * 2); c.stroke();
        c.lineWidth = 2; c.beginPath(); c.moveTo(sx - 26, sy); c.lineTo(sx - 10, sy); c.moveTo(sx + 10, sy); c.lineTo(sx + 26, sy);
        c.moveTo(sx, sy - 26); c.lineTo(sx, sy - 10); c.moveTo(sx, sy + 10); c.lineTo(sx, sy + 26); c.stroke();
      }
      return off.toDataURL("image/png");
    } finally {
      Object.assign(this, save);
      this.drawing = false;
      this.dirty();
    }
  }

  focusFlag(f) {
    const w = f.where || {};
    if (!this.data) return;
    const box = w.region ? padBox(w.region, 3) : [w.x - 8, w.y - 6, w.x + 8, w.y + 6];
    this.flyTo(box, 420, 0.7);
    this.flagLayer.mark(f.id);
    setTimeout(() => this.flagLayer.mark(null), 1600);
  }

  hover(px, py, x, y) {
    const f = this.pickFp(x, y);
    let html = "";
    if (f) {
      const pad = this.pickPad(f, x, y);
      html = `<b>${f.ref}</b> ${esc(f.val)}<br><span class="k">${esc(f.lib.split(":").pop())} · ${f.side === "F" ? "top" : "bottom"}${f.locked ? " · locked" : ""}</span>`;
      if (pad) html += `<br>pad <b>${esc(pad.n)}</b>${pad.fn ? " " + esc(pad.fn) : ""} · <span class="k">net</span> ${esc((pad.net || "-").split("/").pop())}`;
      if (f.fields && (f.fields.MPN || f.fields.LCSC)) html += `<br><span class="k">${esc(f.fields.MPN || "")} ${esc(f.fields.LCSC || "")}</span>`;
    } else {
      const t = this.pickTrack(x, y);
      if (t) html = t.kind === "track" ? `<b>${esc(t.net.split("/").pop() || "no net")}</b><br><span class="k">track ${t.w} mm · ${t.layer}</span>`
        : `<b>${esc(t.net.split("/").pop() || "no net")}</b><br><span class="k">via ${t.d}/${t.drill} mm</span>`;
      else if (this.panel === "copper") {
        const p = this.pickPour(x, y);
        if (p) html = `<b>${esc(p.net.split("/").pop() || "no net")}</b><br><span class="k">pour on ${p.layer}${p.name ? " · zone " + esc(p.name) : ""} · ${Math.round(p.area)} mm² · priority ${p.pri}</span>`;
      }
    }
    if (html && this.panel === "copper") html += `<br><span class="k">click: spotlight the net</span>`;
    if (!html) { this.tip.style.display = "none"; return; }
    this.tip.innerHTML = html;
    this.tip.style.display = "block";
    this.tip.style.left = Math.min(px + 16, this.w - 260) + "px";
    this.tip.style.top = Math.min(py + 14, this.h - 80) + "px";
  }

  click(px, py, shift) {
    const [x, y] = this.toWorld(px, py);
    if (this.panel === "copper" && !shift) { const n = this.netAt(x, y); this.setSpot(n && n !== this.spot ? n : null); return; }
    const f = this.pickFp(x, y);
    if (f) {
      if (!shift) { this.sel.clear(); this.selNet = null; }
      if (this.sel.has(f.ref)) this.sel.delete(f.ref); else this.sel.add(f.ref);
    } else {
      const t = this.pickTrack(x, y);
      this.sel.clear();
      this.selNet = t && t.net ? t.net : null;
    }
    this.updateSel();
    this.dirty();
  }

  updateSel() {
    const items = [...this.sel].map((r) => ({ ref: r, kind: "footprint" }));
    if (this.selNet) items.push({ net: this.selNet, kind: "net" });
    this.ws.select(items, "board");
    this.renderSelBar();
  }

  renderSelBar() {
    const items = [...this.sel].map((r) => ({ ref: r, kind: "footprint" }));
    if (this.selNet) items.push({ net: this.selNet, kind: "net" });
    clear(this.selBar);
    if (!items.length) { this.selBar.style.display = "none"; return; }
    this.selBar.style.display = "flex";
    const label = items.map((i) => i.ref || "net " + i.net.split("/").pop()).join(", ");
    const refs = [...this.sel];
    this.selBar.append(icon(this.selNet && !refs.length ? "cable" : "microchip", 14), h("span.sl", label.length > 60 ? label.slice(0, 60) + "..." : label),
      h("button.tbtn", { onclick: () => this.ws.ask(`About ${label}: `), "data-tip": "Ask Claude" }, icon("message-square", 14), h("span", "Ask")),
      h("button.tbtn", { "data-tip": "Flag the selection", onclick: () => {
        const box = this.selectionBox();
        if (box) this.flags.create({ x: box[0], y: box[1], region: box, refs, nets: this.selNet ? [this.selNet] : [] });
      } }, icon("flag", 14), h("span", "Flag")),
      refs.length ? h("button.tbtn", { "data-tip": "Show in the schematic", onclick: () => { this.ws.show("schematic"); this.ws.view("schematic").probe(refs, "board", true); } }, icon("waypoints", 14)) : null,
      refs.length ? h("button.tbtn", { "data-tip": "Show in the BOM", onclick: () => { this.ws.show("bom"); this.ws.view("bom").probe(refs, "board"); } }, icon("list", 14)) : null,
      h("button.tbtn", { "data-tip": "Clear the selection", onclick: () => { this.sel.clear(); this.selNet = null; this.updateSel(); this.dirty(); } }, icon("x", 14)));
  }

  selectionBox() {
    const pts = [];
    for (const r of this.sel) { const f = this.byRef[r]; if (f) pts.push([f.bbox[0], f.bbox[1]], [f.bbox[2], f.bbox[3]]); }
    if (this.selNet) for (const t of this.data.tracks) if (t[6] === this.selNet) pts.push([t[0], t[1]], [t[2], t[3]]);
    if (!pts.length) return null;
    return padBox([Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1]))], 0.6);
  }

  // ------------------------------------------------------------------ live events
  highlight(e) {
    if (!this.data) return;
    const refs = new Set(e.refs || []), nets = new Set();
    for (const n of e.nets || []) for (const full of this.data.nets) if (full === n || full.split("/").pop() === n) nets.add(full);
    this.hl = { refs, nets, points: e.points || [], note: e.note || "", t0: performance.now() };
    if (e.note) { this.flash.textContent = e.note; this.flash.style.display = "block"; clearTimeout(this.flashT); this.flashT = setTimeout(() => this.flash.style.display = "none", 7000); }
    let box = e.region ? e.region.slice(0, 4) : null;
    const pts = [];
    for (const r of refs) { const f = this.byRef[r]; if (f) pts.push([f.bbox[0], f.bbox[1]], [f.bbox[2], f.bbox[3]]); }
    for (const n of nets) for (const t of this.data.tracks) if (t[6] === n) pts.push([t[0], t[1]], [t[2], t[3]]);
    for (const n of nets) for (const f of this.fps) for (const p of f.pads) if (p.net === n) pts.push([p.x, p.y]);
    for (const p of this.hl.points) pts.push([p.x, p.y]);
    if (!box && pts.length) {
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      box = padBox([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], 4);
    }
    if (box) {                                        // never closer than ~14 mm across: keep the neighbours in view
      const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
      const w = Math.max(box[2] - box[0], 14), hh = Math.max(box[3] - box[1], 10);
      box = [cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2];
    }
    if (box) this.flyTo(box, 450, 0.7);
    this.dirty();
  }

  animateMoves(moves) {
    if (!this.data) { this.reload(); return; }
    const t0 = performance.now();
    moves.forEach((m, i) => {
      const f = this.byRef[m.ref];
      if (!f) return;
      this.anim[m.ref] = { from: this.pose(f) || { x: f.x, y: f.y, rot: f.a }, to: m.to, t0: t0 + i * 60, ms: 520 };
    });
    this.pendingReload = true;
    this.dirty();
  }

  liveMoves(moves) {
    if (!this.data) return;
    const t0 = performance.now();
    for (const [ref, to] of Object.entries(moves)) {
      const f = this.byRef[ref];
      if (!f) continue;
      this.anim[ref] = { from: this.pose(f) || { x: f.x, y: f.y, rot: f.a }, to, t0, ms: 220, keep: true };
    }
    this.dirty();
  }

  kicadSelection(items) {
    this.kicadSel = new Set((items || []).filter((i) => i.ref).map((i) => i.ref));
    this.dirty();
  }

  routeEvent(e) {
    if (e.status === "routed" || e.status === "escape" || e.status === "stitch") {
      for (const t of e.tracks || []) this.live.push({ net: e.net, t });
      for (const v of e.vias || []) this.live.push({ net: e.net, v });
    } else if (e.status === "ripped") {
      this.live = this.live.filter((x) => x.net !== e.net);
    }
    if (e.total) {
      this.progress.style.display = "flex";
      clear(this.progress).append(h("span.spinner"), h("span", `Routing ${e.done || 0} / ${e.total} nets` + (e.status === "failed" ? ` · ${e.net.split("/").pop()} failed` : "")),
        h("div.progress", { style: { width: "90px" } }, h("i", { style: { width: `${Math.round(100 * (e.done || 0) / e.total)}%` } })));
    } else if (e.engine === "freerouting") {
      this.progress.style.display = "flex";
      clear(this.progress).append(h("span.spinner"), h("span", e.status === "done" ? "Freerouting done" : `Freerouting pass ${e.pass || "?"}: ${e.unrouted ?? "?"} unrouted`));
    }
    clearTimeout(this.progT);
    this.progT = setTimeout(() => { this.progress.style.display = "none"; }, 8000);
    this.dirty();
  }

  setAnnotations(items) { this.notes = items || []; this.dirty(); }
  focus(w) { this.highlight({ points: [{ x: w.x, y: w.y }], refs: w.ref ? [w.ref] : [] }); }

  pose(f) {
    const a = this.anim[f.ref];
    if (a) {
      const k = Math.min(1, Math.max(0, (performance.now() - a.t0) / a.ms));
      const e = ease(k);
      let dr = ((a.to.rot ?? a.from.rot) - a.from.rot) % 360;
      if (dr > 180) dr -= 360; if (dr < -180) dr += 360;
      return { x: a.from.x + ((a.to.x ?? a.from.x) - a.from.x) * e, y: a.from.y + ((a.to.y ?? a.from.y) - a.from.y) * e, rot: a.from.rot + dr * e };
    }
    return this.override[f.ref] || null;
  }

  // ------------------------------------------------------------------ the layers panel
  layerOn(l) {
    if (l === "F.Cu" || l === "B.Cu") return this.vis[l];
    return this.vis.inner;
  }

  renderLayers() {
    clear(this.layerBox);
    const collapsed = localStorage.getItem("tw.layers.collapsed") !== "0" || (this.autoFold && !this.forceOpen);
    const tabs = h("div.ltabs", [["layers", "Layers", "layers"], ["copper", "Copper", "cable"]].map(([v, t, ic]) =>
      h("button" + (this.panel === v ? ".on" : ""), { onclick: () => { if (collapsed) localStorage.setItem("tw.layers.collapsed", "0"); this.setPanel(v); },
        "data-tip": v === "copper" ? "Copper by net" : "Layers" }, icon(ic, 12), t)));
    this.layerBox.appendChild(h("div.lhead", tabs, h("button.tbtn", { style: { height: "22px", minWidth: "22px", padding: 0 },
      "data-tip": collapsed ? "Show the panel" : "Hide the panel", onclick: () => {
        if (this.autoFold) this.forceOpen = collapsed; else localStorage.setItem("tw.layers.collapsed", collapsed ? "0" : "1");
        this.renderLayers();
      } },
      icon(collapsed ? "chevron-down" : "chevron-up", 13))));
    this.layerBox.classList.toggle("wide", this.panel === "copper" && !collapsed);
    this.layerBox.style.width = collapsed ? "auto" : "";
    if (collapsed) return;
    if (this.panel === "copper") { this.copperPanel(); return; }
    const items = [["F.Cu", "Top copper", COL["F.Cu"]], ["B.Cu", "Bottom copper", COL["B.Cu"]]];
    if (this.copper.length > 2) items.push(["inner", "Inner copper", COL["In1.Cu"]]);
    items.push(["zones", "Pours and areas", [150, 150, 170]], ["vias", "Vias", COL.via], ["silk", "Silkscreen", COL.silkF], ["labels", "References", [200, 200, 200]],
      ["unrouted", "Unrouted", [255, 190, 90]], ["findings", "Check findings", [240, 101, 96]], ["notes", "Claude's notes", [255, 140, 70]],
      ["flags", "Resolved flags", [67, 194, 131]], ["courtyard", "Courtyards", [180, 90, 200]], ["fab", "Fab layer", [140, 150, 170]]);
    for (const [k, label, c] of items) {
      const el = h("div.lrow" + (this.vis[k] ? ".on" : ""), { onclick: () => {
        this.vis[k] = !this.vis[k]; el.classList.toggle("on", this.vis[k]); clear(eye).appendChild(icon(this.vis[k] ? "eye" : "eye-off", 13));
        if (k === "flags") this.flagLayer.render();
        this.dirty();
      } }, h("i.sw", { style: { background: rgba(c) } }), h("span", label));
      const eye = h("span.eye", icon(this.vis[k] ? "eye" : "eye-off", 13));
      el.appendChild(eye);
      this.layerBox.appendChild(el);
    }
  }

  copperPanel() {
    const box = this.layerBox, d = this.data;
    if (!d || !this.pours) return;
    const short = (n) => (n || "no net").split("/").pop();
    const col = (n) => rgba(this.netColor[n] || [120, 124, 134]);
    box.appendChild(h("div.lsec", "Layer"));
    box.appendChild(h("div.lchips", [null, ...this.copper].map((l) => h("button.lchip" + (this.solo === l ? ".on" : ""),
      { onclick: () => { this.solo = l; this.renderLayers(); this.dirty(); }, "data-tip": l ? `Only ${l}` : "Every copper layer" },
      l ? h("i", { style: { background: rgba(COL[l] || [160, 160, 160]) } }) : null, l ? l.replace(".Cu", "") : "All"))));
    if (this.spot) {
      const s = this.stats(this.spot);
      const rows = this.copper.filter((l) => s.tracks[l] || s.pour[l]).map((l) => h("div.srow3", h("span.k", l),
        h("span", [s.tracks[l] ? `${s.tracks[l].toFixed(1)} mm` : null, s.pour[l] ? `pour ${Math.round(s.pour[l])} mm²` : null].filter(Boolean).join(" · "))));
      box.appendChild(h("div.spotcard",
        h("div.row", h("i.sw", { style: { background: col(this.spot) } }), h("b.grow.ellipsis", short(this.spot)),
          h("button.tbtn", { style: { height: "22px", minWidth: "22px", padding: 0 }, "data-tip": "Clear the spotlight", onclick: () => this.setSpot(null) }, icon("x", 12))),
        this.netKindOf(this.spot) ? h("div.tiny.nkind", netKindText(this.netKindOf(this.spot), short)) : null,
        h("div.tiny", { style: { color: "var(--hud-muted)", margin: "3px 0 6px" } }, `${s.pads} pads on ${s.parts.size} parts · ${s.vias} vias`),
        rows.length ? rows : h("div.tiny", { style: { color: "var(--hud-muted)" } }, "No tracks or pours yet"),
        h("div.row", { style: { marginTop: "8px", gap: "4px" } },
          h("button.tbtn", { "data-tip": "Flag net", onclick: () => {
            const b = this.netBox(this.spot);
            if (b) this.flags.create({ x: b[0], y: b[1], region: b, nets: [this.spot] });
          } }, icon("flag", 13), h("span", "Flag")),
          h("button.tbtn", { "data-tip": "Ask Claude", onclick: () => this.ws.ask(`About net ${short(this.spot)}: `) }, icon("message-square", 13), h("span", "Ask")))));
    }
    const isl = this.islands.filter((i) => !this.solo || i.layer === this.solo);
    if (isl.length) {
      box.appendChild(h("div.lwarn", { onclick: () => this.nextIsland(isl), "data-tip": "Pour islands with no connection. Click to step through." },
        icon("triangle-alert", 13), h("span.grow", `${isl.length} unconnected pour island${isl.length === 1 ? "" : "s"}`),
        h("span.eye", { onclick: (e) => { e.stopPropagation(); this.showIslands = !this.showIslands; this.renderLayers(); this.dirty(); } }, icon(this.showIslands ? "eye" : "eye-off", 13))));
    }
    const pours = this.pours.filter((p) => !this.solo || p.layer === this.solo);
    box.appendChild(h("div.lsec", `Pours ${pours.length ? "(" + pours.length + ")" : ""}`));
    if (!pours.length) box.appendChild(h("div.lempty", this.solo ? `No pours on ${this.solo}` : "No copper pours"));
    for (const p of pours.slice(0, 40)) box.appendChild(h("div.lnrow" + (this.spot === p.net ? ".on" : ""),
      { onclick: () => { this.setSpot(p.net); this.flyTo(padBox(p.bbox, 1), 380, 0.85); }, "data-tip": `${p.name ? "Zone " + p.name + " · " : ""}priority ${p.pri}` },
      h("i.sw", { style: { background: col(p.net) } }), h("span.grow.ellipsis", short(p.net)), h("span.lk", p.layer.replace(".Cu", "")), h("span.lk", `${Math.round(p.area)} mm²`)));
    box.appendChild(h("div.lsec", "Nets"));
    const q = h("input", { placeholder: "Filter nets", value: this.netQ, spellcheck: false, oninput: () => { this.netQ = q.value; drawNets(); } });
    box.appendChild(h("div.lsearch", icon("search", 12), q));
    const exact = this.netKinds || {};
    const byShort = {};                                   // the board may still carry the schematic's older names
    for (const [n, v] of Object.entries(exact)) if (!(short(n) in byShort)) byShort[short(n)] = v;
    const kinds = new Proxy({}, { get: (_, n) => exact[n] || byShort[short(n)], has: (_, n) => !!(exact[n] || byShort[short(n)]) });
    const kindOf = (n) => (kinds[n] || {}).kind || "signal";
    const KIND_F = [["all", "All"], ["power", "Power"], ["pair", "Pairs"], ["clock", "Clocks"], ["signal", "Signals"]];
    const filt = h("div.lchips.nkinds", KIND_F.map(([k, t]) => h("button.lchip" + ((this.netKind || "all") === k ? ".on" : ""),
      { onclick: () => { this.netKind = k; this.renderLayers(); }, "data-tip": k === "power" ? "Supplies and ground" : k === "pair" ? "Differential pairs" :
        k === "clock" ? "Clocks and fast lines" : k === "signal" ? "Everything else" : "Every net" }, t)));
    if (Object.keys(exact).length) box.appendChild(filt);
    const list = h("div");
    box.appendChild(list);
    const counts = {};
    for (const f of this.fps) for (const p of f.pads) if (p.net) counts[p.net] = (counts[p.net] || 0) + 1;
    const nets = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    const keep = (n) => {
      const k = kindOf(n), want = this.netKind || "all";
      return want === "all" || k === want || (want === "power" && k === "ground") || (want === "clock" && k === "fast");
    };
    const tag = (n) => { const t = kinds[n]; return t && t.tag ? h("span.ntag." + t.kind, { "data-tip": netKindText(t, short) }, t.tag) : null; };
    const drawNets = () => {
      clear(list);
      const f = this.netQ.trim().toLowerCase();
      const shown = nets.filter((n) => (!f || short(n).toLowerCase().includes(f)) && keep(n));
      for (const n of shown.slice(0, 80)) list.appendChild(h("div.lnrow" + (this.spot === n ? ".on" : ""), { onclick: () => this.setSpot(this.spot === n ? null : n, true) },
        h("i.sw", { style: { background: col(n) } }), h("span.grow.ellipsis", short(n)), tag(n), h("span.lk", `${counts[n]} pad${counts[n] === 1 ? "" : "s"}`)));
      if (shown.length > 80) list.appendChild(h("div.lempty", `${shown.length - 80} more. Filter to narrow the list.`));
      if (!shown.length) list.appendChild(h("div.lempty", "No net matches"));
    };
    drawNets();
  }

  netKindOf(net) {
    const k = this.netKinds || {};
    if (k[net]) return k[net];
    const s = (net || "").split("/").pop();
    for (const [n, v] of Object.entries(k)) if (n.split("/").pop() === s) return v;
    return null;
  }

  netBox(net) {
    const pts = [];
    for (const t of this.data.tracks) if (t[6] === net) pts.push([t[0], t[1]], [t[2], t[3]]);
    for (const p of this.pours) if (p.net === net) pts.push([p.bbox[0], p.bbox[1]], [p.bbox[2], p.bbox[3]]);
    for (const f of this.fps) for (const p of f.pads) if (p.net === net) pts.push([p.x, p.y]);
    if (!pts.length) return null;
    return padBox([Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])), Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))], 0.5);
  }

  nextIsland(list) {
    if (!list.length) return;
    this.islandI = ((this.islandI ?? -1) + 1) % list.length;
    const i = list[this.islandI];
    this.showIslands = true;
    this.flyTo(padBox(i.bbox, 2), 380, 0.6);
    this.flash.textContent = `Island ${this.islandI + 1} of ${list.length}: ${i.net.split("/").pop()} on ${i.layer}, ${i.area.toFixed(1)} mm², unconnected`;
    this.flash.style.display = "block";
    clearTimeout(this.flashT); this.flashT = setTimeout(() => this.flash.style.display = "none", 6000);
  }

  netAt(x, y) {
    const f = this.pickFp(x, y);
    if (f) { const p = this.pickPad(f, x, y); if (p && p.net) return p.net; }
    const t = this.pickTrack(x, y);
    if (t && t.net) return t.net;
    const p = this.pickPour(x, y);
    return p ? p.net : null;
  }

  // copper coloured by net, one layer or all, a net in the spotlight
  drawCopper(c, px) {
    const d = this.data, spot = this.spot;
    const layers = this.visibleCopper();
    const vis = new Set(layers), top = layers[layers.length - 1];
    const col = (n) => this.netColor[n] || [120, 124, 134];
    const a = (n, base) => spot ? (n === spot ? Math.min(1, base * 1.7 + 0.12) : base * 0.13) : base;
    for (const l of layers) {
      const front = l === top || !!this.solo;
      for (const [net, p] of Object.entries(this.zoneNetPaths[l] || {})) { c.fillStyle = rgba(col(net), a(net, front ? 0.36 : 0.15)); c.fill(p); }
      c.lineCap = "round"; c.lineJoin = "round";
      for (const tp of this.trackNetPaths) {
        if (tp.l !== l) continue;
        c.strokeStyle = rgba(col(tp.net), a(tp.net, front ? 0.95 : 0.45));
        c.lineWidth = tp.w;
        c.stroke(tp.p);
      }
    }
    for (const f of this.fps) this.withPose(c, f, () => {
      for (const p of f.pads) {
        const all = p.l.includes("*.Cu");
        if (!(all ? vis.size : p.l.some((x) => vis.has(x)))) continue;
        const onTop = all || p.l.includes(top);
        c.fillStyle = p.net ? rgba(col(p.net), a(p.net, onTop ? 0.95 : 0.45)) : "rgba(150,152,160,.45)";
        for (const pl of p.p) { c.beginPath(); polyCtx(c, pl); c.fill(); }
      }
    });
    if (this.vis.vias) {
      for (const v of d.vias) { c.fillStyle = rgba(col(v[4]), a(v[4], 0.95)); c.beginPath(); c.arc(v[0], v[1], v[2] / 2, 0, Math.PI * 2); c.fill(); }
      c.fillStyle = "#0b0d11";
      for (const v of d.vias) { c.beginPath(); c.arc(v[0], v[1], v[3] / 2, 0, Math.PI * 2); c.fill(); }
    }
    // the spotlit net's zone outlines, and the islands
    c.setLineDash([5 * px, 4 * px]);
    c.lineWidth = 1.2 * px;
    for (const p of this.pours) if (vis.has(p.layer) && (p.net === spot || this.solo)) {
      c.strokeStyle = rgba(col(p.net), p.net === spot ? 0.95 : 0.4);
      for (const o of p.outline) { c.beginPath(); polyCtx(c, o); c.stroke(); }
    }
    c.setLineDash([]);
    if (this.showIslands && this.islands.length) {
      if (!this.hatch) this.hatch = hatchPattern(c);
      this.hatch.setTransform(new DOMMatrix().scale(px));
      for (const i of this.islands) {
        if (!vis.has(i.layer)) continue;
        c.beginPath(); polyCtx(c, i.poly);
        c.fillStyle = this.hatch; c.fill();
        c.strokeStyle = "rgba(240,101,96,.95)"; c.lineWidth = 2 * px; c.stroke();
      }
    }
  }

  // net names on the pours big enough to read, in screen space
  drawPourLabels(c) {
    const S = this.scale, vis = new Set(this.visibleCopper());
    c.textAlign = "center"; c.textBaseline = "middle";
    c.font = "600 11px ui-sans-serif, -apple-system, sans-serif";
    const seen = new Set();
    for (const p of this.pours) {
      if (!vis.has(p.layer) || (this.spot && p.net !== this.spot)) continue;
      for (const pl of p.polys) {
        const bb = growBox([Infinity, Infinity, -Infinity, -Infinity], pl);
        if ((bb[2] - bb[0]) * S < 70 || (bb[3] - bb[1]) * S < 22) continue;
        let cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2;
        if (!inPoly(cx, cy, pl)) continue;
        const key = p.net + "|" + Math.round(cx * S / 80) + "|" + Math.round(cy * S / 40);
        if (seen.has(key)) continue;
        seen.add(key);
        const label = p.net.split("/").pop() + (this.solo ? "" : " · " + p.layer.replace(".Cu", ""));
        const sx = cx * S + this.ox, sy = cy * S + this.oy;
        const w = c.measureText(label).width + 12;
        c.fillStyle = "rgba(13,14,17,.78)"; roundRect(c, sx - w / 2, sy - 9, w, 18, 5); c.fill();
        c.fillStyle = rgba(this.netColor[p.net] || [200, 200, 200]); c.fillText(label, sx, sy);
      }
    }
  }

  setSide() {
    this.side = this.side === "F" ? "B" : "F";
    this.sideBtn.lastChild.textContent = this.side === "F" ? "Top" : "Bottom";
    this.sideBtn.classList.toggle("on", this.side === "B");
    this.dirty();
  }

  // ------------------------------------------------------------------ drawing
  dirty() { if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = null; this.draw(); }); }

  draw() {
    const c = this.ctx, d = this.data;
    const dpr = this.dpr || 1;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = "#0d0e11";
    c.fillRect(0, 0, this.canvas.width, this.canvas.height);
    if (!d) return;
    const now = performance.now();
    let animating = false;
    for (const [ref, a] of Object.entries(this.anim)) {
      if (now - a.t0 < a.ms) animating = true;
      else {
        if (a.keep) this.override[ref] = { x: a.to.x, y: a.to.y, rot: a.to.rot };
        delete this.anim[ref];
      }
    }
    if (!animating && this.pendingReload && !Object.keys(this.anim).length && !this.drawing) { this.pendingReload = false; this.load(); }
    const S = this.scale;
    c.setTransform(S * dpr, 0, 0, S * dpr, this.ox * dpr, this.oy * dpr);
    const px = 1 / S;
    // board body
    c.fillStyle = this.panel === "copper" ? "#141a17" : "#16241b";
    c.fill(this.boardFill, "evenodd");
    const copperMode = this.panel === "copper";
    if (copperMode) this.drawCopper(c, px);
    const order = copperMode ? [] : this.side === "F" ? [...this.copper].reverse() : [...this.copper];
    const top = order[order.length - 1];
    for (const l of order) {
      if (!this.layerOn(l)) continue;
      const col = COL[l] || [160, 160, 160];
      const front = l === top;
      const dim = front ? 1 : 0.55;
      if (this.vis.zones && this.zonePaths[l]) { c.fillStyle = rgba(col, 0.2 * dim + (front ? 0.08 : 0)); c.fill(this.zonePaths[l]); }
      c.lineCap = "round"; c.lineJoin = "round";
      for (const tp of this.trackPaths) {
        if (tp.l !== l) continue;
        c.strokeStyle = rgba(col, front ? 0.95 : 0.6);
        c.lineWidth = tp.w;
        c.stroke(tp.p);
      }
      // pads of this side, drawn with its layer
      if (l === "F.Cu" || l === "B.Cu") {
        const onF = l === "F.Cu";
        for (const f of this.fps) this.drawFpPads(c, f, onF, front ? 1 : 0.6);
      }
    }
    // vias and holes
    if (this.vis.vias && !copperMode) {
      c.fillStyle = rgba(COL.via, 0.95);
      for (const v of d.vias) { c.beginPath(); c.arc(v[0], v[1], v[2] / 2, 0, Math.PI * 2); c.fill(); }
      c.fillStyle = "#0b0d11";
      for (const v of d.vias) { c.beginPath(); c.arc(v[0], v[1], v[3] / 2, 0, Math.PI * 2); c.fill(); }
    }
    c.fillStyle = "#0b0d11";
    for (const f of this.fps) this.withPose(c, f, () => { for (const [x, y, r] of f.holes) { c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill(); } });
    // rule areas
    if (this.vis.zones) {
      c.setLineDash([4 * px, 3 * px]);
      c.lineWidth = 1.2 * px;
      for (const r of this.rulePaths) { c.strokeStyle = r.z.name.startsWith("TW neck") ? "rgba(120,160,255,.35)" : "rgba(255,110,110,.7)"; c.stroke(r.p); }
      c.setLineDash([]);
    }
    // live routing overlay (glow)
    if (this.live.length) {
      c.lineCap = "round";
      for (const it of this.live) {
        if (it.t) {
          c.strokeStyle = it.t.layer === "B.Cu" ? "rgba(130,180,255,.95)" : "rgba(255,150,120,.95)";
          c.lineWidth = Math.max(it.t.w, 2 * px);
          c.beginPath(); c.moveTo(it.t.a[0], it.t.a[1]); c.lineTo(it.t.b[0], it.t.b[1]); c.stroke();
        } else if (it.v) {
          c.fillStyle = "rgba(255,255,255,.9)"; c.beginPath(); c.arc(it.v.x, it.v.y, it.v.d / 2, 0, Math.PI * 2); c.fill();
        }
      }
    }
    // silk, fab, courtyards (faint over the copper inspector)
    const sk = copperMode ? 0.3 : 1;
    for (const f of this.fps) this.withPose(c, f, () => {
      if (this.vis.silk) {
        c.strokeStyle = rgba(COL.silkF, (this.side === "F" ? 0.85 : 0.35) * sk); c.lineWidth = 0.12; c.stroke(f.silkF);
        c.strokeStyle = rgba(COL.silkB, (this.side === "B" ? 0.85 : 0.3) * sk); c.stroke(f.silkB);
      }
      if (this.vis.fab) { c.strokeStyle = "rgba(140,150,170,.7)"; c.lineWidth = 0.08; c.stroke(f.fabP); }
      if (this.vis.courtyard) { c.strokeStyle = "rgba(190,100,210,.7)"; c.lineWidth = 0.05; c.stroke(f.cyP); }
    });
    if (this.vis.silk) {
      c.strokeStyle = rgba(COL.silkF, 0.85 * sk); c.lineWidth = 0.12; c.stroke(this.boardSilk.F);
      c.strokeStyle = rgba(COL.silkB, 0.35 * sk); c.stroke(this.boardSilk.B);
      if (!copperMode) this.drawSilkText(c);
    }
    // edge
    c.strokeStyle = rgba(COL.edge, 0.95); c.lineWidth = Math.max(0.15, 1.5 * px); c.stroke(this.edge);
    // unrouted
    if (this.vis.unrouted && d.unrouted) {
      c.strokeStyle = "rgba(255,200,100,.85)"; c.lineWidth = 1.2 * px; c.setLineDash([3 * px, 3 * px]);
      for (const u of d.unrouted) { c.beginPath(); c.moveTo(u[0], u[1]); c.lineTo(u[2], u[3]); c.stroke(); }
      c.setLineDash([]);
    }
    // highlight: dim everything, then draw the highlighted copper bright
    if (this.hl && (this.hl.refs.size || this.hl.nets.size)) {
      const age = (now - this.hl.t0) / 1000;
      if (age < 30) {
        c.setTransform(1, 0, 0, 1, 0, 0);
        c.fillStyle = `rgba(5,7,10,${0.5 * Math.min(1, age * 4)})`;
        c.fillRect(0, 0, this.canvas.width, this.canvas.height);
        c.setTransform(S * dpr, 0, 0, S * dpr, this.ox * dpr, this.oy * dpr);
        const pulse = 0.65 + 0.35 * Math.sin(age * 5);
        for (const n of this.hl.nets) {
          c.strokeStyle = rgba(COL.hl, pulse);
          for (const t of d.tracks) if (t[6] === n) { c.lineWidth = Math.max(t[4], 2 * px); c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); }
          c.fillStyle = rgba(COL.hl, pulse);
          for (const v of d.vias) if (v[4] === n) { c.beginPath(); c.arc(v[0], v[1], v[2] / 2, 0, Math.PI * 2); c.fill(); }
          for (const f of this.fps) this.withPose(c, f, () => { for (const p of f.pads) if (p.net === n) for (const pl of p.p) { c.beginPath(); polyCtx(c, pl); c.fill(); } });
        }
        for (const r of this.hl.refs) {
          const f = this.byRef[r];
          if (!f) continue;
          this.withPose(c, f, () => {
            c.fillStyle = rgba(COL.hl, 0.9 * pulse + 0.1); c.fill(f.side === "F" ? f.padF : f.padB);
            c.strokeStyle = rgba(COL.hl, pulse); c.lineWidth = 2.2 * px;
            const b = f.bbox; c.strokeRect(b[0] - 0.4, b[1] - 0.4, b[2] - b[0] + 0.8, b[3] - b[1] + 0.8);
          });
        }
        animating = true;
      }
    }
    // selection and KiCad selection
    c.lineWidth = 2 * px;
    for (const r of this.sel) { const f = this.byRef[r]; if (f) this.withPose(c, f, () => { c.strokeStyle = rgba(COL.sel, 1); const b = f.bbox; c.strokeRect(b[0] - 0.25, b[1] - 0.25, b[2] - b[0] + 0.5, b[3] - b[1] + 0.5); }); }
    if (this.selNet) {
      c.strokeStyle = rgba(COL.sel, 0.95);
      for (const t of d.tracks) if (t[6] === this.selNet) { c.lineWidth = Math.max(t[4], 2 * px); c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); }
    }
    c.setLineDash([4 * px, 3 * px]);
    for (const r of this.kicadSel) { const f = this.byRef[r]; if (f) this.withPose(c, f, () => { c.strokeStyle = rgba(COL.kicad, 1); const b = f.bbox; c.strokeRect(b[0] - 0.5, b[1] - 0.5, b[2] - b[0] + 1, b[3] - b[1] + 1); }); }
    c.setLineDash([]);
    // screen-space overlays: labels, findings, notes, highlight points, the measurement
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (copperMode) this.drawPourLabels(c);
    if (this.vis.labels && !(copperMode && this.spot)) this.drawLabels(c);
    if (this.vis.findings) for (const f of this.findings) this.marker(c, f.x, f.y, f.sev === "error" ? [240, 101, 96] : f.sev === "warning" ? [232, 184, 74] : [99, 164, 248], 5);
    if (this.vis.notes) for (const n of this.notes) this.note(c, n);
    if (this.hl) for (const p of this.hl.points) { const age = (now - this.hl.t0) / 1000; if (age < 12) { this.marker(c, p.x, p.y, COL.hl, 7 + 4 * Math.abs(Math.sin(age * 4))); animating = true; } }
    if (this.measure && !this.drawing) this.drawMeasure(c);
    if (!this.drawing) this.flagLayer.update();
    if (animating && !this.drawing) this.dirty();
  }

  drawMeasure(c) {
    const m = this.measure;
    const [ax, ay] = this.toScreen(...m.a), [bx, by] = this.toScreen(...m.b);
    c.strokeStyle = "rgba(255,255,255,.95)"; c.lineWidth = 1.5; c.setLineDash([5, 4]);
    c.beginPath(); c.moveTo(ax, ay); c.lineTo(bx, by); c.stroke(); c.setLineDash([]);
    c.strokeStyle = "rgba(255,255,255,.35)"; c.lineWidth = 1;
    c.beginPath(); c.moveTo(ax, ay); c.lineTo(bx, ay); c.lineTo(bx, by); c.stroke();
    for (const [x, y] of [[ax, ay], [bx, by]]) { c.fillStyle = "#fff"; c.beginPath(); c.arc(x, y, 3.5, 0, Math.PI * 2); c.fill(); }
    const dx = m.b[0] - m.a[0], dy = m.b[1] - m.a[1], d = Math.hypot(dx, dy);
    this.measureEl.style.display = d > 0 ? "" : "none";
    this.measureEl.innerHTML = `${d.toFixed(3)} mm <span class="k">· dx ${Math.abs(dx).toFixed(2)} · dy ${Math.abs(dy).toFixed(2)}${d > 0 ? ` · ${(Math.atan2(-dy, dx) * 180 / Math.PI).toFixed(1)}°` : ""} · ${(d / 0.0254).toFixed(1)} mil</span>`;
    this.measureEl.style.left = Math.min((ax + bx) / 2 + 12, this.w - 280) + "px";
    this.measureEl.style.top = ((ay + by) / 2 - 30) + "px";
  }

  withPose(c, f, fn) {
    const p = this.pose(f);
    if (!p) { fn(); return; }
    c.save();
    c.translate(p.x, p.y);
    c.rotate(-(p.rot - f.a) * Math.PI / 180);
    c.translate(-f.x, -f.y);
    fn();
    c.restore();
  }

  drawFpPads(c, f, onF, alpha) {
    this.withPose(c, f, () => {
      c.fillStyle = rgba(onF ? COL.pad : COL.padB, alpha);
      c.fill(onF ? f.padF : f.padB);
    });
  }

  drawSilkText(c) {
    const S = this.scale;
    for (const f of this.fps) {
      for (const t of f.t) {
        if (!(t.l === "F.SilkS" || t.l === "F.Silkscreen" || t.l === "B.SilkS" || t.l === "B.Silkscreen")) continue;
        if (t.h * S < 5) continue;
        const back = t.l.startsWith("B");
        this.withPose(c, f, () => {
          c.save();
          c.translate(t.x, t.y);
          c.rotate(-t.a * Math.PI / 180);
          if (t.m) c.scale(-1, 1);
          c.fillStyle = rgba(back ? COL.silkB : COL.silkF, back === (this.side === "B") ? 0.9 : 0.35);
          c.font = `${t.h * 1.25}px ui-sans-serif, -apple-system, sans-serif`;
          c.textAlign = t.j.includes("left") ? "left" : t.j.includes("right") ? "right" : "center";
          c.textBaseline = "middle";
          c.fillText(t.s, 0, 0);
          c.restore();
        });
      }
    }
  }

  // Reference labels where KiCad's silk does not show them: the selection first, then the bigger parts;
  // a label that would run into one already drawn is left out (zoom in and it appears).
  drawLabels(c) {
    const S = this.scale;
    c.textAlign = "center"; c.textBaseline = "middle";
    const items = [];
    for (const f of this.fps) {
      if (!f.pads.length) continue;
      const b = f.bbox;
      const wpx = (b[2] - b[0]) * S, hpx = (b[3] - b[1]) * S;
      const on = this.sel.has(f.ref);
      if (Math.max(wpx, hpx) < 14 && !on) continue;
      if (!on && this.vis.silk && f.t.some((t) => t.k === "reference" && t.h * S >= 5 && /SilkS|Silkscreen/.test(t.l))) continue;   // silk shows it
      const p = this.pose(f);
      let cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
      if (p) { const [x, y] = fwdPose(p, f, cx, cy); cx = x; cy = y; }
      const size = Math.max(9, Math.min(15, Math.min(wpx, hpx) * 0.45));
      const own = f.side === this.side;
      items.push({ f, on, own, size, sx: cx * S + this.ox, sy: cy * S + this.oy, pri: (on ? 1e12 : 0) + (own ? 1e6 : 0) + wpx * hpx });
    }
    items.sort((a, b) => b.pri - a.pri);
    const placed = [];
    const W = this.w || 1e5, H = this.h || 1e5;
    for (const it of items) {
      if (it.sx < -40 || it.sy < -20 || it.sx > W + 40 || it.sy > H + 20) continue;
      c.font = `600 ${it.size}px ui-sans-serif, -apple-system, sans-serif`;
      const w = c.measureText(it.f.ref).width + 5, hh = it.size + 3;
      const box = [it.sx - w / 2, it.sy - hh / 2, it.sx + w / 2, it.sy + hh / 2];
      if (!it.on && placed.some((q) => box[0] < q[2] && box[2] > q[0] && box[1] < q[3] && box[3] > q[1])) continue;
      placed.push(box);
      c.lineWidth = 3; c.strokeStyle = "rgba(0,0,0,.8)"; c.strokeText(it.f.ref, it.sx, it.sy);
      c.fillStyle = it.on ? "#9ec2ff" : it.own ? "rgba(245,245,245,.95)" : "rgba(200,200,200,.5)";
      c.fillText(it.f.ref, it.sx, it.sy);
    }
  }

  marker(c, x, y, col, r) {
    const sx = x * this.scale + this.ox, sy = y * this.scale + this.oy;
    c.beginPath(); c.moveTo(sx, sy - r); c.lineTo(sx + r, sy); c.lineTo(sx, sy + r); c.lineTo(sx - r, sy); c.closePath();
    c.fillStyle = rgba(col, 0.95); c.fill(); c.lineWidth = 1.5; c.strokeStyle = "rgba(0,0,0,.7)"; c.stroke();
  }

  note(c, n) {
    const sx = n.x * this.scale + this.ox, sy = n.y * this.scale + this.oy;
    const col = n.kind === "issue" ? [240, 101, 96] : n.kind === "proposal" ? [91, 155, 248] : [255, 150, 70];
    c.beginPath(); c.arc(sx, sy, 6, 0, Math.PI * 2); c.fillStyle = rgba(col, 1); c.fill(); c.lineWidth = 2; c.strokeStyle = "#fff"; c.stroke();
    if (!n.text) return;
    c.font = "12px ui-sans-serif, -apple-system, sans-serif"; c.textAlign = "left"; c.textBaseline = "middle";
    const w = Math.min(260, c.measureText(n.text).width + 14);
    c.fillStyle = "rgba(20,21,25,.94)"; c.strokeStyle = rgba(col, 0.9); c.lineWidth = 1;
    roundRect(c, sx + 10, sy - 11, w, 22, 6); c.fill(); c.stroke();
    c.fillStyle = "#eef2f8"; c.fillText(n.text.length > 40 ? n.text.slice(0, 40) + "..." : n.text, sx + 17, sy);
  }
}

// ------------------------------------------------------------------ helpers
function esc(s) { return String(s ?? "").replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch])); }
function poly(p, pts) { if (!pts.length) return; p.moveTo(pts[0][0], pts[0][1]); for (let i = 1; i < pts.length; i++) p.lineTo(pts[i][0], pts[i][1]); p.closePath(); }
function polyCtx(c, pts) { c.moveTo(pts[0][0], pts[0][1]); for (let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]); c.closePath(); }
function shapePath(p, s) {
  if (s.t === "circle") { p.moveTo(s.c[0] + s.r, s.c[1]); p.arc(s.c[0], s.c[1], s.r, 0, Math.PI * 2); return; }
  const pts = s.p || [];
  if (!pts.length) return;
  p.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) p.lineTo(pts[i][0], pts[i][1]);
  if (s.t === "rect" || s.t === "poly") p.closePath();
}
function inPoly(x, y, pts) {
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < xi + (y - yi) * (xj - xi) / (yj - yi)) c = !c;
  }
  return c;
}
function inBox(x, y, b) { return x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3]; }
function polyArea(pts) { let a = 0; for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1]); return a / 2; }
function growBox(b, pts) { for (const [x, y] of pts) { if (x < b[0]) b[0] = x; if (y < b[1]) b[1] = y; if (x > b[2]) b[2] = x; if (y > b[3]) b[3] = y; } return b; }
function layerIndex(copper, l) { const i = copper.indexOf(l); return i < 0 ? 99 : i; }

// A colour per net: ground a blue-grey, supplies warm, the rest spread round the wheel (golden angle).
export function netColors(nets) {
  const out = {}, rest = [];
  const hsl = (hh, s, l) => {
    s /= 100; l /= 100;
    const k = (n) => (n + hh / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [Math.round(255 * f(0)), Math.round(255 * f(8)), Math.round(255 * f(4))];
  };
  let pw = 0;
  for (const n of nets) {
    const s = (n || "").split("/").pop().toUpperCase();
    if (!n) continue;
    if (/^(GND|AGND|DGND|PGND|SGND|GNDA|GNDD|VSS|0V|EARTH|CHASSIS)/.test(s)) out[n] = [104, 138, 196];
    else if (/^(\+|V(CC|DD|IN|BUS|BAT|SYS|MAIN|SUP)|[0-9.]+V[0-9]*$|PWR|VIO)/.test(s)) out[n] = hsl([6, 22, 38, 350, 14, 30][pw++ % 6], 78, 58);
    else rest.push(n);
  }
  rest.forEach((n, i) => { out[n] = hsl((i * 137.508 + 170) % 360, 62, 60); });
  return out;
}

// Pour fills with nothing of their net on them (no pad, via or track end): dead copper, or a pour
// cut off from its net. A pad joins through its thermal spokes (the fill reaches into the pad) or solidly.
function findIslands(pours, fps, vias, tracks) {
  const anchors = {};                                  // net|layer -> [[x0, y0, x1, y1, cx, cy]]
  const add = (net, layer, b, cx, cy) => { const k = net + "|" + layer; (anchors[k] = anchors[k] || []).push([...b, cx, cy]); };
  const layersOf = new Set(pours.map((p) => p.layer));
  for (const f of fps) for (const p of f.pads) {
    if (!p.net) continue;
    const b = [Infinity, Infinity, -Infinity, -Infinity];
    for (const pl of p.p) growBox(b, pl);
    if (!isFinite(b[0])) continue;
    for (const l of layersOf) if (p.l.includes(l) || p.l.includes("*.Cu") || (p.d && p.l.some((x) => x.endsWith(".Cu")))) add(p.net, l, padBox(b, 0.08), p.x, p.y);
  }
  for (const v of vias) {                             // counted on every layer (a blind via's span is not checked)
    if (!v[4]) continue;
    const r = v[2] / 2 + 0.08;
    for (const l of layersOf) add(v[4], l, [v[0] - r, v[1] - r, v[0] + r, v[1] + r], v[0], v[1]);
  }
  for (const t of tracks) {
    if (!t[6]) continue;
    for (const [x, y] of [[t[0], t[1]], [t[2], t[3]]]) add(t[6], t[5], [x - t[4] / 2, y - t[4] / 2, x + t[4] / 2, y + t[4] / 2], x, y);
  }
  const out = [];
  for (const p of pours) {
    const as = anchors[p.net + "|" + p.layer] || [];
    for (const pl of p.polys) {
      const bb = growBox([Infinity, Infinity, -Infinity, -Infinity], pl);
      const near = as.filter((a) => a[2] >= bb[0] && a[0] <= bb[2] && a[3] >= bb[1] && a[1] <= bb[3]);
      let hit = near.some((a) => inPoly(a[4], a[5], pl));                       // a solid connection, a track end in the fill
      if (!hit && near.length) hit = pl.some(([x, y]) => near.some((a) => x >= a[0] && x <= a[2] && y >= a[1] && y <= a[3]));   // thermal spokes
      if (!hit) out.push({ net: p.net, layer: p.layer, poly: pl, bbox: bb, area: Math.abs(polyArea(pl)), name: p.name });
    }
  }
  return out.filter((i) => i.area > 0.05);
}
function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
function padBox(b, m) { return [b[0] - m, b[1] - m, b[2] + m, b[3] + m]; }
function fwdPose(p, f, x, y) {
  const a = -(p.rot - f.a) * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a);
  const dx = x - f.x, dy = y - f.y;
  return [p.x + dx * cs - dy * sn, p.y + dx * sn + dy * cs];
}
function invPose(p, f, x, y) {
  const a = (p.rot - f.a) * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a);
  const dx = x - p.x, dy = y - p.y;
  return [f.x + dx * cs - dy * sn, f.y + dx * sn + dy * cs];
}
function hatchPattern(c) {
  const t = document.createElement("canvas");
  t.width = t.height = 10;
  const g = t.getContext("2d");
  g.strokeStyle = "rgba(240,101,96,.8)"; g.lineWidth = 2;
  g.beginPath(); g.moveTo(-2, 12); g.lineTo(12, -2); g.moveTo(-2, 2); g.lineTo(2, -2); g.moveTo(8, 12); g.lineTo(12, 8); g.stroke();
  return c.createPattern(t, "repeat");
}
function roundRect(c, x, y, w, hh, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r); c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
