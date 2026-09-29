// The bill of materials: every part, grouped the way JLC orders them, with stock and price from the
// dated parts cache and what the checks say about each part. Click a line (or one designator) and the
// parts light up on the board and in the schematic beside the table, and in the Board, Schematic and
// 3D tabs; click a part in either preview and its line is selected.
import { h, clear, api, toast, btn, menu, fmtTime, debounce } from "./util.js";
import { icon } from "./icons.js";
import { native } from "./native.js";
import { PAPER } from "./schematic.js";

const enc = encodeURIComponent;
const NS = "http://www.w3.org/2000/svg";
const lcscURL = (c) => `https://www.lcsc.com/product-detail/${encodeURIComponent(c)}.html`;
const ease = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

const FILTERS = [
  ["all", "All", () => true],
  ["issues", "Issues", (r) => r.issues.length > 0],
  ["nolcsc", "No LCSC code", (r) => r.assembled && !r.lcsc],
  ["extended", "Extended", (r) => (r.source || {}).lib === "Extended"],
  ["stock", "Low stock", (r) => lowStock(r)],
  ["dnp", "Not placed", (r) => !r.assembled],
];

function lowStock(r) {
  const s = r.source || {};
  const st = s.jlc_stock ?? s.lcsc_stock;
  return r.assembled && st !== undefined && st !== null && st < Math.max(50, r.qty * 20);
}

function fmtStock(n) {
  if (n === undefined || n === null) return null;
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "k";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

function fmtPrice(p) {
  if (p === undefined || p === null || p === "") return null;
  const v = +p;
  return v >= 1 ? "$" + v.toFixed(2) : v >= 0.01 ? "$" + v.toFixed(3) : "$" + v.toFixed(4);
}

export class BomView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.filter = localStorage.getItem("tw.bom.filter") || "all"; this.q = ""; this.grouped = localStorage.getItem("tw.bom.each") !== "1";
    this.sel = new Set(); this.side = localStorage.getItem("tw.bom.side") !== "0"; this.lookup = null;
    this.build();
    ws.ev.on("bom.changed", () => this.load());
    ws.ev.on("checks.done", () => this.stale = true);
    ws.ev.on("schematic.changed", () => { this.stale = true; this.sch.stale = true; if (this.el.classList.contains("on")) this.load(); });
    ws.ev.on("board.changed", () => { this.board.stale = true; if (this.el.classList.contains("on")) this.board.load(); });
    ws.ev.on("bom.lookup", (e) => this.lookupEvent(e));
    this.load();
  }

  build() {
    this.head = h("div.bom-head");
    this.tools = h("div.bom-tools");
    this.table = h("div.bom-table");
    this.left = h("div.bom-left", this.head, this.tools, this.table);
    this.boardPane = h("div.bom-pane");
    this.schPane = h("div.bom-pane");
    this.right = h("div.bom-right", this.boardPane, this.schPane);
    this.split = h("div.bom-split");
    this.box = h("div.bomview" + (this.side ? "" : ".noside"), this.left, this.split, this.right);
    this.el.appendChild(this.box);
    const w = +localStorage.getItem("tw.bom.sidew");
    if (w) this.right.style.width = w + "px";
    this.dragSplit();
    this.board = new MiniBoard(this.boardPane, this.ws, (ref, add) => this.pick(ref, add, "board"));
    this.sch = new MiniSchematic(this.schPane, this.ws, (ref, add) => this.pick(ref, add, "schematic"));
    this.ro = new ResizeObserver(() => { this.box.classList.toggle("narrow", this.box.clientWidth < 880); this.board.resize(); this.sch.resize(); });
    this.ro.observe(this.box);
  }

  destroy() { this.ro && this.ro.disconnect(); this.board.destroy(); }
  shown() { if (this.stale || !this.data) this.load(); this.board.shown(); this.sch.shown(); if (this.pending) { const p = this.pending; this.pending = null; this.probe(p, "pending"); } }
  resize() { this.board.resize(); this.sch.resize(); }

  dragSplit() {
    this.split.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const x0 = e.clientX, w0 = this.right.getBoundingClientRect().width;
      this.split.classList.add("drag");
      const mv = (ev) => { this.right.style.width = Math.max(300, Math.min(this.box.clientWidth - 420, w0 - (ev.clientX - x0))) + "px"; this.resize(); };
      const up = () => { this.split.classList.remove("drag"); removeEventListener("mousemove", mv); removeEventListener("mouseup", up); localStorage.setItem("tw.bom.sidew", parseInt(this.right.style.width)); this.resize(); };
      addEventListener("mousemove", mv); addEventListener("mouseup", up);
    });
  }

  async load() {
    this.stale = false;
    try { this.data = await api(`/api/projects/${enc(this.pid)}/bom`); }
    catch (e) { clear(this.table).appendChild(h("div.empty", h("div.eicon", icon("triangle-alert", 20)), h("h3", "The BOM could not be read"), h("p", e.message))); return; }
    if (this.data.looking_up && !this.lookup) this.lookup = { done: 0, total: 0 };
    this.render();
    this.board.load();
    this.sch.load();
  }

  // ------------------------------------------------------------------ selection (shared with the other tabs)
  pick(ref, add, source) {
    if (!ref) { if (!add) this.setSel([], source); return; }
    const s = add ? new Set(this.sel) : new Set();
    if (add && s.has(ref)) s.delete(ref); else s.add(ref);
    this.setSel([...s], source);
  }

  pickRow(r, add) {
    const s = add ? new Set(this.sel) : new Set();
    const all = r.refs.every((x) => s.has(x));
    for (const x of r.refs) if (all && add) s.delete(x); else s.add(x);
    this.setSel([...s], "bom");
  }

  setSel(refs, source) {
    this.sel = new Set(refs);
    this.paintSel(source !== "bom");
    this.board.setSel(refs, source !== "board");
    this.sch.setSel(refs, source !== "schematic");
    this.ws.select(refs.map((r) => ({ ref: r, kind: "footprint" })), "bom");
  }

  // another tab selected parts: follow it
  probe(refs, source) {
    if (!this.el.classList.contains("on")) { this.pending = refs; return; }
    this.sel = new Set(refs);
    this.paintSel(true);
    this.board.setSel(refs, true);
    this.sch.setSel(refs, true);
  }

  paintSel(scroll) {
    let first = null;
    for (const tr of this.table.querySelectorAll(".bom-row")) {
      const refs = tr.__row ? tr.__row.refs : [];
      const any = refs.some((r) => this.sel.has(r));
      tr.classList.toggle("sel", any);
      if (any && !first) first = tr;
      for (const c of tr.querySelectorAll(".rchip")) c.classList.toggle("on", this.sel.has(c.dataset.ref));
    }
    if (scroll && first) {
      const r = first.getBoundingClientRect(), t = this.table.getBoundingClientRect();
      if (r.top < t.top + 34 || r.bottom > t.bottom) first.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    this.renderSelBar();
  }

  renderSelBar() {
    const bar = this.selBar;
    if (!bar) return;
    clear(bar);
    bar.style.display = this.sel.size ? "flex" : "none";
    if (!this.sel.size) return;
    const refs = [...this.sel];
    const label = refs.length > 6 ? `${refs.slice(0, 6).join(", ")} +${refs.length - 6}` : refs.join(", ");
    bar.append(icon("microchip", 14), h("span.sl", label),
      h("button.tbtn", { onclick: () => this.ws.locate({ ref: refs[0] }), "data-tip": "Show on board" }, icon("circuit-board", 14), h("span", "Board")),
      h("button.tbtn", { onclick: () => { this.ws.show("schematic"); this.ws.view("schematic").probe(refs, "bom", true); }, "data-tip": "Show in schematic" }, icon("waypoints", 14), h("span", "Schematic")),
      h("button.tbtn", { onclick: () => this.setSel([], "bom"), "data-tip": "Clear selection" }, icon("x", 14)));
  }

  // ------------------------------------------------------------------ the table
  rows() {
    const d = this.data;
    let rows = d.rows || [];
    if (!this.grouped) {
      const val = Object.fromEntries((d.parts || []).map((p) => [p.ref, p.value]));
      rows = rows.flatMap((r) => r.refs.map((ref) => ({ ...r, refs: [ref], qty: 1, value: val[ref] || r.value, values: [val[ref] || r.value],
        issues: r.issues.filter((i) => i.ref === ref) })));
    }
    const f = FILTERS.find((x) => x[0] === this.filter) || FILTERS[0];
    rows = rows.filter(f[2]);
    const q = this.q.trim().toLowerCase();
    if (q) rows = rows.filter((r) => [r.refs.join(" "), (r.values || [r.value]).join(" "), r.footprint, r.mpn, r.mfr, r.lcsc, r.description, (r.source || {}).name]
      .some((v) => (v || "").toLowerCase().includes(q)));
    return rows;
  }

  render() {
    const d = this.data;
    const head = clear(this.head);
    if (d.empty) {
      clear(this.tools); clear(this.table).appendChild(h("div.empty", h("div.eicon", icon("list-checks", 20)), h("h3", "No schematic yet"),
        h("p", "The BOM is built from the schematic.")));
      return;
    }
    const t = d.totals;
    const busy = !!this.lookup;
    const stat = (v, label, cls, tip) => h("div.bstat" + (cls ? "." + cls : ""), tip ? { "data-tip": tip } : {}, h("b", v), h("span", label));
    head.append(
      h("div.bom-title", h("div.vic", icon("package", 18)), h("div", h("div.vt", "Bill of materials"),
        h("div.vs", `${t.lines} lines · ${t.parts} parts${t.dnp ? ` · ${t.dnp} not placed` : ""}${d.checked ? ` · checked ${fmtTime(d.checked)}` : ""}`))),
      h("div.grow"),
      h("div.bom-actions",
        busy ? h("div.bom-look", h("span.spinner"), h("span", this.lookup.total ? `Looking up ${this.lookup.done} of ${this.lookup.total}` : "Looking up…"),
          h("button.btn.sm.ghost", { onclick: () => api(`/api/projects/${enc(this.pid)}/bom/stop`, { body: {} }) }, "Stop"))
          : btn("refresh-cw", t.lib_known >= t.codes && t.codes ? "Refresh stock" : "Look up stock", { onclick: () => this.startLookup(),
            "data-tip": "Stock, library and price from LCSC and JLC" }, t.lib_known < t.codes ? "primary sm" : "sm"),
        t.lib_known && t.extended ? btn("piggy-bank", "Save money", { onclick: () => this.savings(), "data-tip": "Find Basic-part equivalents" }, "sm") : null,
        h("button.btn.sm", { onclick: (e) => this.exportMenu(e.currentTarget) }, icon("download", 14), h("span", "Export"), icon("chevron-down", 12)),
        h("button.btn.sm.ghost" + (this.side ? ".on" : ""), { onclick: () => this.toggleSide(), "data-tip": this.side ? "Hide preview" : "Show preview" }, icon("panel-right", 14))));
    const statsRow = h("div.bom-stats",
      stat(String(t.assembled), "placed by JLC", null, `${t.assembled_lines} lines`),
      stat(String(t.codes), "part codes"),
      stat(t.lib_known ? `${t.basic} / ${t.extended}` : "-", "Basic / Extended", t.extended ? "warn" : null,
        t.lib_known ? `About $3 per Extended part per order${t.lib_known < t.codes ? ` · ${t.codes - t.lib_known} not looked up` : ""}` : "Look up stock to see the library"),
      stat(t.cost !== null ? fmtPrice(t.cost) : "-", "parts per board", null, t.cost !== null ? `${t.priced} of ${t.assembled_lines} lines priced, at quantity 1` : "No prices yet"),
      t.no_lcsc ? stat(String(t.no_lcsc), "without LCSC code", "err") : null,
      t.missing_board ? stat(String(t.missing_board), "not on the board", "err") : null);
    head.appendChild(statsRow);
    // tools
    const tools = clear(this.tools);
    const search = h("input", { placeholder: "Filter", value: this.q, spellcheck: false,
      oninput: debounce((e) => { this.q = e.target.value; this.drawTable(); }, 120) });
    const counts = Object.fromEntries(FILTERS.map(([k, , f]) => [k, (d.rows || []).filter(f).length]));
    tools.append(h("div.findbox.bom-find", icon("search", 13), search),
      h("div.seg.bom-filter", FILTERS.map(([k, label]) => (k === "all" || counts[k]) ? h("button" + (this.filter === k ? ".on" : ""), { onclick: () => { this.filter = k; localStorage.setItem("tw.bom.filter", k); this.render(); } },
        label, k !== "all" ? h("span.n", String(counts[k])) : null) : null)),
      h("div.grow"),
      h("div.seg", h("button" + (this.grouped ? ".on" : ""), { onclick: () => this.setGrouped(true), "data-tip": "One line per part number" }, "Grouped"),
        h("button" + (!this.grouped ? ".on" : ""), { onclick: () => this.setGrouped(false), "data-tip": "One line per part" }, "Each part")));
    this.drawTable();
  }

  setGrouped(g) { this.grouped = g; localStorage.setItem("tw.bom.each", g ? "0" : "1"); this.render(); }

  drawTable() {
    const rows = this.rows();
    const scroll = this.table.scrollTop;
    const tb = clear(this.table);
    tb.appendChild(h("div.bom-tr.bom-th", h("div.c-qty", "Qty"), h("div.c-refs", "Designators"), h("div.c-val", "Value"),
      h("div.c-mpn", "Part"), h("div.c-lcsc", "LCSC · JLC"), h("div.c-stock", "Stock"), h("div.c-iss", "")));
    if (!rows.length) { tb.appendChild(h("div.empty.small", h("p", "No part matches."))); return; }
    for (const r of rows) tb.appendChild(this.row(r));
    if (!this.selBar) { this.selBar = h("div.selbar.bom-selbar", { style: { display: "none" } }); this.left.appendChild(this.selBar); }
    this.table.scrollTop = scroll;
    this.paintSel(false);
  }

  row(r) {
    const s = r.source || {};
    const stock = s.jlc_stock ?? s.lcsc_stock;
    const price = s.jlc_price ?? s.lcsc_price;
    const worst = r.issues.some((i) => i.severity === "error") ? "err" : r.issues.length ? "warn" : "";
    const chips = h("div.c-refs", r.refs.slice(0, 40).map((ref) => h("span.rchip", { "data-ref": ref, onclick: (e) => { e.stopPropagation(); this.pick(ref, e.shiftKey || e.metaKey, "bom"); } }, ref)),
      r.refs.length > 40 ? h("span.more", `+${r.refs.length - 40}`) : null);
    const lib = s.lib ? h("span.lib" + (s.lib === "Extended" ? ".ext" : ".basic"), s.lib) : r.lcsc ? h("span.lib", s.jlc_utc ? "not at JLC" : "not looked up") : null;
    const stockTip = stock !== undefined && stock !== null
      ? [s.jlc_stock !== undefined && s.jlc_stock !== null ? `JLC ${s.jlc_stock.toLocaleString()}` : "", s.lcsc_stock !== undefined && s.lcsc_stock !== null ? `LCSC ${s.lcsc_stock.toLocaleString()}` : ""]
        .filter(Boolean).join(" · ") + (s.jlc_utc || s.lcsc_utc ? ` (asked ${fmtTime(s.jlc_utc || s.lcsc_utc)})` : "")
      : "Not looked up yet";
    const el = h("div.bom-tr.bom-row" + (r.assembled ? "" : ".dnp"), { onclick: (e) => this.pickRow(r, e.shiftKey || e.metaKey), ondblclick: () => this.ws.locate({ ref: r.refs[0] }) },
      h("div.c-qty", String(r.qty)), chips,
      h("div.c-val", { title: `${(r.values || [r.value]).join("\n")}\n${r.lib}` }, h("div.ellipsis", h("b", r.value),
        (r.values || []).length > 1 ? h("span.muted.small", ` +${r.values.length - 1} names`) : null,
        r.assembled ? null : h("span.badge.soft", r.dnp ? "DNP" : "not placed")),
        h("div.sub.mono.ellipsis", r.footprint)),
      h("div.c-mpn", { title: s.name || r.description || "" }, h("div.ellipsis", r.mpn || h("span.muted", "no MPN")), r.mfr || s.brand ? h("div.sub.ellipsis", r.mfr || s.brand) : null),
      h("div.c-lcsc", r.lcsc ? h("a.mono", { href: lcscURL(r.lcsc), onclick: (e) => { e.stopPropagation(); e.preventDefault(); native.openURL(lcscURL(r.lcsc)); }, "data-tip": s.name || "Open on LCSC" }, r.lcsc)
        : (r.assembled ? h("span.badge.err", "no code") : h("span.muted", "-")), lib ? h("div.sub", lib) : null),
      h("div.c-stock" + (lowStock(r) ? ".low" : ""), { "data-tip": stockTip },
        h("div", fmtStock(stock) || h("span.muted", "-")), h("div.sub", fmtPrice(price) || "")),
      h("div.c-iss", r.issues.length ? h("span.iss." + worst, { "data-tip": r.issues.slice(0, 6).map((i) => `${i.ref}: ${i.message}`).join("\n") + (r.issues.length > 6 ? `\n+ ${r.issues.length - 6} more` : ""),
        onclick: (e) => { e.stopPropagation(); this.ws.show("checks"); } }, icon(worst === "err" ? "circle-x" : "triangle-alert", 13), String(r.issues.length)) : null));
    el.__row = r;
    return el;
  }

  // Basic-part equivalents for the Extended resistors and capacitors; Claude makes the swaps
  async savings() {
    const body = h("div.sv-body", h("div.sv-run", h("span.spinner"), this.svProg = h("span", "Searching JLC's Basic library…")));
    const m = modal({ title: "Save on Extended-part fees", icon: "piggy-bank", cls: "wide", sub: "Exact Basic-library equivalents in stock. Each Extended part costs about $3 per order.",
      body, actions: [] });
    const off = this.ws.ev.on("savings.progress", (e) => { if (this.svProg) this.svProg.textContent = `Checking ${e.done} of ${e.total}: ${e.what}`; });
    let r;
    try { r = await api(`/api/projects/${enc(this.pid)}/bom/savings`, { body: { boards: 5 } }); } catch (e) { off(); clear(body).appendChild(h("div.errline", icon("circle-alert", 14), e.message)); return; }
    off();
    clear(body);
    const picked = new Set(r.items.map((_, i) => i));
    if (!r.items.length) body.appendChild(h("div.sv-none", icon("circle-check", 18), h("div", h("b", "No equivalents found"),
      h("span", `None of the ${r.checked} Extended lines has an exact Basic equivalent.`))));
    else body.appendChild(h("div.sv-total", h("b", `Save about $${r.total.toFixed(2)} per order`), h("span", `${r.items.length} swap${r.items.length === 1 ? "" : "s"} · 5 boards`)));
    for (const [i, it] of r.items.entries()) {
      const cb = h("input", { type: "checkbox", checked: true, onchange: () => { cb.checked ? picked.add(i) : picked.delete(i); go.disabled = !picked.size; } });
      body.appendChild(h("label.sv-row", cb, h("div.grow", h("div.sv-t", h("b", it.refs.join(", ")), h("span", `${it.value} · ${it.footprint}`)),
        h("div.sv-sw", h("span.sv-old", it.now.lcsc), icon("arrow-right", 12), h("span.sv-new", it.instead.lcsc), h("span.muted", it.instead.describe))),
        h("span.sv-save", `$${it.saving.toFixed(2)}`)));
    }
    if (r.skipped.length) body.appendChild(h("details.sv-skip", h("summary", `${r.skipped.length} unchanged`),
      r.skipped.map((x) => h("div.sv-sk", h("b", x.refs.slice(0, 6).join(", ")), h("span", x.why)))));
    const go = btn("sparkles", "Swap with Claude", { onclick: () => {
      const items = r.items.filter((_, i) => picked.has(i));
      m.close();
      this.ws.ask("");
      this.ws.chat.input.value = messageFor(items); this.ws.chat.grow(); this.ws.chat.input.focus();
    } }, "primary");
    if (r.items.length) m.box.querySelector(".modal-foot") ? m.box.querySelector(".modal-foot").append(go) : m.box.appendChild(h("div.modal-foot", h("button.btn", { onclick: () => m.close() }, "Close"), go));
    else m.box.appendChild(h("div.modal-foot", h("button.btn.primary", { onclick: () => m.close() }, "Close")));
  }

  exportMenu(anchor) {
    const dl = (kind) => native.save(`/api/projects/${enc(this.pid)}/bom.csv?kind=${kind}`);
    menu(anchor, [{ label: "JLC BOM (.csv)", icon: "package", hint: "Comment, Designator, Footprint, LCSC", run: () => dl("jlc") },
      { label: "Full BOM (.csv)", icon: "sheet", hint: "With stock and prices", run: () => dl("full") }], { align: "end" });
  }

  toggleSide() {
    this.side = !this.side;
    localStorage.setItem("tw.bom.side", this.side ? "1" : "0");
    this.box.classList.toggle("noside", !this.side);
    this.render();
    requestAnimationFrame(() => this.resize());
  }

  async startLookup() {
    try { await api(`/api/projects/${enc(this.pid)}/bom/lookup`, { body: {} }); this.lookup = { done: 0, total: 0 }; this.render(); }
    catch (e) { toast(e.message, "error"); }
  }

  lookupEvent(e) {
    if (e.finished) {
      this.lookup = null;
      if (e.error) toast("Parts lookup failed: " + e.error, "error", 8000);
      else if (e.failed) toast(`No answer for ${e.failed} part code${e.failed > 1 ? "s" : ""}${e.note ? ` (${e.note})` : ""}. Try again later.`, "info", 8000);
      else if (e.stopped) toast("Lookup stopped", "info");
      else toast("Stock and prices updated", "ok");
      return;
    }
    this.lookup = { done: e.done || 0, total: e.total || 0 };
    const el = this.head.querySelector(".bom-look span:not(.spinner)");
    if (el) el.textContent = `Looking up ${this.lookup.done} of ${this.lookup.total}`; else if (this.data) this.render();
  }
}

// --------------------------------------------------------------------------- the board, iBOM style
class MiniBoard {
  constructor(el, ws, onPick) {
    this.el = el; this.ws = ws; this.pid = ws.pid; this.onPick = onPick;
    this.side = "F"; this.sel = new Set(); this.hover = null; this.scale = 1; this.ox = 0; this.oy = 0;
    this.canvas = h("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.tip = h("div.vtip", { style: { display: "none" } });
    this.sideBtns = h("div.seg.tiny", h("button.on", { onclick: () => this.setSide("F") }, "Top"), h("button", { onclick: () => this.setSide("B") }, "Bottom"));
    this.title = h("span.bp-title", icon("circuit-board", 13), h("span", "Board"));
    el.append(h("div.bp-head", this.title, h("div.grow"), this.sideBtns,
      h("button.tbtn", { onclick: () => this.fit(true), "data-tip": "Fit the board" }, icon("scan", 14)),
      h("button.tbtn", { onclick: () => this.ws.show("board"), "data-tip": "Open the Board tab" }, icon("external-link", 13))),
      h("div.bp-body", this.canvas, this.tip));
    this.body = el.lastChild;
    this.mouse();
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.body);
  }

  destroy() { this.ro.disconnect(); }
  shown() { this.resize(); }

  async load() {
    const bv = this.ws.views.board;
    let d = bv && bv.data;
    if (!d || this.stale) {
      try { d = await api(`/api/projects/${enc(this.pid)}/board`); } catch { return; }
    }
    this.stale = false;
    if (!d || d.empty) { this.d = null; this.draw(); return; }
    const first = !this.d;
    this.d = d;
    this.byRef = Object.fromEntries(d.footprints.map((f) => [f.ref, f]));
    this.tracks = { F: d.tracks.filter((t) => t[5] === "F.Cu"), B: d.tracks.filter((t) => t[5] === "B.Cu") };
    if (first) this.fit(false);
    if (this.sel.size) this.setSel([...this.sel], true); else this.draw();
  }

  resize() {
    const r = this.body.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const dpr = Math.min(2, devicePixelRatio || 1);
    this.w = r.width; this.h = r.height; this.dpr = dpr;
    this.canvas.width = Math.round(r.width * dpr); this.canvas.height = Math.round(r.height * dpr);
    if (!this.fitted) this.fit(false); else this.draw();
  }

  setSide(s) {
    this.side = s;
    for (const [i, b] of [...this.sideBtns.children].entries()) b.classList.toggle("on", (i === 0) === (s === "F"));
    this.fit(false);
  }

  // board mm -> screen px (the bottom is seen mirrored, as KiCad shows it)
  tx(x) { const b = this.d.bbox; return this.side === "B" ? (b[0] + b[2] - x) : x; }
  toScreen(x, y) { return [(this.tx(x) - this.ox) * this.scale, (y - this.oy) * this.scale]; }
  toWorld(px, py) { const x = px / this.scale + this.ox, b = this.d.bbox; return [this.side === "B" ? b[0] + b[2] - x : x, py / this.scale + this.oy]; }

  view(box, ms) {
    if (!this.d || !this.w) return;
    const [x0, y0, x1, y1] = box;
    const bw = Math.max(x1 - x0, 1), bh = Math.max(y1 - y0, 1);
    const s = Math.min(this.w / bw, this.h / bh) * 0.9;
    const cx = this.tx((x0 + x1) / 2), cy = (y0 + y1) / 2;
    const to = { scale: s, ox: cx - this.w / s / 2, oy: cy - this.h / s / 2 };
    if (!ms) { Object.assign(this, to); this.draw(); return; }
    const from = { scale: this.scale, ox: this.ox, oy: this.oy }, t0 = performance.now();
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / ms), e = ease(k);
      this.scale = from.scale + (to.scale - from.scale) * e; this.ox = from.ox + (to.ox - from.ox) * e; this.oy = from.oy + (to.oy - from.oy) * e;
      this.draw();
      if (k < 1) requestAnimationFrame(step);
    };
    step();
  }

  fit(anim) { if (!this.d || !this.w) return; this.fitted = true; const b = this.d.bbox; this.view([b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2], anim ? 300 : 0); }

  setSel(refs, fly) {
    this.sel = new Set(refs);
    if (!this.d) return;
    const fps = refs.map((r) => this.byRef[r]).filter(Boolean);
    if (fps.length && fps.every((f) => f.side !== this.side)) this.setSide(fps[0].side);
    if (fly && fps.length) {
      const xs = fps.flatMap((f) => [f.bbox[0], f.bbox[2]]), ys = fps.flatMap((f) => [f.bbox[1], f.bbox[3]]);
      const b = this.d.bbox;
      let box = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
      const w = Math.max(box[2] - box[0], (b[2] - b[0]) * 0.25), hh = Math.max(box[3] - box[1], (b[3] - b[1]) * 0.25);
      const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
      if (fps.length > 1 && (box[2] - box[0]) > (b[2] - b[0]) * 0.5) box = [b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2];
      else box = [cx - w / 2 - 2, cy - hh / 2 - 2, cx + w / 2 + 2, cy + hh / 2 + 2];
      this.view(box, 380);
    } else this.draw();
  }

  pickFp(x, y) {
    let best = null;
    for (const f of this.d.footprints) {
      if (f.side !== this.side) continue;
      const b = f.bbox;
      if (x < b[0] || x > b[2] || y < b[1] || y > b[3]) continue;
      const a = (b[2] - b[0]) * (b[3] - b[1]);
      if (!best || a < best[0]) best = [a, f];
    }
    return best ? best[1] : null;
  }

  draw() {
    const c = this.ctx, W = this.canvas.width, H = this.canvas.height;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = "#0d0e11"; c.fillRect(0, 0, W, H);
    if (!this.d) {
      c.fillStyle = "#6b6d76"; c.font = `${12 * (this.dpr || 1)}px -apple-system, sans-serif`; c.textAlign = "center";
      c.fillText("No board yet", W / 2, H / 2);
      return;
    }
    const s = this.scale * this.dpr;
    const b = this.d.bbox;
    c.setTransform(this.side === "B" ? -s : s, 0, 0, s, this.side === "B" ? (b[0] + b[2] - this.ox) * s : -this.ox * s, -this.oy * s);
    const px = 1 / s;
    // board
    c.beginPath();
    for (const loop of this.d.outline || []) { loop.forEach((p, i) => i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1])); c.closePath(); }
    c.fillStyle = "#17261f"; c.fill("evenodd");
    c.lineWidth = 1.4 * px; c.strokeStyle = "rgba(238,214,72,.85)"; c.stroke();
    // copper, faint, for orientation
    c.lineCap = "round";
    c.strokeStyle = this.side === "F" ? "rgba(214,90,70,.22)" : "rgba(80,120,230,.24)";
    for (const t of this.tracks[this.side] || []) { c.lineWidth = Math.max(t[4], px); c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); }
    const sel = this.sel, anySel = sel.size > 0;
    // footprints
    for (const f of this.d.footprints) {
      const mine = f.side === this.side;
      const on = sel.has(f.ref), hov = this.hover === f.ref;
      const alpha = mine ? (anySel && !on ? 0.5 : 1) : 0.16;
      for (const p of f.pads || []) {
        if (!mine && !p.d) continue;                  // the other side shows only its through-hole pads
        c.fillStyle = on ? "rgba(255,170,110,1)" : `rgba(201,171,92,${alpha * (mine ? 0.85 : 1)})`;
        for (const pl of p.p || []) { c.beginPath(); pl.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.closePath(); c.fill(); }
      }
      if (!mine) continue;
      const bb = f.bbox;
      if (on || hov) {
        c.fillStyle = on ? "rgba(235,138,80,.22)" : "rgba(255,255,255,.06)";
        c.fillRect(bb[0] - 0.3, bb[1] - 0.3, bb[2] - bb[0] + 0.6, bb[3] - bb[1] + 0.6);
        c.lineWidth = (on ? 2 : 1.2) * px; c.strokeStyle = on ? "rgba(244,165,116,1)" : "rgba(255,255,255,.55)";
        c.strokeRect(bb[0] - 0.3, bb[1] - 0.3, bb[2] - bb[0] + 0.6, bb[3] - bb[1] + 0.6);
      } else if (f.cy && f.cy.length) {
        c.lineWidth = px; c.strokeStyle = `rgba(160,170,190,${anySel ? 0.12 : 0.25})`;
        for (const l of f.cy) { c.beginPath(); l.forEach((q, i) => i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])); c.closePath(); c.stroke(); }
      }
    }
    // labels: the selection always, the rest when there is room
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.textAlign = "center"; c.textBaseline = "middle";
    const fs = 11 * this.dpr;
    const all = this.scale > 7;
    for (const f of this.d.footprints) {
      if (f.side !== this.side) continue;
      const on = sel.has(f.ref);
      if (!on && !(all && !anySel)) continue;
      const [sx, sy] = this.toScreen((f.bbox[0] + f.bbox[2]) / 2, f.bbox[1]);
      const label = f.ref;
      c.font = `${on ? 600 : 500} ${fs}px -apple-system, "SF Pro Text", sans-serif`;
      const tw = c.measureText(label).width + 8 * this.dpr;
      const y = sy * this.dpr - 9 * this.dpr;
      if (on) { c.fillStyle = "rgba(235,138,80,.95)"; roundRect(c, sx * this.dpr - tw / 2, y - 8 * this.dpr, tw, 16 * this.dpr, 4 * this.dpr); c.fill(); c.fillStyle = "#1d1007"; }
      else c.fillStyle = "rgba(220,222,228,.8)";
      c.fillText(label, sx * this.dpr, y);
    }
  }

  mouse() {
    const cv = this.canvas;
    let drag = null;
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (!this.d) return;
      const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX) && !e.shiftKey) {
        const f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022));
        const wx = mx / this.scale + this.ox, wy = my / this.scale + this.oy;
        this.scale = Math.max(0.5, Math.min(400, this.scale * f));
        this.ox = wx - mx / this.scale; this.oy = wy - my / this.scale;
      } else { this.ox += e.deltaX / this.scale; this.oy += e.deltaY / this.scale; }
      this.draw();
    }, { passive: false });
    cv.addEventListener("mousedown", (e) => { drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy, moved: false }; });
    addEventListener("mousemove", (e) => {
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        if (drag.moved) { this.ox = drag.ox - dx / this.scale; this.oy = drag.oy - dy / this.scale; cv.style.cursor = "grabbing"; this.draw(); }
        return;
      }
      if (e.target !== cv || !this.d) return;
      const r = cv.getBoundingClientRect();
      const [x, y] = this.toWorld(e.clientX - r.left, e.clientY - r.top);
      const f = this.pickFp(x, y);
      const ref = f && f.side === this.side ? f.ref : null;
      if (ref !== this.hover) { this.hover = ref; this.draw(); }
      cv.style.cursor = ref ? "pointer" : "grab";
      if (ref) {
        this.tip.innerHTML = `<b>${esc(f.ref)}</b> ${esc(f.val)}<br><span class="k">${esc((f.lib || "").split(":").pop())}</span>`;
        this.tip.style.display = "block";
        this.tip.style.left = Math.min(e.clientX - r.left + 14, r.width - 220) + "px";
        this.tip.style.top = Math.min(e.clientY - r.top + 12, r.height - 50) + "px";
      } else this.tip.style.display = "none";
    });
    addEventListener("mouseup", (e) => {
      if (!drag) return;
      const d = drag; drag = null;
      cv.style.cursor = this.hover ? "pointer" : "grab";
      if (d.moved || e.target !== cv || !this.d) return;
      const r = cv.getBoundingClientRect();
      const [x, y] = this.toWorld(e.clientX - r.left, e.clientY - r.top);
      const f = this.pickFp(x, y);
      this.onPick(f && f.side === this.side ? f.ref : null, e.shiftKey || e.metaKey);
    });
    cv.addEventListener("mouseleave", () => { this.tip.style.display = "none"; if (this.hover) { this.hover = null; this.draw(); } });
    cv.addEventListener("dblclick", () => this.fit(true));
  }
}

// --------------------------------------------------------------------------- the schematic sheet with the parts on it
class MiniSchematic {
  constructor(el, ws, onPick) {
    this.el = el; this.ws = ws; this.pid = ws.pid; this.onPick = onPick;
    this.sheets = []; this.cur = null; this.sel = new Set(); this.vb = null;
    this.svg = document.createElementNS(NS, "svg");
    this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    this.paper = document.createElementNS(NS, "rect");
    this.paper.setAttribute("fill", "#fbfaf6");
    this.img = document.createElementNS(NS, "image");
    this.overlay = document.createElementNS(NS, "g");
    this.svg.append(this.paper, this.img, this.overlay);
    this.sheetBtn = h("button.tbtn.bp-sheet", { onclick: (e) => this.sheetMenu(e.currentTarget), "data-tip": "Choose a sheet" }, h("span", "Schematic"), icon("chevron-down", 11));
    el.append(h("div.bp-head", h("span.bp-title", icon("waypoints", 13)), this.sheetBtn, h("div.grow"),
      h("button.tbtn", { onclick: () => this.fit(true), "data-tip": "Fit the sheet" }, icon("scan", 14)),
      h("button.tbtn", { onclick: () => { this.ws.show("schematic"); if (this.sel.size) this.ws.view("schematic").probe([...this.sel], "bom", true); }, "data-tip": "Open the Schematic tab" }, icon("external-link", 13))),
      h("div.bp-body.paper", this.svg));
    this.body = el.lastChild;
    this.mouse();
    new ResizeObserver(() => this.resize()).observe(this.body);
  }

  shown() { this.resize(); }
  resize() { if (this.want) this.setVB(...this.want, false); }

  async load() {
    if (this.sheets.length && !this.stale) return;
    let d;
    try { d = await api(`/api/projects/${enc(this.pid)}/schematic`); } catch { return; }
    this.stale = false;
    if (d.empty) return;
    this.version = d.version;
    this.sheets = d.sheets;
    if (!this.cur || !this.sheets.find((s) => s.name_path === this.cur)) this.show(this.sheets[0].name_path);
    else this.show(this.cur, true);
    if (this.sel.size) this.setSel([...this.sel], true);
  }

  sheet() { return this.sheets.find((s) => s.name_path === this.cur); }

  show(np, keep) {
    this.cur = np;
    const s = this.sheet();
    if (!s) return;
    let [W, H] = PAPER[s.paper] || [420, 297];
    if (s.portrait) [W, H] = [H, W];
    this.W = W; this.H = H;
    this.paper.setAttribute("width", W); this.paper.setAttribute("height", H);
    this.img.setAttribute("width", W); this.img.setAttribute("height", H);
    this.img.setAttribute("href", `/api/projects/${enc(this.pid)}/schematic/svg?sheet=${enc(np)}&v=${this.version}`);
    clear(this.sheetBtn.firstChild).append(document.createTextNode(np === "/" ? "Root sheet" : s.name));
    this.drawOverlay();
    if (!keep || !this.vb) this.fit(false);
  }

  sheetMenu(anchor) {
    menu(anchor, this.sheets.map((s) => ({ label: s.name_path === "/" ? "Root sheet" : s.name, hint: `${s.symbols.filter((y) => !y.power).length} parts`, checked: s.name_path === this.cur,
      run: () => this.show(s.name_path) })));
  }

  drawOverlay() {
    const s = this.sheet();
    clear(this.overlay);
    if (!s) return;
    for (const sym of s.symbols) {
      if (sym.power) continue;
      const [x0, y0, x1, y1] = sym.bbox;
      const on = this.sel.has(sym.ref);
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", x0 - 0.8); r.setAttribute("y", y0 - 0.8); r.setAttribute("width", x1 - x0 + 1.6); r.setAttribute("height", y1 - y0 + 1.6);
      r.setAttribute("rx", 1);
      r.setAttribute("fill", on ? "rgba(235,138,80,.22)" : "rgba(0,0,0,0)");
      r.setAttribute("stroke", on ? "rgba(214,110,50,1)" : "rgba(0,0,0,0)");
      r.setAttribute("stroke-width", on ? 0.7 : 0.4);
      r.style.cursor = "pointer";
      r.addEventListener("mouseenter", () => { if (!this.sel.has(sym.ref)) r.setAttribute("stroke", "rgba(91,155,248,.55)"); });
      r.addEventListener("mouseleave", () => { if (!this.sel.has(sym.ref)) r.setAttribute("stroke", "rgba(0,0,0,0)"); });
      r.addEventListener("click", (e) => { e.stopPropagation(); if (!this.dragged) this.onPick(sym.ref, e.shiftKey || e.metaKey); });
      const t = document.createElementNS(NS, "title");
      t.textContent = `${sym.ref} ${sym.val || ""}`;
      r.appendChild(t);
      this.overlay.appendChild(r);
    }
  }

  setSel(refs, fly) {
    this.sel = new Set(refs);
    if (!this.sheets.length) return;
    let s = this.sheet();
    if (refs.length && !(s && s.symbols.some((y) => this.sel.has(y.ref)))) {
      const t = this.sheets.find((x) => x.symbols.some((y) => this.sel.has(y.ref)));
      if (t) { this.show(t.name_path, true); s = t; }
    }
    this.drawOverlay();
    if (!fly || !s) return;
    const syms = s.symbols.filter((y) => this.sel.has(y.ref));
    if (!syms.length) return;
    const x0 = Math.min(...syms.map((y) => y.bbox[0])), y0 = Math.min(...syms.map((y) => y.bbox[1]));
    const x1 = Math.max(...syms.map((y) => y.bbox[2])), y1 = Math.max(...syms.map((y) => y.bbox[3]));
    const m = 22;
    this.setVB(x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m, true);
  }

  fit(anim) { if (this.W) this.setVB(-4, -4, this.W + 8, this.H + 8, anim); }

  setVB(x, y, w, hh, anim) {
    this.want = [x, y, w, hh];                       // re-applied when the pane is laid out or resized
    const r = this.body.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) return;
    const asp = r.width / r.height;
    if (w / hh < asp) { const nw = hh * asp; x -= (nw - w) / 2; w = nw; } else { const nh = w / asp; y -= (nh - hh) / 2; hh = nh; }
    const to = [x, y, w, hh];
    if (!anim || !this.vb) { this.vb = to; this.apply(); return; }
    const from = this.vb.slice(), t0 = performance.now();
    const step = () => {
      const k = Math.min(1, (performance.now() - t0) / 360), e = ease(k);
      this.vb = from.map((v, i) => v + (to[i] - v) * e);
      this.apply();
      if (k < 1) requestAnimationFrame(step);
    };
    step();
  }

  apply() { if (this.vb) this.svg.setAttribute("viewBox", this.vb.join(" ")); }

  mouse() {
    const s = this.svg;
    s.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (!this.vb) return;
      const r = s.getBoundingClientRect(), [x, y, w, hh] = this.vb;
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX) && !e.shiftKey) {
        const cx = x + (e.clientX - r.left) / r.width * w, cy = y + (e.clientY - r.top) / r.height * hh;
        const nw = Math.min(Math.max(w * Math.exp(e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)), 6), (this.W || 420) * 3), k = nw / w;
        this.vb = [cx - (cx - x) * k, cy - (cy - y) * k, nw, hh * k];
      } else this.vb = [x + e.deltaX / r.width * w, y + e.deltaY / r.height * hh, w, hh];
      this.apply();
    }, { passive: false });
    let drag = null;
    s.addEventListener("mousedown", (e) => { drag = { x: e.clientX, y: e.clientY, vb: this.vb && this.vb.slice() }; this.dragged = false; });
    addEventListener("mousemove", (e) => {
      if (!drag || !drag.vb) return;
      const r = s.getBoundingClientRect(), dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this.dragged = true;
      if (!this.dragged) return;
      this.vb = [drag.vb[0] - dx / r.width * drag.vb[2], drag.vb[1] - dy / r.height * drag.vb[3], drag.vb[2], drag.vb[3]];
      this.apply();
    });
    addEventListener("mouseup", () => { drag = null; setTimeout(() => { this.dragged = false; }, 0); });
    s.addEventListener("click", (e) => { if (!this.dragged && (e.target === s || e.target === this.img || e.target === this.paper)) this.onPick(null, e.shiftKey); });
    s.addEventListener("dblclick", () => this.fit(true));
  }
}

function roundRect(c, x, y, w, hh, r) {
  c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + hh, r); c.arcTo(x + w, y + hh, x, y + hh, r);
  c.arcTo(x, y + hh, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
}
function esc(s) { return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }

function messageFor(items) {
  return ["Swap these Extended parts for the JLC Basic equivalents below (same value, package, voltage and dielectric; checked in stock).",
    "Update the LCSC and MPN fields everywhere they are defined (the schematic and any design script that generates it), then run the BOM checks:", "",
    ...items.map((it) => `- ${it.refs.join(",")} (${it.value}, ${it.footprint}): ${it.now.lcsc} -> ${it.instead.lcsc} (${it.instead.mpn || ""}, ${(it.instead.describe || "").slice(0, 70)})`)].join("\n");
}
