// The guided start's live canvas, beside the chat while Claude runs the intake: the requirements, a block
// diagram, the connectors on a sketch of the board with their pinouts, and a live BOM with JLC stock and
// price. Claude fills it with its canvas tool; when the intake is done a Start card appears, and Start
// opens the full workspace.
import { h, clear, api, toast, btn, confirmDialog } from "./util.js";
import { icon } from "./icons.js";
import { diagramSVG } from "./blockdiagram.js";
import { limitsPanel, limitsSummary } from "./limits.js";
import { FloorplanView } from "./floorplan.js";

const enc = encodeURIComponent;

// Text widths in the diagram's own fonts, for its layout.
let mctx, fonts;
function measure(s, font) {
  if (!mctx) {
    mctx = document.createElement("canvas").getContext("2d");
    const cs = getComputedStyle(document.documentElement);
    const sans = cs.getPropertyValue("--font") || "sans-serif", mono = cs.getPropertyValue("--mono") || "monospace";
    fonts = { t: `600 12.5px ${sans}`, n: `11px ${sans}`, l: `10.5px ${mono}` };
  }
  mctx.font = fonts[font] || fonts.l;
  return mctx.measureText(String(s)).width;
}

export class GuidedCanvas {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.el = h("div.gd");
    this.cv = {};
    this.seen = new Set();
    new ResizeObserver(() => { this.drawDiagram(); if (this.fpView && this.el.clientWidth) this.fpView.resize(this.el.clientWidth - 58); }).observe(this.el);
    this.load();
  }

  async load() {
    try { this.set(await api(`/api/projects/${enc(this.pid)}/canvas`)); } catch (e) { /* drawn when the first update arrives */ }
  }

  set(cv) {
    this.cv = { ...this.cv, ...cv };
    this.render();
  }

  get hasContent() {
    const c = this.cv;
    return !!((c.requirements && c.requirements.items && c.requirements.items.length) || (c.diagram && c.diagram.blocks && c.diagram.blocks.length) ||
      (c.connectors && c.connectors.items && c.connectors.items.length) || (c.parts && c.parts.items && c.parts.items.length) ||
      (c.floorplan && c.floorplan.board) || c.phase === "ready");
  }

  render() {
    // the cards are drawn again on every change (a drop on the floorplan too): the page keeps its place
    const sc = this.el.closest(".gdpane") || this.el.parentElement, top = sc ? sc.scrollTop : 0;
    const fpFocus = !!(this.fpView && this.fpView.el.contains(document.activeElement));     // and the floorplan its keys
    const c = this.cv, el = clear(this.el);
    const cards = [];
    if (c.feasibility && c.feasibility.length) cards.push(["feasibility", this.feasCard(c.feasibility)]);
    if (c.phase === "ready" && c.plan) cards.push(["plan", this.startCard(c.plan)]);
    if (c.requirements && c.requirements.items && c.requirements.items.length) cards.push(["requirements", this.reqCard(c.requirements)]);
    if (c.diagram && c.diagram.blocks && c.diagram.blocks.length) cards.push(["diagram", this.diagramCard(c.diagram)]);
    if (c.floorplan && c.floorplan.board) cards.push(["floorplan", this.floorplanCard(c.floorplan)]);
    else this.fpView = null;
    if (c.connectors && c.connectors.items && c.connectors.items.length) cards.push(["connectors", this.connCard(c.connectors, !!(c.floorplan && c.floorplan.board))]);
    if (c.parts && c.parts.items && c.parts.items.length) cards.push(["parts", this.partsCard(c.parts)]);
    for (const [k, card] of cards) {
      if (!this.seen.has(k)) { card.classList.add("new"); this.seen.add(k); }
      el.appendChild(card);
    }
    if (sc) sc.scrollTop = top;
    if (fpFocus && this.fpView && this.fpView.el.isConnected) this.fpView.el.focus({ preventScroll: true });
    this.ws.guidedChanged && this.ws.guidedChanged(this.hasContent, c.phase);
  }

  card(ic, title, sub, ...body) {
    return h("section.gd-card", h("div.gd-h", h("span.gd-hi", icon(ic, 15)), h("b", title), sub ? h("span.gd-sub", sub) : null), ...body);
  }

  // ------------------------------------------------------------------ what cannot work, or is tight, as set
  feasCard(list) {
    const bad = list.filter((i) => i.level === "impossible"), tight = list.filter((i) => i.level !== "impossible");
    const row = (i) => h("div.gd-fz." + (i.level === "impossible" ? "bad" : "tight"),
      icon(i.level === "impossible" ? "circle-x" : "triangle-alert", 14),
      h("div", h("b", i.what), h("div.gd-fzw", i.why), h("div.gd-fzf", i.fix)));
    return h("section.gd-card.gd-feas" + (bad.length ? ".bad" : ""),
      h("div.gd-h", h("span.gd-hi" + (bad.length ? ".bad" : ".warn"), icon(bad.length ? "circle-x" : "triangle-alert", 15)),
        h("b", bad.length ? "Can't work as set" : "Tight"),
        h("span.gd-sub", [bad.length ? `${bad.length} to fix` : "", tight.length ? `${tight.length} tight` : ""].filter(Boolean).join(" · "))),
      ...bad.map(row), ...tight.map(row));
  }

  // ------------------------------------------------------------------ start
  startCard(plan) {
    const go = btn("play", "Start design", { onclick: async () => {
      const bad = (this.cv.feasibility || []).filter((i) => i.level === "impossible");
      if (bad.length && !await confirmDialog({ title: "Start with this unresolved?", ok: "Start anyway",
        text: bad.map((i) => `${i.what}: ${i.fix}`).join("\n") })) return;
      go.disabled = true; go.classList.add("busy");
      try { await api(`/api/projects/${enc(this.pid)}/start`, { body: {} }); }
      catch (e) { toast(e.message, "error"); go.disabled = false; go.classList.remove("busy"); }
    } }, "primary");
    const auto = this.cv.run_mode !== "check_in";
    return h("section.gd-card.gd-start", h("div.gd-h", h("span.gd-hi.ok", icon("circle-check", 15)), h("b", "Ready to design")),
      plan.summary ? h("p.gd-sum", plan.summary) : null,
      plan.steps && plan.steps.length ? h("ol.gd-plan", plan.steps.map((s) => h("li", s))) : null,
      h("div.gd-go", go, h("span.gd-note", auto ? "Claude works through it on its own. Keep chatting to change the plan first."
        : "Claude checks in after each stage. Keep chatting to change the plan first.")));
  }

  // ------------------------------------------------------------------ requirements
  reqCard(r) {
    // Advanced: preset limits, all Claude's to decide until you set one (kept open once opened)
    const sum = h("span.gd-advn", limitsSummary(this.cv.constraints));
    const adv = h("details.gd-adv" + (this.advOpen ? "" : ""), { open: !!this.advOpen, ontoggle: async (e) => {
      this.advOpen = e.target.open;
      if (e.target.open && !body.firstChild) {
        try { body.appendChild(await limitsPanel(this.pid, (v) => { this.cv.constraints = v; sum.textContent = limitsSummary(v); })); }
        catch (er) { body.appendChild(h("div.small.muted", er.message)); }
      }
    } }, h("summary", h("span", "Advanced"), sum), h("div.gd-advb"));
    const body = adv.lastChild;
    if (this.advOpen) adv.dispatchEvent(new Event("toggle"));
    return this.card("list", "Requirements", null, h("div.gd-req", r.items.map((i) => h("div.gd-rq", h("span", i.label), h("b", i.value)))),
      this.styleRow(), adv);
  }

  // how the schematic's sheets will be joined: chosen before there is one (then the Schematic tab switches it)
  styleRow() {
    if (this.ws.p && this.ws.p.has_sch) return null;
    const cur = ((this.ws.p && this.ws.p.schematic) || {}).style || "hierarchical";
    const pick = async (style) => {
      try {
        const s = await api(`/api/projects/${enc(this.pid)}`, { method: "PATCH", body: { schematic: { style } } });
        this.ws.p = { ...this.ws.p, ...s };
        this.render();
      } catch (e) { toast(e.message, "error"); }
    };
    return h("div.gd-style", h("span", "Schematic sheets"),
      h("div.seg", [["hierarchical", "Hierarchical", "Sheet pins only: the top sheet shows what goes where"], ["flat", "Flat", "Global labels join the pages"]]
        .map(([k, t, tip]) => h("button" + (cur === k ? ".on" : ""), { onclick: () => pick(k), "data-tip": tip }, t))));
  }

  // ------------------------------------------------------------------ block diagram
  diagramCard(d) {
    this.diagram = d;
    this.diagramBox = h("div.gd-diagram");
    this.drawDiagram();
    const kinds = new Set((d.links || []).map((l) => l.kind || "signal"));
    const legend = kinds.size > 1 ? h("div.gd-legend", ["power", "bus", "signal"].filter((k) => kinds.has(k))
      .map((k) => h("span", h("i." + k), k === "power" ? "Power" : k === "bus" ? "Bus" : "Signal"))) : null;
    return this.card("layout-grid", "Block diagram", `${d.blocks.length} blocks`, this.diagramBox, legend);
  }

  // Laid out for the column's width, again when the width changes enough to change the rows.
  drawDiagram() {
    const box = this.diagramBox;
    if (!box || !this.diagram) return;
    const cw = this.el.clientWidth, w = cw ? cw - 78 : 560;
    if (box.dataset.w && Math.abs(w - Number(box.dataset.w)) < 20) return;
    box.dataset.w = w;
    box.innerHTML = diagramSVG(this.diagram, { width: w, measure });
  }

  // ------------------------------------------------------------------ floorplan
  // The board to scale: drag blocks, holes and connectors (along the edges) into place, or the corner to size it.
  floorplanCard(fp) {
    // movable during the setup, and after it until there is a board (then the board is where parts move)
    const editable = this.cv.phase !== "done" || !(this.ws.p && this.ws.p.has_pcb);
    const opts = { pid: this.pid, editable, width: Math.max(320, (this.el.clientWidth || 600) - 58),
      maxSize: (this.cv.constraints || {}).max_size_mm || null };
    if (this.fpView) { Object.assign(this.fpView.opts, opts); this.fpView.set(fp); }
    else this.fpView = new FloorplanView(fp, opts);
    const n = (fp.items || []).length, moved = [...(fp.items || []), ...(fp.holes || [])].filter((o) => o.moved).length;
    const bad = this.fpView.bad ? this.fpView.bad.size : 0;
    const pr = this.cv.proposal;
    let list = null;
    this.fpView.setProposal(pr && pr.moves && pr.moves.length ? pr : null, () => this.render());
    if (editable) list = this.suggestBox(fp, pr);
    return this.card("layout-grid", "Floorplan", `${fp.board.w} × ${fp.board.h} mm`, this.fpView.el,
      h("div.gd-fpnote", bad ? h("span.bad", `${bad} overlap${bad === 1 ? "s" : ""} or run${bad === 1 ? "s" : ""} off the board`) : null,
        h("span", opts.editable ? `Drag to move, click to select: R turns, F puts it on the other side, L locks, arrows nudge${moved ? ` · ${moved} placed by you` : ""}. Claude follows what you place.`
          : `${n} items. The board holds the layout now.`)),
      fp.note ? h("div.gd-fpnote", fp.note) : null, list);
  }

  // Let Claude suggest a layout, with notes; its suggestion as ghosts on the floorplan, each move with why, a reply to
  // each note (followed: green, declined: red), and the user takes all, some or none
  suggestBox(fp, pr) {
    const enc = encodeURIComponent, base = `/api/projects/${enc(this.pid)}/canvas/floorplan`;
    const busy = this.ws.chat && this.ws.chat.busy;
    const name = (id) => { const o = [...(fp.items || []), ...(fp.holes || []), ...(fp.keepouts || [])].find((x) => x.id === id); return o ? o.ref || o.label || id : id; };
    if (pr && pr.moves && pr.moves.length) {
      const view = this.fpView;
      const rows = pr.moves.map((m, i) => h("label.gd-sg" + (view.take.has(m.id) ? ".on" : ""),
        h("input", { type: "checkbox", checked: view.take.has(m.id) || undefined, onchange: () => view.toggleTake(m.id) }),
        h("span.gd-sgn", String(i + 1)), h("b", name(m.id)), h("span", m.why || "")));
      const notes = (pr.notes || []).map((t, i) => {
        const r = (pr.replies || []).find((x) => x.note === i + 1);
        return h("div.gd-sgnote" + (r ? "." + r.outcome : ""), h("div.q", `Your note: ${t}`),
          r ? h("div.a", h("b", r.outcome === "declined" ? "Not followed: " : "Followed: "), r.text) : h("div.a.faint", "No reply"));
      });
      const n = view.take.size;
      return h("div.gd-suggest",
        h("div.gd-sgh", icon("sparkles", 14), h("b", "Claude's suggestion"), pr.summary ? h("span", pr.summary) : null),
        h("div.gd-sglist", rows), notes.length ? h("div.gd-sgnotes", notes) : null,
        h("div.gd-sgacts", h("button.btn.sm", { onclick: async () => { try { await api(`${base}/proposal`, { body: { action: "dismiss" } }); } catch (e) { toast(e.message, "error"); } } }, "Set it aside"),
          h("div.grow"),
          h("button.btn.sm.primary", { disabled: !n || undefined, onclick: async () => {
            try { await api(`${base}/proposal`, { body: { action: "accept", ids: [...view.take] } }); toast(`Took ${n} of Claude's ${pr.moves.length} moves`, "ok"); }
            catch (e) { toast(e.message, "error"); }
          } }, n === pr.moves.length ? "Take all of it" : `Take ${n} of ${pr.moves.length}`)));
    }
    if (pr && pr.asked) {
      return h("div.gd-suggest.wait", h("span.spinner"), h("span", "Claude is working on a layout"),
        h("button.btn.sm.ghost", { onclick: async () => { try { await api(`${base}/proposal`, { body: { action: "dismiss" } }); } catch (e) { toast(e.message, "error"); } } }, "Never mind"));
    }
    const notes = h("textarea", { rows: 2, placeholder: "Notes for Claude, e.g. USB-C on the left edge; the antenna away from the motor driver",
      oninput: () => { this.sgNotes = notes.value; } });
    notes.value = this.sgNotes || "";                     // the canvas is drawn again as Claude works: keep what was typed
    if (this.sgFocus) setTimeout(() => { notes.focus({ preventScroll: true }); notes.setSelectionRange(notes.value.length, notes.value.length); }, 0);
    notes.addEventListener("focus", () => { this.sgFocus = true; });
    notes.addEventListener("blur", () => { this.sgFocus = false; });
    return h("details.gd-suggest.ask", { open: this.sgOpen || undefined, ontoggle: (e) => { this.sgOpen = e.target.open; } },
      h("summary", icon("sparkles", 14), h("span", "Let Claude suggest a layout")),
      notes, h("div.gd-sgacts", h("span.tiny.faint", "One note a line. Locked items stay put."), h("div.grow"),
        h("button.btn.sm.primary", { disabled: busy || undefined, "data-tip": busy ? "Available when Claude finishes" : "", onclick: async () => {
          try { await api(`${base}/suggest`, { body: { notes: notes.value.split("\n").map((x) => x.trim()).filter(Boolean) } }); this.sgNotes = ""; this.sgFocus = false; }
          catch (e) { toast(e.message, "error"); }
        } }, "Suggest a layout")));
  }

  // ------------------------------------------------------------------ connectors
  connCard(c, planned) {
    const list = h("div.gd-conns", c.items.map((k) => {
      const pins = k.pins || [];
      const open = h("details.gd-conn", h("summary", h("b", k.ref ? `${k.ref} · ${k.name}` : k.name), h("span", k.type || ""),
        k.edge ? h("span.gd-edge", k.edge) : null, pins.length ? h("span.gd-np", `${pins.length} pins`) : null),
        pins.length ? h("div.gd-pins", pins.map((p) => h("div.gd-pin", h("span", p.n), h("b", p.signal || "—")))) : null);
      return open;
    }));
    return this.card("plug", "Connectors", `${c.items.length}`, planned ? null : h("div.gd-sketch", { html: boardSketch(c) }), list);
  }

  // ------------------------------------------------------------------ live BOM
  partsCard(p) {
    let total = 0, priced = 0;
    const rows = p.items.map((it) => {
      const stock = it.jlc_stock ?? it.lcsc_stock;
      if (it.price != null) { total += it.price * (it.qty || 1); priced++; }
      const st = !it.lcsc ? h("span.gd-tag", "no code") : it.pending ? h("span.gd-look", h("span.spinner"), "looking up")
        : !it.found ? h("span.gd-tag", "no stock data")
        : h("span.gd-stock." + (stock > 1000 ? "ok" : stock > 0 ? "warn" : "err"), stock != null ? `${Number(stock).toLocaleString()} in stock` : "stock unknown");
      return h("div.gd-part",
        h("div.gd-pn", h("b", it.mpn || it.role), h("span", [it.role, it.package].filter(Boolean).join(" · "))),
        it.lcsc ? h("span.gd-code", it.lcsc) : h("span"),
        it.lib ? h("span.gd-lib." + (it.lib === "Basic" ? "basic" : "ext"), it.lib) : h("span"),
        st,
        h("span.gd-price", it.price != null ? `$${Number(it.price).toFixed(it.price < 1 ? 3 : 2)}` : ""));
    });
    const foot = priced ? h("div.gd-total", h("span", `Parts for one board (${priced} of ${p.items.length} priced)`), h("b", `$${total.toFixed(2)}`)) : null;
    return this.card("list", "Parts", `${p.items.length} key parts · live from JLC`, h("div.gd-parts", rows), foot);
  }
}

// ------------------------------------------------------------------ drawing
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function boardSketch(c) {
  const bw = (c.board && c.board.w) || 60, bh = (c.board && c.board.h) || 40;
  const S = Math.min(280 / bw, 150 / bh), W = bw * S, H = bh * S, M = 70;
  const x0 = M, y0 = 34;
  const byEdge = { left: [], right: [], top: [], bottom: [] };
  for (const k of c.items) if (byEdge[k.edge]) byEdge[k.edge].push(k);
  let parts = `<rect class="bs-board" x="${x0}" y="${y0}" width="${W}" height="${H}" rx="6"/>`;
  parts += `<text class="bs-dim" x="${x0 + W / 2}" y="${y0 + H / 2 + 4}">${c.board && c.board.w ? `${bw} × ${bh} mm` : "board"}</text>`;
  for (const [edge, list] of Object.entries(byEdge)) {
    list.forEach((k, i) => {
      const f = (i + 1) / (list.length + 1);
      const name = esc((k.ref || k.name).slice(0, 12));
      if (edge === "left" || edge === "right") {
        const y = y0 + H * f, x = edge === "left" ? x0 - 8 : x0 + W - 6;
        parts += `<rect class="bs-c" x="${x}" y="${y - 7}" width="14" height="14" rx="2"/>`;
        parts += `<text class="bs-l" x="${edge === "left" ? x - 6 : x + 20}" y="${y + 4}" text-anchor="${edge === "left" ? "end" : "start"}">${name}</text>`;
      } else {
        const x = x0 + W * f, y = edge === "top" ? y0 - 8 : y0 + H - 6;
        parts += `<rect class="bs-c" x="${x - 7}" y="${y}" width="14" height="14" rx="2"/>`;
        parts += `<text class="bs-l" x="${x}" y="${edge === "top" ? y - 6 : y + 28}" text-anchor="middle">${name}</text>`;
      }
    });
  }
  return `<svg viewBox="0 0 ${W + M * 2} ${H + 70}" width="100%" style="max-height:170px">${parts}</svg>`;
}
