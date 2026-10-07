// The part inspector. Pick one part anywhere (board, schematic, 3D, BOM) and a card shows what it is,
// what it looks like (LCSC's photo, its footprint), what its pins connect (each net with its kind),
// what it costs, and what the checks say about it. Open shows everything.
import { h, clear, api, toast, modal } from "./util.js";
import { icon } from "./icons.js";
import { netKindText } from "./board.js";
import { editPart } from "./schedit.js";

const enc = encodeURIComponent;
const short = (n) => (n || "").split("/").pop();

export class Inspector {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.el = h("div.inspector", { style: { display: "none" } });
    this.ref = null;
    this.cache = {};
  }

  mount(parent) { parent.appendChild(this.el); }

  async show(ref) {
    if (!ref) return this.hide();
    this.ref = ref;
    this.el.style.display = "";
    const info = this.cache[ref] || await api(`/api/projects/${enc(this.pid)}/parts/${enc(ref)}`).catch(() => null);
    if (this.ref !== ref) return;
    if (!info) { this.hide(); return; }
    this.cache[ref] = info;
    this.render(info);
    if (info.lcsc && !info.lcsc_detail) this.fetchMore(ref);        // the first look at this part: ask LCSC once
  }

  async fetchMore(ref) {
    const info = await api(`/api/projects/${enc(this.pid)}/parts/${enc(ref)}?fetch=1`).catch(() => null);
    if (!info) return;
    this.cache[ref] = info;
    if (this.ref === ref && this.el.style.display !== "none") this.render(info);
  }

  hide() { this.ref = null; this.el.style.display = "none"; }
  invalidate() { this.cache = {}; if (this.ref) { const r = this.ref; this.show(r); } }

  photoURL(info, i = 0) {
    return info.lcsc && info.lcsc_detail && info.lcsc_detail.photos > i
      ? `/api/projects/${enc(this.pid)}/parts/${enc(info.ref)}/photo?lcsc=${enc(info.lcsc)}&i=${i}` : null;
  }

  render(info) {
    const el = clear(this.el);
    const src = info.sourcing || {};
    const photo = this.photoURL(info);
    const sub = [info.mpn, info.manufacturer].filter(Boolean).join(" · ") || info.description || info.footprint.split(":").pop();
    el.append(
      h("div.in-head",
        h("div.in-pic", photo ? h("img", { src: photo, alt: "", onerror: (e) => { e.target.replaceWith(icon("microchip", 22)); } })
          : info.board ? h("div.in-fp", { html: footprintSVG(info.board, 44) }) : icon("microchip", 22)),
        h("div.grow", h("div.in-title", h("b", info.ref), h("span", info.value)), h("div.in-sub.ellipsis", { "data-tip": sub }, sub)),
        h("button.tbtn", { "data-tip": "Close", onclick: () => this.hide() }, icon("x", 13))),
      h("div.in-chips", chips(info, src)),
      info.description && info.description !== sub ? h("div.in-desc", info.description) : null,
      params(info, 4),
      pinsTable(info, 8, (p) => this.goNet(p)),
      findingsLine(info),
      info.placed_why ? h("div.in-why", icon("move", 12), h("span", h("b", "Placed here: "), info.placed_why)) : null,
      this.notesBox(info.ref),
      h("div.in-acts",
        h("button.btn.sm.primary", { onclick: () => this.open(info) }, icon("fullscreen", 13), "Open"),
        info.in_schematic ? h("button.btn.sm", { onclick: () => editPart(this.ws, info), "data-tip": "Its value, footprint, part number, LCSC code; fitted or not" }, icon("pencil", 13), "Edit") : null,
        datasheetButton(this.ws, info, "sm"), this.libraryButton(info)));
  }

  // the part's design notes: the reasoning behind it, kept off the sheet
  notesBox(ref) {
    const box = h("div.in-notes", { style: { display: "none" } });
    api(`/api/projects/${enc(this.pid)}/schematic/notes`).then((r) => {
      const ns = (r.notes || []).filter((n) => n.anchor && n.anchor.ref === ref);
      if (!ns.length || !box.isConnected) return;
      box.style.display = "";
      box.append(...ns.map((n) => h("div.in-note", icon("message-square", 12),
        h("div", n.anchor.pin ? h("b", `Pin ${n.anchor.pin}: `) : null, n.short ? h("b", n.short + " ") : null, n.why || ""))));
    }).catch(() => {});
    return box;
  }

  // keep this part for other projects (My parts)
  libraryButton(info) {
    const b = h("button.btn.sm.ghost", { "data-tip": "Keep its symbol, footprint, 3D model and pin table for other projects", onclick: async () => {
      b.disabled = true;
      try {
        const it = await api(`/api/projects/${enc(this.pid)}/library`, { body: { ref: info.ref } });
        toast(`${it.name} is in My parts`, "ok", 3500, { label: "Open", run: () => { location.hash = "#/library"; } });
        b.textContent = "In My parts";
      } catch (e) { toast(e.message, "error"); b.disabled = false; }
    } }, icon("bookmark", 13), "Save to my library");
    return b;
  }

  goNet(p) {
    if (!p.full) return;
    const b = this.ws.views && this.ws.views.board;
    if (this.ws.show) this.ws.show("board");
    if (b && b.setSpot) setTimeout(() => b.setSpot(p.full, true), 120);
  }

  // ------------------------------------------------------------------ everything, on Open
  open(info) {
    const src = info.sourcing || {};
    const n = (info.lcsc_detail && info.lcsc_detail.photos) || 0;
    let cur = 0;
    const big = h("div.inx-photo");
    const thumbs = h("div.inx-thumbs");
    const showPhoto = (i) => {
      cur = i;
      clear(big).appendChild(n ? h("img", { src: this.photoURL(info, i), alt: `${info.ref} photo ${i + 1}` }) :
        info.board ? h("div.inx-fpbig", { html: footprintSVG(info.board, 260) }) : h("div.inx-none", icon("microchip", 40)));
      [...thumbs.children].forEach((t, j) => t.classList.toggle("on", j === i));
    };
    for (let i = 0; i < n; i++) thumbs.appendChild(h("button.inx-thumb", { onclick: () => showPhoto(i) }, h("img", { src: this.photoURL(info, i), alt: "" })));
    showPhoto(0);
    const where = info.board ? `${info.board.side === "B" ? "Bottom" : "Top"} side, ${info.board.x.toFixed(2)}, ${info.board.y.toFixed(2)} mm, ${Math.round(info.board.rot)}°` : "Not on the board yet";
    const facts = [["Value", info.value], ["Part number", info.mpn], ["Maker", info.manufacturer], ["Package", (info.lcsc_detail && info.lcsc_detail.package) || info.footprint.split(":").pop()],
      ["Footprint", info.footprint], ["LCSC", info.lcsc], ["Sheet", info.sheet], ["Board", where],
      ...Object.entries(info.fields || {}).filter(([k]) => !["MPN", "Manufacturer", "LCSC"].includes(k))];
    const body = [
      h("div.inx",
        h("div.inx-left", big, n > 1 ? thumbs : null,
          info.board && n ? h("div.inx-fp", h("div.inx-cap", "Footprint"), h("div", { html: footprintSVG(info.board, 220) })) : null),
        h("div.inx-right",
          h("div.in-chips", chips(info, src)),
          info.description ? h("p.inx-desc", info.description) : null,
          h("div.inx-sec", "Details"),
          h("div.inx-kv", facts.filter(([, v]) => v).map(([k, v]) => h("div.kv", h("span", k), h("b", String(v))))),
          info.lcsc_detail && Object.keys(info.lcsc_detail.params || {}).length ? h("div.inx-sec", "Parameters (LCSC)") : null,
          params(info, 99),
          h("div.inx-sec", `Pins (${info.pins.length})`),
          pinsTable(info, 999, (p) => { m.close(); this.goNet(p); }),
          info.pin_table ? h("div.inx-sec", `Pin table from the data sheet${info.pin_table.differ ? ` · ${info.pin_table.differ} differ from the symbol` : ""}`) : null,
          info.pin_table ? h("div.small.muted", info.pin_table.source) : null,
          pinTable(info),
          info.findings.length ? h("div.inx-sec", `What the checks say (${info.findings.length})`) : null,
          info.findings.length ? h("div.inx-finds", info.findings.map((f) => h("div.inx-find." + f.severity,
            h("span.sev", f.severity), h("div", h("b", f.message), f.hint ? h("div.hint", f.hint) : null)))) : null)),
    ];
    const m = modal({ title: `${info.ref} · ${info.value}`, sub: [info.mpn, info.manufacturer].filter(Boolean).join(" · "), icon: "microchip", cls: "wide",
      body, actions: [
        datasheetButton(this.ws, info),
        h("button.btn", { onclick: () => { m.close(); this.ws.select([{ ref: info.ref }], "inspector"); if (this.ws.show) this.ws.show("schematic"); } }, "Show in the schematic"),
        info.board ? h("button.btn", { onclick: () => { m.close(); if (this.ws.show) this.ws.show("board"); this.ws.select([{ ref: info.ref }], "inspector"); } }, "Show on the board") : null,
        h("button.btn.primary", { onclick: () => m.close() }, "Done")].filter(Boolean) });
  }
}

// the data sheet: the project's copy when there is one (it opens in Files), else the maker's link with Save
function datasheetButton(ws, info, size = "") {
  const cls = "a.btn" + (size ? "." + size : "");
  if (info.datasheet_saved) return h("button.btn" + (size ? "." + size : ""), { onclick: () => ws.showFile && ws.showFile(info.datasheet_saved),
    "data-tip": info.datasheet_saved }, icon("file-text", 13), "Data sheet");
  if (!info.datasheet || !/^https?:/.test(info.datasheet)) return null;
  const save = h("button.btn" + (size ? "." + size : "") + ".ghost", { "data-tip": "Save it into the project (docs/datasheets)", onclick: async () => {
    save.disabled = true;
    try {
      const r = await api(`/api/projects/${encodeURIComponent(ws.pid)}/datasheets`, { body: { ref: info.ref } });
      info.datasheet_saved = r.saved;
      toast(`Saved ${r.saved}`, "ok", 4000, { label: "Open", run: () => ws.showFile(r.saved) });
      save.replaceWith(h("span.small.muted", "saved"));
    } catch (e) { toast(e.message, "error"); save.disabled = false; }
  } }, icon("download", 13));
  return h("span.in-ds", h(cls, { href: info.datasheet, target: "_blank", rel: "noopener" }, icon("file-text", 13), "Data sheet"), save);
}

// the pin table read from the data sheet, against the symbol's pins
function pinTable(info) {
  const t = info.pin_table;
  if (!t) return null;
  const MARK = { match: ["check", "same"], unknown: ["circle", "no name to compare"], mismatch: ["triangle-alert", "named differently"],
    critical: ["circle-x", "a different pin"], missing: ["circle-dot", "only on one side"] };
  return h("div.in-ptab", t.rows.map((r) => h("div.in-prow." + r.match, { "data-tip": (MARK[r.match] || MARK.unknown)[1] },
    h("span.pn", r.pin), h("span.ellipsis", r.sheet || "—"), h("span.ellipsis.sym", r.symbol || "—"), icon((MARK[r.match] || MARK.unknown)[0], 12))));
}

function chips(info, src) {
  const out = [];
  if (info.dnp) out.push(h("span.in-chip.warn", "DNP"));
  if (src.lib) out.push(h("span.in-chip" + (src.lib === "Basic" ? ".ok" : ""), src.lib));
  const stock = src.jlc_stock ?? src.lcsc_stock;
  if (stock != null) out.push(h("span.in-chip" + (stock > 1000 ? ".ok" : stock > 0 ? ".warn" : ".err"), `${Number(stock).toLocaleString()} in stock`));
  if (src.price != null) out.push(h("span.in-chip", `$${Number(src.price).toFixed(src.price < 1 ? 3 : 2)}`));
  if (src.discontinued) out.push(h("span.in-chip.err", "Discontinued"));
  if (info.lcsc) out.push(h("span.in-chip.mono", info.lcsc));
  return out;
}

function params(info, limit) {
  const p = (info.lcsc_detail && info.lcsc_detail.params) || {};
  const rows = Object.entries(p).filter(([, v]) => v && v !== "-").slice(0, limit);
  if (!rows.length) return null;
  return h("div.in-params", rows.map(([k, v]) => h("div.kv", h("span", k), h("b", v))));
}

function pinsTable(info, limit, onNet) {
  const pins = info.pins || [];
  if (!pins.length) return null;
  const rows = pins.slice(0, limit).map((p) => h("div.in-pin" + (p.full ? ".link" : ""), { onclick: () => p.full && onNet(p),
    "data-tip": p.full ? `Show ${short(p.full)} on the board` : null },
    h("span.pn", p.pin), h("span.nm.ellipsis", pinName(p.name)), h("span.nt.ellipsis" + (p.net ? "" : ".none"), p.net || (p.kind === "unconnected" ? "not connected" : "—")),
    p.tag ? h("span.ntag." + p.kind, { "data-tip": netKindText({ kind: p.kind }) }, p.tag) : h("span")));
  return h("div.in-pins", rows, pins.length > limit ? h("div.in-more", `${pins.length - limit} more pins`) : null);
}

// KiCad's pin-name markup: ~{RESET} is RESET with a bar over it (active low).
function pinName(s) {
  const out = h("span");
  const re = /~\{([^}]*)\}/g;
  let last = 0, m;
  while ((m = re.exec(s || ""))) {
    if (m.index > last) out.append(s.slice(last, m.index));
    out.append(h("span.ovl", m[1]));
    last = m.index + m[0].length;
  }
  if (last < (s || "").length) out.append(s.slice(last));
  return out;
}

function findingsLine(info) {
  const f = info.findings || [];
  const bad = f.filter((x) => x.severity === "error" || x.severity === "warning");
  if (!bad.length) return null;
  return h("div.in-find." + bad[0].severity, icon("triangle-alert", 13), h("span.ellipsis", bad[0].message),
    bad.length > 1 ? h("span.more", `+${bad.length - 1}`) : null);
}

// The footprint as drawn on the board: pads (pin 1 marked), silkscreen and courtyard, fitted to `px`.
export function footprintSVG(b, px) {
  const pts = [];
  for (const p of b.pads) for (const pl of p.p) for (const q of pl) pts.push(q);
  for (const l of b.cy || []) for (const q of l) pts.push(q);
  if (!pts.length) return "";
  const x0 = Math.min(...pts.map((q) => q[0])), y0 = Math.min(...pts.map((q) => q[1]));
  const x1 = Math.max(...pts.map((q) => q[0])), y1 = Math.max(...pts.map((q) => q[1]));
  const m = Math.max(x1 - x0, y1 - y0) * 0.08 + 0.3;
  const vb = [x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m];
  const path = (pl) => "M" + pl.map((q) => `${q[0].toFixed(3)} ${q[1].toFixed(3)}`).join("L") + "Z";
  const sw = Math.max(vb[2], vb[3]) / px;
  let s = "";
  for (const l of b.cy || []) s += `<path class="fp-cy" d="${"M" + l.map((q) => `${q[0].toFixed(3)} ${q[1].toFixed(3)}`).join("L")}" stroke-width="${(sw * 1).toFixed(4)}"/>`;
  for (const g of b.g || []) {
    if (!/SilkS/.test(g.l) || !g.p) continue;
    s += `<path class="fp-silk" d="${"M" + g.p.map((q) => `${q[0].toFixed(3)} ${q[1].toFixed(3)}`).join("L")}" stroke-width="${Math.max(g.w || 0.12, sw * 1.2).toFixed(4)}"/>`;
  }
  for (const p of b.pads) {
    const one = p.n === "1" || p.n === "A1";
    for (const pl of p.p) s += `<path class="fp-pad${one ? " one" : ""}${p.k === "thru_hole" || p.d ? " th" : ""}" d="${path(pl)}"/>`;
  }
  return `<svg viewBox="${vb.map((v) => v.toFixed(3)).join(" ")}" width="${px}" height="${Math.round(px * vb[3] / vb[2])}" style="max-width:100%;height:auto">${s}</svg>`;
}
