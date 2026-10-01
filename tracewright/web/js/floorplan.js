// The floorplan: the board to scale before the schematic exists -- the outline and its size, the mounting
// holes, each connector on its edge, the main blocks where they go, keep-outs -- drawn from the canvas's
// floorplan section (canvas.py). Everything can be dragged: blocks, keep-outs and holes anywhere on the board,
// connectors along the edges (onto another edge too), the board's corner to resize it. A click selects: R turns
// it a quarter (a connector to face the next edge), L locks it (Claude keeps it where it is), the arrow keys
// nudge it (Shift: 5 mm). Each change is saved and told to Claude (PATCH .../canvas/floorplan). Millimetres
// from the board's top-left corner, y down.
import { h, api, toast } from "./util.js";
import { icon } from "./icons.js";

const NS = "http://www.w3.org/2000/svg";
const enc = encodeURIComponent;
const snap = (v, q = 0.5) => Math.round(v / q) * q;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmt = (v) => (Math.round(v * 10) / 10).toString();
const NEXT_EDGE = { top: "right", right: "bottom", bottom: "left", left: "top" };

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
// a block's (or keep-out's) rectangle, turned a quarter when its rotation says so
function blockRect(it) {
  const q = (it.rot || 0) % 180 === 90, w = q ? it.h : it.w, hh = q ? it.w : it.h;
  return [it.x - w / 2, it.y - hh / 2, w, hh];
}
function overlaps(a, b) { return a[0] < b[0] + b[2] - 0.01 && b[0] < a[0] + a[2] - 0.01 && a[1] < b[1] + b[3] - 0.01 && b[1] < a[1] + a[3] - 0.01; }

export class FloorplanView {
  // opts: {pid, editable, maxSize: [a, b] | null, width, maxHeight}
  constructor(fp, opts = {}) {
    this.opts = { editable: true, width: 520, maxHeight: 330, ...opts };
    this.el = h("div.fp", { tabindex: "-1" });
    this.el.__fp = this;                             // for debugging from the console, and the browser tests
    this.tip = h("div.fp-tip", { style: { display: "none" } });
    this.bar = h("div.fp-bar", { style: { display: "none" } });
    this.el.append(this.tip, this.bar);
    this.selId = null;
    this.proposal = null; this.take = new Set();          // Claude's suggested layout, and the moves the user would take
    this.el.addEventListener("keydown", (e) => this.key(e));
    this.set(fp);
  }

  // Claude's suggestion, drawn as ghosts: {moves: [{id, x, y | edge, at, rot, why}]}; onTake(set) when the user
  // picks a ghost in or out
  setProposal(pr, onTake) {
    const ids = ((pr && pr.moves) || []).map((m) => m.id);
    const same = this.proposal && JSON.stringify(this.proposal.moves) === JSON.stringify(pr && pr.moves);
    this.proposal = pr && pr.moves && pr.moves.length ? pr : null;
    if (!same) this.take = new Set(ids);
    this.onTake = onTake || null;
    this.draw();
  }

  set(fp) {
    if (this.drag) { this.pendingFp = fp; return; }             // a drag is under way: draw it once it is dropped
    this.fp = JSON.parse(JSON.stringify(fp));
    (this.fp.keepouts || []).forEach((k, i) => { if (!k.id) k.id = `K${i + 1}`; });     // saved before keep-outs had ids
    if (this.selId && !this.find(this.selId)) this.selId = null;
    this.draw();
  }

  resize(width) { if (Math.abs(width - this.opts.width) > 12) { this.opts.width = width; this.draw(); } }

  // everything that can be picked, by id
  find(id) {
    const fp = this.fp;
    for (const it of fp.items || []) if (it.id === id) return { kind: it.edge ? "conn" : "blk", it };
    for (const it of fp.holes || []) if (it.id === id) return { kind: "hole", it };
    for (const it of fp.keepouts || []) if (it.id === id) return { kind: "ko", it };
    return null;
  }

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
    const boardRect = el("rect", { x: X(0), y: Y(0), width: W * S, height: H * S, rx: r, class: "fp-board" });
    boardRect.addEventListener("pointerdown", (e) => { if (e.button === 0 && !this.drag) this.select(null); });
    svg.appendChild(boardRect);
    svg.appendChild(grid);
    // dimensions
    svg.appendChild(el("text", { x: X(W / 2), y: Y(H) + 24, class: "fp-dim" }, `${fmt(W)} mm`));
    // the height beside the board, out past any connector's name on that side
    const rightNames = (fp.items || []).some((it) => it.edge === "right");
    const hx = rightNames ? X(W) + MR - 10 : X(W) + 22;
    svg.appendChild(el("text", { x: hx, y: Y(H / 2), class: "fp-dim v", transform: `rotate(90 ${hx} ${Y(H / 2)})` }, `${fmt(H)} mm`));
    // keep-outs first (areas), then blocks largest first so a small one is never buried, connectors, holes on top
    for (const k of fp.keepouts || []) svg.appendChild(this.keepout(k));
    const rects = [];
    const area = (it) => it.w * it.h;
    const blocks = (fp.items || []).filter((it) => !it.edge).sort((a, b) => area(b) - area(a));
    const conns = (fp.items || []).filter((it) => it.edge);
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
    if (this.proposal) svg.appendChild(this.ghosts(W, H));
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
    this.renderBar();
  }

  // the suggested layout: each move's outline where it would go, an arrow from where it is, its number
  ghosts(W, H) {
    const S = this.S, X = (x) => this.ox + x * S, Y = (y) => this.oy + y * S;
    const g = el("g", { class: "fp-ghosts" });
    this.proposal.moves.forEach((m, i) => {
      const f = this.find(m.id);
      if (!f) return;
      const it = f.it, on = this.take.has(m.id);
      const cur = f.kind === "conn" ? connectorRect(it, W, H) : f.kind === "hole" ? [it.x - it.d / 2, it.y - it.d / 2, it.d, it.d] : blockRect(it);
      const nxt = { ...it, ...m };
      const to = f.kind === "conn" ? connectorRect(nxt, W, H) : f.kind === "hole" ? [nxt.x - it.d / 2, nxt.y - it.d / 2, it.d, it.d] : blockRect(nxt);
      const c0 = [cur[0] + cur[2] / 2, cur[1] + cur[3] / 2], c1 = [to[0] + to[2] / 2, to[1] + to[3] / 2];
      if (Math.hypot(c1[0] - c0[0], c1[1] - c0[1]) > 0.3) g.appendChild(el("line", { x1: X(c0[0]), y1: Y(c0[1]), x2: X(c1[0]), y2: Y(c1[1]), class: "fp-garrow" + (on ? " on" : "") }));
      const shape = f.kind === "hole" ? el("circle", { cx: X(c1[0]), cy: Y(c1[1]), r: it.d / 2 * S, class: "fp-ghost" + (on ? " on" : "") })
        : el("rect", { x: X(to[0]), y: Y(to[1]), width: to[2] * S, height: to[3] * S, rx: 3, class: "fp-ghost" + (on ? " on" : "") });
      shape.appendChild(el("title", {}, `${it.ref || it.label || it.id}: ${m.why || "suggested"}${on ? "" : " (left out)"}`));
      if (this.opts.editable) shape.addEventListener("click", (e) => { e.stopPropagation(); this.toggleTake(m.id); });
      g.appendChild(shape);
      const n = el("text", { x: X(c1[0]), y: Y(c1[1]) + 4, class: "fp-gnum" + (on ? " on" : "") }, String(i + 1));
      if (this.opts.editable) n.addEventListener("click", (e) => { e.stopPropagation(); this.toggleTake(m.id); });
      g.appendChild(n);
    });
    return g;
  }

  toggleTake(id) {
    if (this.take.has(id)) this.take.delete(id); else this.take.add(id);
    this.draw();
    if (this.onTake) this.onTake(this.take);
  }

  // the name and state each item carries in its tooltip
  lockMark(it, x, y) {
    if (!it.locked) return null;
    const g = el("g", { class: "fp-lock", transform: `translate(${x - 11} ${y + 2})` },
      el("rect", { width: 9, height: 7, x: 0, y: 4, rx: 1.5 }), el("path", { d: "M2 4V2.6a2.5 2.5 0 0 1 5 0V4", fill: "none" }));
    return g;
  }

  item(it, [x, y, w, hh], bad) {
    const S = this.S, X = (v) => this.ox + v * S, Y = (v) => this.oy + v * S;
    const cls = "fp-item " + (it.edge ? "conn" : "blk " + (it.kind || "other")) + (bad ? " bad" : "") + (it.moved ? " moved" : "") +
      (it.id === this.selId ? " sel" : "") + (it.locked ? " locked" : "");
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
    const lk = this.lockMark(it, X(x + w), Y(y));
    if (lk) g.appendChild(lk);
    g.appendChild(el("title", {}, `${name}${it.note ? " — " + it.note : ""}\n${fmt(it.w)} × ${fmt(it.h)} mm${it.rot ? `, turned ${it.rot}°` : ""}` +
      `${it.locked ? "\nLocked: Claude keeps it here" : it.moved ? "\nPlaced by you" : ""}${bad ? "\nOverlaps something or runs off the board" : ""}`));
    if (this.opts.editable) this.dragBy(g, { kind: it.edge ? "conn" : "blk", it });
    return g;
  }

  keepout(k) {
    const S = this.S, X = (v) => this.ox + v * S, Y = (v) => this.oy + v * S;
    const [x, y, w, hh] = blockRect(k);
    const g = el("g", { class: "fp-ko" + (k.id === this.selId ? " sel" : "") + (k.locked ? " locked" : ""), "data-id": k.id },
      el("rect", { x: X(x), y: Y(y), width: w * S, height: hh * S }));
    if (k.label && w * S > 40) g.appendChild(el("text", { x: X(x + w / 2), y: Y(y + hh / 2) + 4 }, k.label));
    const lk = this.lockMark(k, X(x + w), Y(y));
    if (lk) g.appendChild(lk);
    g.appendChild(el("title", {}, `Keep-out${k.label ? ": " + k.label : ""}\n${fmt(w)} × ${fmt(hh)} mm${k.locked ? "\nLocked: Claude keeps it here" : ""}`));
    if (this.opts.editable) this.dragBy(g, { kind: "ko", it: k });
    return g;
  }

  hole(ho) {
    const S = this.S, X = (v) => this.ox + v * S, Y = (v) => this.oy + v * S;
    const g = el("g", { class: "fp-hole" + (ho.moved ? " moved" : "") + (ho.id === this.selId ? " sel" : "") + (ho.locked ? " locked" : ""), "data-id": ho.id },
      el("circle", { cx: X(ho.x), cy: Y(ho.y), r: (ho.d / 2 + 1) * S, class: "ring" }),
      el("circle", { cx: X(ho.x), cy: Y(ho.y), r: (ho.d / 2) * S }));
    const lk = this.lockMark(ho, X(ho.x + ho.d / 2 + 1) + 4, Y(ho.y - ho.d / 2 - 1) - 4);
    if (lk) g.appendChild(lk);
    g.appendChild(el("title", {}, `${ho.ref || "Hole"}: ${fmt(ho.d)} mm at ${fmt(ho.x)}, ${fmt(ho.y)}${ho.locked ? "\nLocked: Claude keeps it here" : ""}`));
    if (this.opts.editable) this.dragBy(g, { kind: "hole", it: ho });
    return g;
  }

  // ------------------------------------------------------------------ selection: turn, lock, nudge
  select(id) {
    if (this.selId === id) return;
    this.selId = id;
    this.draw();
  }

  // the bar over the selected item: turn it, lock it
  renderBar() {
    const bar = this.bar, f = this.selId && this.opts.editable ? this.find(this.selId) : null;
    bar.replaceChildren();
    if (!f) { bar.style.display = "none"; return; }
    const it = f.it;
    if (f.kind !== "hole") bar.appendChild(h("button.fp-bb", { "data-tip": f.kind === "conn" ? "Face the next edge (R)" : "Turn a quarter (R)", onclick: () => this.rotate() }, icon("rotate-cw", 14)));
    bar.appendChild(h("button.fp-bb" + (it.locked ? ".on" : ""), { "data-tip": it.locked ? "Unlock (L)" : "Lock: Claude keeps it here (L)", onclick: () => this.toggleLock() },
      icon(it.locked ? "lock" : "lock-open", 14)));
    bar.style.display = "";
    const node = this.svg.querySelector(`[data-id="${CSS.escape(it.id)}"]`);
    if (!node) { bar.style.display = "none"; return; }
    requestAnimationFrame(() => {                     // above the item, inside the drawing
      if (!node.isConnected) return;
      const nr = node.getBoundingClientRect(), er = this.el.getBoundingClientRect();
      bar.style.left = Math.max(0, nr.left - er.left + nr.width / 2 - bar.offsetWidth / 2) + "px";
      bar.style.top = Math.max(0, nr.top - er.top - bar.offsetHeight - 6) + "px";
    });
  }

  key(e) {
    if (!this.opts.editable || !this.selId || e.metaKey || e.ctrlKey || e.altKey) return;
    const f = this.find(this.selId);
    if (!f) return;
    const k = e.key;
    if (k === "r" || k === "R") this.rotate();
    else if (k === "l" || k === "L") this.toggleLock();
    else if (k === "Escape") this.select(null);
    else if (k.startsWith("Arrow")) {
      const step = e.shiftKey ? 5 : 0.5;
      const dx = k === "ArrowLeft" ? -step : k === "ArrowRight" ? step : 0, dy = k === "ArrowUp" ? -step : k === "ArrowDown" ? step : 0;
      this.nudge(f, dx, dy);
    } else return;
    e.preventDefault(); e.stopPropagation();
  }

  rotate() {
    const f = this.find(this.selId);
    if (!f || f.kind === "hole") return;
    const it = f.it, W = this.fp.board.w, H = this.fp.board.h;
    const start = { ...it };
    if (f.kind === "conn") {                          // a connector turns to face the next edge, as far along it
      const span = (e) => (e === "left" || e === "right" ? H : W);
      const frac = (it.at ?? span(it.edge) / 2) / span(it.edge);
      it.edge = NEXT_EDGE[it.edge] || "top";
      it.at = clamp(snap(frac * span(it.edge)), Math.min(it.w / 2, span(it.edge) / 2), Math.max(span(it.edge) - it.w / 2, span(it.edge) / 2));
      this.save({ id: it.id, edge: it.edge, at: it.at }, it, start);
    } else {
      it.rot = ((it.rot || 0) + 90) % 360;
      this.save({ id: it.id, x: it.x, y: it.y, rot: it.rot }, it, start);
    }
    it.moved = true;
    this.draw();
  }

  toggleLock() {
    const f = this.find(this.selId);
    if (!f) return;
    const start = { ...f.it };
    f.it.locked = !f.it.locked;
    this.draw();
    this.save({ id: f.it.id, locked: f.it.locked }, f.it, start);
  }

  nudge(f, dx, dy) {
    const it = f.it, W = this.fp.board.w, H = this.fp.board.h;
    const start = this.nudgeStart && this.nudgeStart.id === it.id ? this.nudgeStart.v : { ...it };
    this.nudgeStart = { id: it.id, v: start };
    if (f.kind === "conn") {
      const along = it.edge === "left" || it.edge === "right" ? dy : dx, span = it.edge === "left" || it.edge === "right" ? H : W;
      it.at = clamp(snap((it.at ?? span / 2) + along), Math.min(it.w / 2, span / 2), Math.max(span - it.w / 2, span / 2));
    } else {
      const m = f.kind === "hole" ? it.d / 2 : 0;
      it.x = clamp(snap(it.x + dx), m, W - m); it.y = clamp(snap(it.y + dy), m, H - m);
    }
    it.moved = true;
    this.draw();
    clearTimeout(this.nudgeT);                         // one save when the keys stop
    this.nudgeT = setTimeout(() => {
      this.nudgeStart = null;
      this.save(f.kind === "conn" ? { id: it.id, edge: it.edge, at: it.at } : { id: it.id, x: it.x, y: it.y, ...(it.rot != null && f.kind !== "hole" ? { rot: it.rot } : {}) }, it, start);
    }, 350);
  }

  async save(body, it, start) {
    if (!this.opts.pid) return;
    try {
      const r = await api(`/api/projects/${enc(this.opts.pid)}/canvas/floorplan`, { method: "PATCH", body });
      if (r.floorplan) this.set(r.floorplan);
    } catch (e) {
      toast(e.message, "error");
      if (it && start) { Object.assign(it, start); this.draw(); }
    }
  }

  // ------------------------------------------------------------------ dragging
  // The pointer is followed on the document for the whole drag (each step redraws the plan, replacing the node
  // that was grabbed); while the board's corner is dragged the scale holds still. A press without a move selects.
  dragBy(node, what) {
    node.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || this.drag) return;
      e.preventDefault(); e.stopPropagation();
      this.el.focus({ preventScroll: true });
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
        if (what.it) this.selId = what.it.id;
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
        else if (what.it) this.selId = what.it.id;   // a click: select it
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
      : d.what.kind === "conn" ? { id: it.id, edge: it.edge, at: it.at }
      : { id: it.id, x: it.x, y: it.y, ...(d.what.kind !== "hole" && it.rot != null ? { rot: it.rot } : {}) };
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
