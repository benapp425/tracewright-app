// Geometry for editing the board in the app: distances between copper shapes, the copper in the way of a new track
// (an obstacle index per layer and net), the clearance check, and the walkaround router -- a shortest path at 45°
// steps on a fine grid round everything of another net, as KiCad's interactive router walks round it.
//
// Shapes: {t: "cap", a, b, r} (a track: a segment with round ends), {t: "circ", c, r} (a via or a hole),
// {t: "poly", p} (a pad or a keep-out). Coordinates in board mm.

export function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function orient(ax, ay, bx, by, cx, cy) { return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax); }

export function segsCross(a, b, c, d) {
  const d1 = orient(c[0], c[1], d[0], d[1], a[0], a[1]), d2 = orient(c[0], c[1], d[0], d[1], b[0], b[1]);
  const d3 = orient(a[0], a[1], b[0], b[1], c[0], c[1]), d4 = orient(a[0], a[1], b[0], b[1], d[0], d[1]);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

export function segSegDist(a, b, c, d) {
  if (segsCross(a, b, c, d)) return 0;
  return Math.min(segDist(a[0], a[1], c[0], c[1], d[0], d[1]), segDist(b[0], b[1], c[0], c[1], d[0], d[1]),
    segDist(c[0], c[1], a[0], a[1], b[0], b[1]), segDist(d[0], d[1], a[0], a[1], b[0], b[1]));
}

export function inPoly(x, y, pts) {
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
    if ((yi > y) !== (yj > y) && x < xi + (y - yi) * (xj - xi) / (yj - yi)) c = !c;
  }
  return c;
}

// distance from a point to a polygon's edges (0 inside)
export function polyDist(x, y, pts) {
  if (inPoly(x, y, pts)) return 0;
  let d = Infinity;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) d = Math.min(d, segDist(x, y, pts[j][0], pts[j][1], pts[i][0], pts[i][1]));
  return d;
}

// distance from a segment to a polygon (0 when it runs into it)
export function segPolyDist(a, b, pts) {
  if (inPoly(a[0], a[1], pts) || inPoly(b[0], b[1], pts)) return 0;
  let d = Infinity;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) d = Math.min(d, segSegDist(a, b, pts[j], pts[i]));
  return d;
}

export function polysMeet(p, q) {
  for (let i = 0, j = p.length - 1; i < p.length; j = i++)
    for (let k = 0, l = q.length - 1; k < q.length; l = k++) if (segsCross(p[j], p[i], q[l], q[k])) return true;
  return (p.length && inPoly(p[0][0], p[0][1], q)) || (q.length && inPoly(q[0][0], q[0][1], p));
}

export function bboxOf(pts) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; }
  return [x0, y0, x1, y1];
}

// how far a shape is from a point
function shapeDistPt(s, x, y) {
  if (s.t === "cap") return segDist(x, y, s.a[0], s.a[1], s.b[0], s.b[1]) - s.r;
  if (s.t === "circ") return Math.hypot(x - s.c[0], y - s.c[1]) - s.r;
  return polyDist(x, y, s.p);
}
// how far a shape is from a segment
function shapeDistSeg(s, a, b) {
  if (s.t === "cap") return segSegDist(a, b, s.a, s.b) - s.r;
  if (s.t === "circ") return segDist(s.c[0], s.c[1], a[0], a[1], b[0], b[1]) - s.r;
  return segPolyDist(a, b, s.p);
}

const CELL = 2.0;

// The copper of other nets on one layer, the holes and the keep-outs that bar tracks there, and the board's edge,
// indexed by a coarse grid. view: the board view (its data, footprints and rules).
export class Obstacles {
  constructor(view, layer, net, opts = {}) {
    const d = view.data, rules = d.rules || {};
    this.layer = layer; this.net = net;
    this.edgeCl = rules.edge_clearance || 0.3;
    this.items = [];
    this.grid = new Map();
    const clOf = (n) => netClearance(rules, n);
    const ignore = opts.ignore || new Set();           // track/via ids moved by the edit itself
    d.tracks.forEach((t, i) => {
      if (t[5] !== layer || (net && t[6] === net) || ignore.has("t:" + d.tid[i])) return;
      this.add({ t: "cap", a: [t[0], t[1]], b: [t[2], t[3]], r: t[4] / 2, net: t[6], cl: clOf(t[6]), what: "track", i });
    });
    d.vias.forEach((v, i) => {
      if ((net && v[4] === net) || ignore.has("v:" + d.vid[i])) return;
      if (v[6] && v[6].length === 2 && !viaSpans(d.copper, v[6], layer)) return;
      this.add({ t: "circ", c: [v[0], v[1]], r: v[2] / 2, net: v[4], cl: clOf(v[4]), what: "via", i });
    });
    for (const f of view.fps) {
      if (opts.skipRefs && opts.skipRefs.has(f.ref)) continue;
      const pose = view.pose(f);
      for (const p of f.pads) {
        const onCu = p.l.includes("*.Cu") || p.l.includes(layer) || (p.l.includes("F&B.Cu") && (layer === "F.Cu" || layer === "B.Cu"));
        const hole = p.d && (p.d[0] > 0 || p.d[1] > 0);
        if (onCu && !(net && p.net === net)) {
          for (const pl of p.p) this.add({ t: "poly", p: pose ? pl.map(([x, y]) => fwd(pose, f, x, y)) : pl, net: p.net, cl: clOf(p.net), what: "pad", ref: f.ref, pad: p.n });
        } else if (hole && !onCu) {                    // a bare hole (NPTH): kept clear on every layer
          const [x, y] = pose ? fwd(pose, f, p.x, p.y) : [p.x, p.y];
          this.add({ t: "circ", c: [x, y], r: Math.max(p.d[0], p.d[1]) / 2, net: "", cl: rules.hole_clearance || 0.25, what: "hole", ref: f.ref });
        }
      }
    }
    for (const z of d.zones) {                         // keep-outs barring tracks on this layer
      if (!z.rule || !z.ko || !(z.ko.tracks) || !(z.l || []).some((l) => l === layer || l === "*.Cu" || l === "F&B.Cu")) continue;
      for (const o of z.o) if (o.length >= 3) this.add({ t: "poly", p: o, net: "", cl: 0, what: "keepout", name: z.name });
    }
    this.outline = (d.outline || []).filter((o) => o.length >= 3);
    for (const o of this.outline) for (let i = 0, j = o.length - 1; i < o.length; j = i++)
      this.add({ t: "cap", a: o[j], b: o[i], r: 0, net: "", cl: this.edgeCl, what: "edge" });
  }

  add(s) {
    const r = (s.r || 0) + (s.cl || 0) + 2;          // reach: anything within this many mm may be close enough to matter
    let bb;
    if (s.t === "cap") bb = [Math.min(s.a[0], s.b[0]), Math.min(s.a[1], s.b[1]), Math.max(s.a[0], s.b[0]), Math.max(s.a[1], s.b[1])];
    else if (s.t === "circ") bb = [s.c[0], s.c[1], s.c[0], s.c[1]];
    else bb = bboxOf(s.p);
    s.bb = [bb[0] - (s.t === "poly" ? 0 : s.r || 0), bb[1] - (s.t === "poly" ? 0 : s.r || 0), bb[2] + (s.t === "poly" ? 0 : s.r || 0), bb[3] + (s.t === "poly" ? 0 : s.r || 0)];
    const k = this.items.length;
    this.items.push(s);
    for (let i = Math.floor((s.bb[0] - r) / CELL); i <= Math.floor((s.bb[2] + r) / CELL); i++)
      for (let j = Math.floor((s.bb[1] - r) / CELL); j <= Math.floor((s.bb[3] + r) / CELL); j++) {
        const key = i * 100003 + j;
        const q = this.grid.get(key);
        if (q) q.push(k); else this.grid.set(key, [k]);
      }
  }

  near(x0, y0, x1, y1) {
    const out = new Set();
    for (let i = Math.floor(x0 / CELL); i <= Math.floor(x1 / CELL); i++)
      for (let j = Math.floor(y0 / CELL); j <= Math.floor(y1 / CELL); j++) {
        const q = this.grid.get(i * 100003 + j);
        if (q) for (const k of q) out.add(k);
      }
    return [...out].map((k) => this.items[k]);
  }

  // on the board: inside its outline and not in a cut-out (even-odd over every loop)
  inside(x, y) {
    if (!this.outline.length) return true;
    let n = 0;
    for (const o of this.outline) if (inPoly(x, y, o)) n++;
    return n % 2 === 1;
  }

  // the first thing a track (a, b, width w, clearance cl) would come too close to, or null
  hitSeg(a, b, w, cl) {
    if (!this.inside(a[0], a[1]) || !this.inside(b[0], b[1])) return { what: "edge", net: "" };
    const m = w / 2 + 2;
    for (const s of this.near(Math.min(a[0], b[0]) - m, Math.min(a[1], b[1]) - m, Math.max(a[0], b[0]) + m, Math.max(a[1], b[1]) + m)) {
      const need = w / 2 + Math.max(cl, s.cl || 0) - 1e-4;
      if (shapeDistSeg(s, a, b) < need) return s;
    }
    return null;
  }

  // the first thing a via (centre c, diameter d) would come too close to, or null
  hitCirc(c, r, cl) {
    if (!this.inside(c[0], c[1])) return { what: "edge", net: "" };
    for (const s of this.near(c[0] - r - 2, c[1] - r - 2, c[0] + r + 2, c[1] + r + 2)) {
      if (shapeDistPt(s, c[0], c[1]) < r + Math.max(cl, s.cl || 0) - 1e-4) return s;
    }
    return null;
  }

  hitPath(pts, w, cl) {
    for (let i = 0; i + 1 < pts.length; i++) { const s = this.hitSeg(pts[i], pts[i + 1], w, cl); if (s) return s; }
    return null;
  }
}

export function netClearance(rules, net) {
  const cls = (rules.classes || {})[(rules.net_class || {})[net] || "Default"] || (rules.classes || {}).Default || {};
  return Math.max(cls.clearance || 0.2, rules.min_clearance || 0);
}

export function netClass(rules, net) {
  const name = (rules.net_class || {})[net] || "Default";
  const c = (rules.classes || {})[name] || (rules.classes || {}).Default || {};
  return { name, w: Math.max(c.track_width || 0.25, rules.min_track || 0), cl: netClearance(rules, net),
           vd: Math.max(c.via_diameter || 0.6, rules.min_via || 0), vdrill: c.via_drill || 0.3 };
}

function viaSpans(copper, pair, layer) {
  const i = copper.indexOf(pair[0]), j = copper.indexOf(pair[1]), k = copper.indexOf(layer);
  if (i < 0 || j < 0 || k < 0) return true;
  return k >= Math.min(i, j) && k <= Math.max(i, j);
}

export function fwd(p, f, x, y) {
  const a = -(p.rot - f.a) * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a);
  const dx = x - f.x, dy = y - f.y;
  return [p.x + dx * cs - dy * sn, p.y + dx * sn + dy * cs];
}

// ------------------------------------------------------------------ the two-segment posture and the walkaround
// From a to b in at most two segments at 45° steps: diagonal first, or straight first.
export function posture(a, b, diagFirst) {
  const dx = b[0] - a[0], dy = b[1] - a[1], ax = Math.abs(dx), ay = Math.abs(dy);
  if (ax < 1e-6 || ay < 1e-6 || Math.abs(ax - ay) < 1e-6) return [a, b];
  const d = Math.min(ax, ay), sx = Math.sign(dx), sy = Math.sign(dy);
  const mid = diagFirst ? [a[0] + sx * d, a[1] + sy * d] : [b[0] - sx * d, b[1] - sy * d];
  return [a, mid, b];
}

const DIRS = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
const BUF = { n: 0, g: null, came: null };            // the search's tables, kept between calls

// The shortest way from a to b at 45° steps that keeps clear of everything in obs: [[x, y], ...] or null.
// opts.ends: the copper of the route's own ends (pad polygons), which the path may cross to leave and reach them.
// The grid lines up with a, so the path leaves it exactly; it comes into b exactly by a last bend at 45° steps.
// Every segment is checked against the copper itself; a path that still comes too close is searched again with a
// margin, else there is none.
export function walk(obs, a, b, w, cl, opts = {}) {
  for (const margin of [0, 0.75]) {
    const cells = search(obs, a, b, w, cl, margin, opts);
    if (!cells) return null;                            // a margin only makes it harder
    const pts = finish(obs, cells, b, w, cl);
    if (!pts) continue;
    const out = straighten(obs, pts, w, cl);
    if (!obs.hitPath(out, w, cl)) return out;
  }
  return null;
}

// the way in: the latest point of the path from which one bend at 45° steps reaches b and keeps clear
function finish(obs, cells, b, w, cl) {
  for (let k = cells.length - 1; k >= 0; k--) {
    const P = cells[k];
    for (const p of [posture(P, b, true), posture(P, b, false)]) {
      if (obs.hitPath(p, w, cl)) continue;
      const head = compress(cells.slice(0, k + 1));
      return head.concat(p.slice(1));
    }
  }
  return null;
}

// grid points to corners: only where the direction changes
function compress(cells) {
  if (cells.length < 3) return cells.slice();
  const out = [cells[0]];
  for (let i = 1; i + 1 < cells.length; i++) {
    const a = cells[i - 1], b = cells[i], c = cells[i + 1];
    if (Math.abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9) out.push(b);
  }
  out.push(cells[cells.length - 1]);
  return out;
}

// A* on the grid: the cells from a to b's cell, as points, or null
function search(obs, a, b, w, cl, margin, opts) {
  const span = Math.hypot(b[0] - a[0], b[1] - a[1]);
  const m = Math.max(3, Math.min(20, span * 0.6));
  let res = opts.res || Math.max(0.05, Math.min(0.1, (w + cl) / 3));
  let x0 = Math.min(a[0], b[0]) - m, y0 = Math.min(a[1], b[1]) - m, x1 = Math.max(a[0], b[0]) + m, y1 = Math.max(a[1], b[1]) + m;
  while ((x1 - x0) / res * (y1 - y0) / res > 160000) res *= 1.25;
  // the grid's origin on a: the path starts exactly there
  const ox = a[0] - Math.ceil((a[0] - x0) / res) * res, oy = a[1] - Math.ceil((a[1] - y0) / res) * res;
  const W = Math.ceil((x1 - ox) / res) + 1, H = Math.ceil((y1 - oy) / res) + 1, N = W * H;
  const blocked = new Uint8Array(N);
  const half = w / 2, extra = margin * res;
  for (const s of obs.near(x0, y0, x1, y1)) {
    const need = half + Math.max(cl, s.cl || 0) + extra;
    const r = s.t === "poly" ? 0 : s.r || 0;
    const bx0 = Math.max(0, Math.floor((s.bb[0] - need - r - ox) / res)), bx1 = Math.min(W - 1, Math.ceil((s.bb[2] + need + r - ox) / res));
    const by0 = Math.max(0, Math.floor((s.bb[1] - need - r - oy) / res)), by1 = Math.min(H - 1, Math.ceil((s.bb[3] + need + r - oy) / res));
    for (let j = by0; j <= by1; j++) for (let i = bx0; i <= bx1; i++) {
      const k = j * W + i;
      if (blocked[k]) continue;
      if (shapeDistPt(s, ox + i * res, oy + j * res) < need - 1e-4) blocked[k] = 1;
    }
  }
  if (obs.outline.length) {                            // off the board: a scanline per row, even-odd over every loop
    for (let j = 0; j < H; j++) {
      const y = oy + j * res, xs = [];
      for (const o of obs.outline) for (let p = 0, q = o.length - 1; p < o.length; q = p++) {
        const xa = o[q][0], ya = o[q][1], xb = o[p][0], yb = o[p][1];
        if ((ya > y) !== (yb > y)) xs.push(xa + (y - ya) * (xb - xa) / (yb - ya));
      }
      xs.sort((u, v) => u - v);
      let c = 0;
      for (let i = 0; i < W; i++) {
        const x = ox + i * res;
        while (c < xs.length && xs[c] <= x) c++;
        if (c % 2 === 0) blocked[j * W + i] = 1;
      }
    }
  }
  const cellOf = (p) => [Math.round((p[0] - ox) / res), Math.round((p[1] - oy) / res)];
  const [si, sj] = cellOf(a), [gi, gj] = cellOf(b);
  if (si < 0 || sj < 0 || si >= W || sj >= H || gi < 0 || gj < 0 || gi >= W || gj >= H) return null;
  const S = sj * W + si, G = gj * W + gi;
  // the route's own ends are free to cross: its pads (or a little round a bare point)
  const ends = opts.ends || [];
  for (const pl of ends) {
    const bb = bboxOf(pl);
    for (let j = Math.max(0, Math.floor((bb[1] - oy) / res)); j <= Math.min(H - 1, Math.ceil((bb[3] - oy) / res)); j++)
      for (let i = Math.max(0, Math.floor((bb[0] - ox) / res)); i <= Math.min(W - 1, Math.ceil((bb[2] - ox) / res)); i++)
        if (inPoly(ox + i * res, oy + j * res, pl)) blocked[j * W + i] = 0;
  }
  blocked[S] = 0; blocked[G] = 0;
  if (BUF.n < N * 8) { BUF.n = N * 8; BUF.g = new Float32Array(BUF.n); BUF.came = new Int32Array(BUF.n); }
  const g = BUF.g, came = BUF.came;
  g.fill(Infinity, 0, N * 8); came.fill(-1, 0, N * 8);
  const heap = new Heap();
  const hfun = (k) => { const i = k % W, j = (k / W) | 0, dx = Math.abs(i - gi), dy = Math.abs(j - gj); return (Math.max(dx, dy) + (Math.SQRT2 - 1) * Math.min(dx, dy)); };
  for (let d = 0; d < 8; d++) { g[S * 8 + d] = 0; heap.push(hfun(S), S * 8 + d); }
  const TURN = 0.35, limit = opts.limit || 600000;
  let found = -1, n = 0;
  while (heap.size) {
    const st = heap.pop();
    const k = (st / 8) | 0, d = st % 8;
    if (k === G) { found = st; break; }
    if (++n > limit) break;
    const gs = g[st], i = k % W, j = (k / W) | 0;
    for (const nd of [d, (d + 1) % 8, (d + 7) % 8, (d + 2) % 8, (d + 6) % 8]) {
      const [dx, dy] = DIRS[nd];
      const ni = i + dx, nj = j + dy;
      if (ni < 0 || nj < 0 || ni >= W || nj >= H) continue;
      const nk = nj * W + ni;
      if (blocked[nk]) continue;
      if (dx && dy && (blocked[j * W + ni] && blocked[nj * W + i])) continue;          // no squeezing through a corner
      const turn = nd === d ? 0 : (nd === (d + 1) % 8 || nd === (d + 7) % 8) ? TURN : TURN * 3;
      const ng = gs + (dx && dy ? Math.SQRT2 : 1) + turn;
      const ns = nk * 8 + nd;
      if (ng < g[ns]) { g[ns] = ng; came[ns] = st; heap.push(ng + hfun(nk), ns); }
    }
  }
  if (found < 0) return null;
  const ks = [];
  for (let st = found; st >= 0; st = came[st]) { const k = (st / 8) | 0; if (!ks.length || ks[ks.length - 1] !== k) ks.push(k); if (came[st] < 0) break; }
  ks.reverse();
  return ks.map((k, q) => q === 0 ? a : [ox + (k % W) * res, oy + ((k / W) | 0) * res]);
}

// fewer corners: a corner is cut out where its neighbours can meet in one 45° step, two corners become one where a
// single bend joins their neighbours -- each change takes a point away, so this always ends
function straighten(obs, pts, w, cl) {
  const ok45 = (a, b) => { const dx = Math.abs(b[0] - a[0]), dy = Math.abs(b[1] - a[1]); return dx < 1e-6 || dy < 1e-6 || Math.abs(dx - dy) < 1e-6; };
  let changed = true;
  while (changed && pts.length > 2) {
    changed = false;
    for (let i = 1; i + 1 < pts.length && !changed; i++) {
      const a = pts[i - 1], c = pts[i + 1];
      if (ok45(a, c) && !obs.hitSeg(a, c, w, cl)) { pts.splice(i, 1); changed = true; }
    }
    for (let i = 1; i + 2 < pts.length && !changed; i++) {
      const a = pts[i - 1], d = pts[i + 2];
      for (const p of [posture(a, d, true), posture(a, d, false)]) {
        if (p.length !== 3 || obs.hitPath(p, w, cl)) continue;
        pts.splice(i, 2, p[1]);
        changed = true;
        break;
      }
    }
  }
  const out = [pts[0]];                                // drop zero-length and straight-through points
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i], q = out[out.length - 1];
    if (Math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-6) continue;
    if (out.length >= 2) {
      const o = out[out.length - 2];
      if (Math.abs(orient(o[0], o[1], q[0], q[1], p[0], p[1])) < 1e-9 && (q[0] - o[0]) * (p[0] - q[0]) + (q[1] - o[1]) * (p[1] - q[1]) > 0) { out[out.length - 1] = p; continue; }
    }
    out.push(p);
  }
  return out;
}

class Heap {
  constructor() { this.k = []; this.v = []; }
  get size() { return this.k.length; }
  push(key, val) {
    const k = this.k, v = this.v;
    let i = k.length;
    k.push(key); v.push(val);
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (k[p] <= key) break;
      k[i] = k[p]; v[i] = v[p]; i = p;
    }
    k[i] = key; v[i] = val;
  }
  pop() {
    const k = this.k, v = this.v, top = v[0];
    const lk = k.pop(), lv = v.pop();
    if (k.length) {
      let i = 0;
      const n = k.length;
      for (;;) {
        let c = 2 * i + 1;
        if (c >= n) break;
        if (c + 1 < n && k[c + 1] < k[c]) c++;
        if (k[c] >= lk) break;
        k[i] = k[c]; v[i] = v[c]; i = c;
      }
      k[i] = lk; v[i] = lv;
    }
    return top;
  }
}
