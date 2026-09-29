// The floorplan: the board to scale before the schematic exists -- the outline and its size, the mounting
// holes, each connector on its edge, the main blocks where they go, keep-outs -- drawn from the canvas's
// floorplan section (canvas.py). Everything can be dragged: blocks and holes anywhere on the board, connectors
// along the edges (onto another edge too), the board's corner to resize it. Each drop is saved and told to
// Claude (PATCH .../canvas/floorplan). Millimetres from the board's top-left corner, y down.
import { h, api, toast } from "./util.js";

const NS = "http://www.w3.org/2000/svg";
const enc = encodeURIComponent;
const snap = (v, q = 0.5) => Math.round(v / q) * q;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmt = (v) => (Math.round(v * 10) / 10).toString();

function el(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  for (const k of kids) if (k != null) e.appendChild(typeof k === "string" ? document.createTextNode(k) : k);
  return e;
}

// a connector's rectangle on the board: w along its edge, h into the board, its outer side flush with the edge
export function connectorRect(it, W, H) {
  const w = it.w || 8, d = it.h || 6, at = it.at ?? (it.edge === "left" || it.edge === "right" ? H / 2 : W / 2);
  if (it.edge === "left") return [0, at - w / 2, d, w];
  if (it.edge === "right") return [W - d, at - w / 2, d, w];
  if (it.edge === "top") return [at - w / 2, 0, w, d];
  return [at - w / 2, H - d, w, d];
}
function blockRect(it) { return [it.x - it.w / 2, it.y - it.h / 2, it.w, it.h]; }
function overlaps(a, b) { return a[0] < b[0] + b[2] - 0.01 && b[0] < a[0] + a[2] - 0.01 && a[1] < b[1] + b[3] - 0.01 && b[1] < a[1] + a[3] - 0.01; }

export class FloorplanView {
  // opts: {pid, editable, maxSize: [a, b] | null, width, maxHeight}
  constructor(fp, opts = {}) {
    this.opts = { editable: true, width: 520, maxHeight: 330, ...opts };
    this.el = h("div.fp");
    this.el.__fp = this;                             // for debugging from the console, and the browser tests
    this.tip = h("div.fp-tip", { style: { display: "none" } });
    this.el.appendChild(this.tip);
    this.set(fp);
  }

  set(fp) {
    if (this.drag) { this.pendingFp = fp; return; }             // a drag is under way: draw it once it is dropped
    this.fp = JSON.parse(JSON.stringify(fp));
    this.draw();
  }

  resize(width) { if (Math.abs(width - this.opts.width) > 12) { this.opts.width = width; this.draw(); } }

  draw() {
    const fp = this.fp, { width, maxHeight } = this.opts;
    const W = fp.board.w, H = fp.board.h;
    const ML = 78, MR = 78, MT = 30, MB = 34;
    const S = this.S = this.fixedS || Math.max(1, Math.min((width - ML - MR) / W, (maxHeight - MT - MB) / H));
    this.ox = ML; this.oy = MT;
    const vw = W * S + ML + MR, vh = H * S + MT + MB;
    const X = (x) => this.ox + x * S, Y = (y) => this.oy + y * S;
    const svg = this.svg = el("svg", { viewBox: `0 0 ${vw} ${vh}`, width: vw, height: vh, class: "fp-svg" });
    if (this.fixedS && this.renderK) {                // sizing the board: the drawing keeps its scale and its place
      svg.style.width = vw * this.renderK + "px"; svg.style.maxWidth = "none"; svg.style.height = vh * this.renderK + "px";
      svg.style.margin = "0"; svg.style.marginLeft = this.renderLeft + "px";
    }
    // the grid, every 5 mm
    const grid = el("g", { class: "fp-grid" });
    for (let x = 5; x < W; x += 5) grid.appendChild(el("line", { x1: X(x), y1: Y(0), x2: X(x), y2: Y(H), class: x % 10 ? "" : "m" }));
    for (let y = 5; y < H; y += 5) grid.appendChild(el("line", { x1: X(0), y1: Y(y), x2: X(W), y2: Y(y), class: y % 10 ? "" : "m" }));
    // the largest board the requirements allow, when they set one (in the board's own orientation)
    const mx = this.opts.maxSize;
    if (mx && mx.length === 2) {
      const [a, b] = (W >= H) === (mx[0] >= mx[1]) ? mx : [mx[1], mx[0]];
      svg.appendChild(el("rect", { x: X(0), y: Y(0), width: a * S, height: b * S, class: "fp-max" + (W > a + 0.01 || H > b + 0.01 ? " over" : "") }));
      svg.appendChild(el("text", { x: X(a) - 4, y: Y(b) - 5, class: "fp-maxl" }, `largest allowed ${fmt(a)} × ${fmt(b)}`));
    }
    const r = Math.min(fp.board.radius || 0, W / 2, H / 2) * S;
    svg.appendChild(el("rect", { x: X(0), y: Y(0), width: W * S, height: H * S, rx: r, class: "fp-board" }));
    svg.appendChild(grid);
    // dimensions
    svg.appendChild(el("text", { x: X(W / 2), y: Y(H) + 24, class: "fp-dim" }, `${fmt(W)} mm`));
    // the height beside the board, out past any connector's name on that side
    const rightNames = (fp.items || []).some((it) => it.edge === "right");
    const hx = rightNames ? X(W) + MR - 10 : X(W) + 22;
    svg.appendChild(el("text", { x: hx, y: Y(H / 2), class: "fp-dim v", transform: `rotate(90 ${hx} ${Y(H / 2)})` }, `${fmt(H)} mm`));
    // keep-outs
    for (const k of fp.keepouts || []) {
      const g = el("g", { class: "fp-ko" }, el("rect", { x: X(k.x - k.w / 2), y: Y(k.y - k.h / 2), width: k.w * S, height: k.h * S }));
      if (k.label && k.w * S > 40) g.appendChild(el("text", { x: X(k.x), y: Y(k.y) + 4 }, k.label));
      svg.appendChild(g);
    }
    // blocks, then connectors, then holes on top
    const rects = [];
    const blocks = (fp.items || []).filter((it) => !it.edge), conns = (fp.items || []).filter((it) => it.edge);
    for (const it of blocks) rects.push([it, blockRect(it)]);
    for (const it of conns) rects.push([it, connectorRect(it, W, H)]);
    const bad = new Set();
    for (let i = 0; i < rects.length; i++) {
      const [ia, ra] = rects[i];
      if (ra[0] < -0.01 || ra[1] < -0.01 || ra[0] + ra[2] > W + 0.01 || ra[1] + ra[3] > H + 0.01) bad.add(ia.id);
      for (let j = i + 1; j < rects.length; j++) if (overlaps(ra, rects[j][1])) { bad.add(ia.id); bad.add(rects[j][0].id); }
    }
    this.bad = bad;
    for (const [it, rc] of rects) svg.appendChild(this.item(it, rc, bad.has(it.id)));
    for (const ho of fp.holes || []) svg.appendChild(this.hole(ho));
    // the corner handle: drag to size the board
    if (this.opts.editable) {
      const hd = el("rect", { x: X(W) - 5, y: Y(H) - 5, width: 10, height: 10, rx: 2, class: "fp-corner" });
      hd.appendChild(el("title", {}, "Drag to size the board"));
      this.dragBy(hd, { kind: "board" });
      svg.appendChild(hd);
    }
    const old = this.el.querySelector("svg.fp-svg");
    if (old) old.replaceWith(svg); else this.el.insertBefore(svg, this.tip);
    this.el.classList.toggle("editable", !!this.opts.editable);
  }

  item(it, [x, y, w, hh], bad) {
    const S = this.S, X = (v) => this.ox + v * S, Y = (v) => this.oy + v * S;
    const cls = "fp-item " + (it.edge ? "conn" : "blk " + (it.kind || "other")) + (bad ? " bad" : "") + (it.moved ? " moved" : "");
    const g = el("g", { class: cls, "data-id": it.id });
    g.appendChild(el("rect", { x: X(x), y: Y(y), width: w * S, height: hh * S, rx: it.edge ? 1.5 : 4 }));
    const name = it.edge ? [it.ref, it.label].filter(Boolean).join(" ") : it.label || it.id;
    if (it.edge) {
      // the opening: a heavier stroke on the board edge it faces
      const W = this.fp.board.w, H = this.fp.board.h;
      const seg = it.edge === "left" ? [0, y, 0, y + hh] : it.edge === "right" ? [W, y, W, y + hh] : it.edge === "top" ? [x, 0, x + w, 0] : [x, H, x + w, H];
      g.appendChild(el("line", { x1: X(seg[0]), y1: Y(seg[1]), x2: X(seg[2]), y2: Y(seg[3]), class: "fp-mouth" }));
      const lx = it.edge === "left" ? X(0) - 8 : it.edge === "right" ? X(W) + 8 : X(x + w / 2);
      const ly = it.edge === "top" ? Y(0) - 8 : it.edge === "bottom" ? Y(H) + 15 : Y(y + hh / 2) + 4;
      g.appendChild(el("text", { x: lx, y: ly, class: "fp-cl", "text-anchor": it.edge === "left" ? "end" : it.edge === "right" ? "start" : "middle" }, name.slice(0, 18)));
    } else {
      const fits = w * S > 34 && hh * S > 16;
      if (fits) {
        const max = Math.max(3, Math.floor(w * S / 6.6));
        g.appendChild(el("text", { x: X(x + w / 2), y: Y(y + hh / 2) + (it.note && hh * S > 34 ? -2 : 4), class: "fp-bl" }, name.length > max ? name.slice(0, max - 1) + "…" : name));
        if (it.note && hh * S > 34) g.appendChild(el("text", { x: X(x + w / 2), y: Y(y + hh / 2) + 12, class: "fp-bn" }, it.note.slice(0, Math.max(4, Math.floor(w * S / 5.6)))));
      }
    }
    g.appendChild(el("title", {}, `${name}${it.note ? " — " + it.note : ""}\n${fmt(it.w)} × ${fmt(it.h)} mm${it.moved ? "\nPlaced by you" : ""}${bad ? "\nOverlaps something or runs off the board" : ""}`));
    if (this.opts.editable) this.dragBy(g, { kind: it.edge ? "conn" : "blk", it });
    return g;
  }

  hole(ho) {
    const S = this.S, X = (v) => this.ox + v * S, Y = (v) => this.oy + v * S;
    const g = el("g", { class: "fp-hole" + (ho.moved ? " moved" : ""), "data-id": ho.id },
      el("circle", { cx: X(ho.x), cy: Y(ho.y), r: (ho.d / 2 + 1) * S, class: "ring" }),
      el("circle", { cx: X(ho.x), cy: Y(ho.y), r: (ho.d / 2) * S }));
    g.appendChild(el("title", {}, `${ho.ref || "Hole"}: ${fmt(ho.d)} mm at ${fmt(ho.x)}, ${fmt(ho.y)}`));
    if (this.opts.editable) this.dragBy(g, { kind: "hole", it: ho });
    return g;
  }

  // ------------------------------------------------------------------ dragging
  // The pointer is followed on the document for the whole drag (each step redraws the plan, replacing the node
  // that was grabbed); while the board's corner is dragged the scale holds still.
  dragBy(node, what) {
    node.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || this.drag) return;
      e.preventDefault(); e.stopPropagation();
      const d = this.drag = { what, p0: this.toMM(e), moved: false, start: what.it ? { ...what.it } : { ...this.fp.board } };
      if (what.kind === "board") {
        this.fixedS = this.S;
        const r = this.svg.getBoundingClientRect();
        this.renderK = r.width / this.svg.viewBox.baseVal.width;
        this.renderLeft = r.left - this.el.getBoundingClientRect().left;
      }
      this.el.classList.add("dragging");
      const move = (ev) => {
        const p = this.toMM(ev), dx = p[0] - d.p0[0], dy = p[1] - d.p0[1];
        if (!d.moved && Math.hypot(dx * this.S, dy * this.S) < 3) return;
        d.moved = true;
        this.preview(d, p, dx, dy);
      };
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        this.drag = null; this.fixedS = null; this.renderK = null;
        this.el.classList.remove("dragging");
        this.tip.style.display = "none";
        if (d.moved) this.drop(d);
        if (this.pendingFp) { const f = this.pendingFp; this.pendingFp = null; this.set(f); } else this.draw();
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    });
  }

  toMM(e) {
    const r = this.svg.getBoundingClientRect(), vb = this.svg.viewBox.baseVal;
    const sx = vb.width / (r.width || 1), sy = vb.height / (r.height || 1);
    return [((e.clientX - r.left) * sx - this.ox) / this.S, ((e.clientY - r.top) * sy - this.oy) / this.S];
  }

  // where the dragged thing would land, drawn as it goes
  preview(d, p, dx, dy) {
    const fp = this.fp, W = fp.board.w, H = fp.board.h, it = d.what.it;
    let label;
    if (d.what.kind === "board") {
      fp.board.w = clamp(snap(d.start.w + dx), 10, 500); fp.board.h = clamp(snap(d.start.h + dy), 10, 500);
      // holes keep their distance from their nearest corner (corner holes stay in the corners)
      d.holes = d.holes || (fp.holes || []).map((o) => ({ ...o }));
      for (const [o, was] of (fp.holes || []).map((o, i) => [o, d.holes[i]])) {
        o.x = clamp(was.x > d.start.w / 2 ? fp.board.w - (d.start.w - was.x) : was.x, o.d / 2, fp.board.w - o.d / 2);
        o.y = clamp(was.y > d.start.h / 2 ? fp.board.h - (d.start.h - was.y) : was.y, o.d / 2, fp.board.h - o.d / 2);
      }
      label = `${fmt(fp.board.w)} × ${fmt(fp.board.h)} mm`;
    } else if (d.what.kind === "conn") {
      const dist = { left: p[0], right: W - p[0], top: p[1], bottom: H - p[1] };      // the nearest edge, and how far along
      const edge = Object.entries(dist).sort((a, b) => a[1] - b[1])[0][0];
      const span = edge === "left" || edge === "right" ? H : W, along = edge === "left" || edge === "right" ? p[1] : p[0];
      it.edge = edge; it.at = clamp(snap(along), Math.min(it.w / 2, span / 2), Math.max(span - it.w / 2, span / 2));
      label = `${edge} edge · ${fmt(it.at)} mm`;
    } else {
      const m = d.what.kind === "hole" ? it.d / 2 : 0;
      it.x = clamp(snap(d.start.x + dx), m, W - m); it.y = clamp(snap(d.start.y + dy), m, H - m);
      label = `x ${fmt(it.x)}  y ${fmt(it.y)} mm`;
    }
    this.draw();
    this.tip.textContent = label;
    this.tip.style.display = "";
    const r = this.svg.getBoundingClientRect(), vb = this.svg.viewBox.baseVal, er = this.el.getBoundingClientRect();
    this.tip.style.left = (r.left - er.left + (this.ox + p[0] * this.S) * r.width / vb.width + 12) + "px";
    this.tip.style.top = (r.top - er.top + (this.oy + p[1] * this.S) * r.height / vb.height - 28) + "px";
  }

  async drop(d) {
    const it = d.what.it;
    const body = d.what.kind === "board" ? { board: { w: this.fp.board.w, h: this.fp.board.h }, holes: (this.fp.holes || []).map((o) => ({ id: o.id, x: o.x, y: o.y })) }
      : d.what.kind === "conn" ? { id: it.id, edge: it.edge, at: it.at } : { id: it.id, x: it.x, y: it.y };
    if (it) it.moved = true;
    if (!this.opts.pid) return;
    try {
      const r = await api(`/api/projects/${enc(this.opts.pid)}/canvas/floorplan`, { method: "PATCH", body });
      if (r.floorplan) this.set(r.floorplan);
    } catch (e) {
      toast(e.message, "error");
      if (it) Object.assign(it, d.start); else Object.assign(this.fp.board, d.start);
      this.draw();
    }
  }
}
