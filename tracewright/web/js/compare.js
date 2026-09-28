// Compare versions: the board at a checkpoint against the board now. Parts that moved show a ghost of
// where they were and an arrow to where they are; copper that is new is green, copper that went is red,
// the rest steps back; the numbers say what changed. Before / Changes / After switch the picture.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { Film, partShapes } from "./timelapse.js";

const enc = encodeURIComponent;
const GREEN = "rgba(67,194,131,.95)", RED = "rgba(240,101,96,.95)", BLUE = "rgba(99,164,248,.95)";

export class Compare {
  // from: a checkpoint {hash, message, date}; to: "now"
  constructor(ws, from) { this.ws = ws; this.pid = ws.pid; this.from = from; this.mode = "changes"; }

  async open() {
    this.canvas = h("canvas.tl-canvas");
    this.film = new Film(this.canvas, [14, 60, 14, 14]);
    this.statsEl = h("div.cmp-stats");
    this.modeEl = h("div.seg.cmp-mode", [["before", "Before"], ["changes", "Changes"], ["after", "Now"]].map(([v, l]) =>
      h("button" + (v === this.mode ? ".on" : ""), { onclick: (e) => { this.mode = v; for (const b of this.modeEl.children) b.classList.toggle("on", b === e.currentTarget); this.draw(); } }, l)));
    this.el = h("div.tl-overlay", { onclick: (e) => { if (e.target === this.el) this.close(); } },
      h("div.tl-win",
        h("div.tl-head", h("div.tl-title", icon("history", 16), h("b", "Compare versions"),
          h("span.muted.ellipsis", `${this.from.hash} · ${this.from.message}`)), h("div.grow"), this.modeEl,
          btn("x", null, { onclick: () => this.close(), "data-tip": "Close", "data-kbd": "esc" }, "sm ghost")),
        h("div.tl-stage", this.canvas, this.statsEl, h("div.cmp-legend", key(GREEN, "added"), key(RED, "removed"), key(BLUE, "moved")))));
    document.body.appendChild(this.el);
    requestAnimationFrame(() => this.el.classList.add("on"));
    this.onKey = (e) => { if (e.key === "Escape") this.close(); else if (e.key === "1") this.setMode("before"); else if (e.key === "2") this.setMode("changes"); else if (e.key === "3") this.setMode("after"); };
    addEventListener("keydown", this.onKey, true);
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(this.canvas);
    try {
      const [a, b, board] = await Promise.all([api(`/api/projects/${enc(this.pid)}/board-at/${enc(this.from.hash)}`),
        api(`/api/projects/${enc(this.pid)}/board-at/now`), api(`/api/projects/${enc(this.pid)}/board`)]);
      this.before = a.picture; this.after = b.picture;
      this.film.setShapes(partShapes(board));
      this.diff = diff(this.before, this.after);
      this.film.fit({ ...this.after, ol: this.after.ol.length ? this.after.ol : this.before.ol });
      this.stats();
      this.draw();
    } catch (e) { toast(e.message, "error"); this.close(); }
  }

  setMode(m) { this.mode = m; for (const b of this.modeEl.children) b.classList.toggle("on", b.textContent === { before: "Before", changes: "Changes", after: "Now" }[m]); this.draw(); }

  close() {
    if (!this.el) return;
    removeEventListener("keydown", this.onKey, true);
    this.ro.disconnect();
    this.el.classList.remove("on");
    const el = this.el; this.el = null;
    setTimeout(() => el.remove(), 180);
  }

  draw() {
    if (!this.after || !this.el) return;
    if (this.mode === "before") { this.film.draw(this.before); return; }
    if (this.mode === "after") { this.film.draw(this.after); return; }
    this.film.draw(this.after, { dim: 0.28 });
    const { ox, oy, k } = this.film.xf, c = this.film.ctx, d = this.diff;
    const X = (x) => ox + x * k, Y = (y) => oy + y * k;
    c.lineCap = "round";
    const seg = (t, col, w) => { c.strokeStyle = col; c.lineWidth = Math.max(1.4, t[4] * k * w); c.beginPath(); c.moveTo(X(t[0]), Y(t[1])); c.lineTo(X(t[2]), Y(t[3])); c.stroke(); };
    for (const t of d.tracksGone) seg(t, RED, 1);
    for (const t of d.tracksNew) seg(t, GREEN, 1.1);
    for (const v of d.viasGone) ring(c, X(v[0]), Y(v[1]), Math.max(2, v[2] / 2 * k), RED);
    for (const v of d.viasNew) ring(c, X(v[0]), Y(v[1]), Math.max(2, v[2] / 2 * k), GREEN);
    c.font = `600 ${Math.max(10, Math.min(13, k * 1.1))}px -apple-system, system-ui, sans-serif`; c.textAlign = "center"; c.textBaseline = "bottom";
    for (const [ref, p0, p1] of d.moved) {
      const x0 = X(p0[0]), y0 = Y(p0[1]), x1 = X(p1[0]), y1 = Y(p1[1]);
      c.setLineDash([4, 4]); c.strokeStyle = "rgba(99,164,248,.55)"; c.lineWidth = 1.2;
      c.beginPath(); c.arc(x0, y0, Math.max(4, 0.9 * k), 0, 7); c.stroke(); c.setLineDash([]);
      arrow(c, x0, y0, x1, y1, BLUE);
      c.fillStyle = BLUE; c.fillText(ref, x1, y1 - 6);
    }
    for (const [ref, p] of d.partsNew) { c.fillStyle = GREEN; ring(c, X(p[0]), Y(p[1]), Math.max(5, 1.2 * k), GREEN); c.fillText(ref, X(p[0]), Y(p[1]) - 8); }
    for (const [ref, p] of d.partsGone) { c.fillStyle = RED; ring(c, X(p[0]), Y(p[1]), Math.max(5, 1.2 * k), RED); c.fillText(ref, X(p[0]), Y(p[1]) - 8); }
  }

  stats() {
    const d = this.diff, a = this.before.stats || {}, b = this.after.stats || {};
    const ch = (x, y, unit = "") => { const n = (y || 0) - (x || 0); return n ? `${n > 0 ? "+" : "−"}${Math.abs(n)}${unit} since then` : "unchanged"; };
    clear(this.statsEl).append(
      stat("Parts moved", String(d.moved.length), d.partsNew.length || d.partsGone.length ? `${d.partsNew.length} added, ${d.partsGone.length} removed` : "none added or removed"),
      stat("Tracks", String(b.tracks ?? 0), `+${d.tracksNew.length} / −${d.tracksGone.length} segments`),
      stat("Vias", String(b.vias ?? 0), `+${d.viasNew.length} / −${d.viasGone.length}`),
      stat("Copper", `${b.len ?? 0} mm`, ch(a.len, b.len, " mm")),
      stat("Nets with copper", `${b.routed ?? 0} of ${b.nets ?? 0}`, ch(a.routed, b.routed)));
  }
}

// what changed between two pictures (tracks and vias by position, since a moved segment is new copper)
export function diff(a, b) {
  const sig = (t) => t.slice(0, 6).map((v) => typeof v === "number" ? v.toFixed(2) : v).join(",");
  const ta = new Map(Object.values(a.tr || {}).map((t) => [sig(t), t])), tb = new Map(Object.values(b.tr || {}).map((t) => [sig(t), t]));
  const vsig = (v) => `${v[0].toFixed(2)},${v[1].toFixed(2)}`;
  const va = new Map(Object.values(a.vi || {}).map((v) => [vsig(v), v])), vb = new Map(Object.values(b.vi || {}).map((v) => [vsig(v), v]));
  const moved = [], partsNew = [], partsGone = [];
  for (const [ref, p] of Object.entries(b.fp || {})) {
    const q = (a.fp || {})[ref];
    if (!q) partsNew.push([ref, p]);
    else if (Math.hypot(p[0] - q[0], p[1] - q[1]) > 0.05 || Math.abs(((p[2] - q[2]) % 360 + 360) % 360) > 0.5 || p[3] !== q[3]) moved.push([ref, q, p]);
  }
  for (const [ref, q] of Object.entries(a.fp || {})) if (!(b.fp || {})[ref]) partsGone.push([ref, q]);
  return {
    moved, partsNew, partsGone,
    tracksNew: [...tb].filter(([k]) => !ta.has(k)).map(([, t]) => t), tracksGone: [...ta].filter(([k]) => !tb.has(k)).map(([, t]) => t),
    viasNew: [...vb].filter(([k]) => !va.has(k)).map(([, v]) => v), viasGone: [...va].filter(([k]) => !vb.has(k)).map(([, v]) => v),
  };
}

function ring(c, x, y, r, col) { c.strokeStyle = col; c.lineWidth = 1.8; c.beginPath(); c.arc(x, y, r, 0, 7); c.stroke(); }
function arrow(c, x0, y0, x1, y1, col) {
  const d = Math.hypot(x1 - x0, y1 - y0);
  if (d < 3) return;
  const a = Math.atan2(y1 - y0, x1 - x0), hl = Math.min(9, d * 0.4);
  c.strokeStyle = col; c.fillStyle = col; c.lineWidth = 1.6;
  c.beginPath(); c.moveTo(x0, y0); c.lineTo(x1 - Math.cos(a) * hl * 0.6, y1 - Math.sin(a) * hl * 0.6); c.stroke();
  c.beginPath(); c.moveTo(x1, y1); c.lineTo(x1 - Math.cos(a - 0.45) * hl, y1 - Math.sin(a - 0.45) * hl); c.lineTo(x1 - Math.cos(a + 0.45) * hl, y1 - Math.sin(a + 0.45) * hl); c.closePath(); c.fill();
}
function stat(label, value, sub) { return h("div.tl-stat", h("span", label), h("b", value), sub ? h("small.cmp-sub", sub) : null); }
function key(col, label) { return h("span", h("i", { style: { background: col } }), label); }
