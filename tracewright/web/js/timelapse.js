// The board's timelapse: every change -- Claude's, yours, the router's live stream, or rebuilt from the
// project's history -- is a frame; the player draws them on its own canvas, parts gliding to their new
// places and new copper drawing itself in, with markers on the timeline for who changed what, the
// board's numbers as they grow, and an export to a video. Film (the renderer) is shared with
// the run monitor, which shows the same picture live.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";

const COL = { "F.Cu": [226, 64, 64], "B.Cu": [64, 110, 232], "In1.Cu": [214, 170, 48], "In2.Cu": [70, 180, 100], "In3.Cu": [180, 100, 210],
  "In4.Cu": [60, 180, 180], pad: [201, 171, 92], padB: [150, 132, 90], via: [196, 200, 210], edge: [238, 214, 72], glow: [255, 226, 110] };
const rgba = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
const layerCol = (l) => COL[l] || [150, 150, 160];
const easeInOut = (t) => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
export const SRC = { claude: ["Claude", "var(--accent)", [217, 119, 87]], you: ["You", "#5b9bf8", [91, 155, 248]], router: ["Router", "#43c283", [67, 194, 131]],
  history: ["History", "#9c9ea6", [156, 158, 166]], start: ["Start", "#ececee", [236, 236, 238]] };

// ------------------------------------------------------------------ frames
// The picture after a frame (the same rules as tracewright/timelapse.py apply()).
export function applyFrame(st, fr) {
  if (fr.k || !st) return { fp: { ...(fr.fp || {}) }, tr: Object.fromEntries((fr["tr+"] || []).map((r) => [r[0], r.slice(1)])),
    vi: Object.fromEntries((fr["vi+"] || []).map((r) => [r[0], r.slice(1)])), zn: { ...(fr.zn || {}) }, ol: fr.ol || [], stats: fr.stats || {} };
  const n = { fp: { ...st.fp }, tr: { ...st.tr }, vi: { ...st.vi }, zn: { ...st.zn }, ol: st.ol, stats: fr.stats || st.stats || {} };
  for (const [ref, pose] of Object.entries(fr.fp || {})) { if (pose === null) delete n.fp[ref]; else n.fp[ref] = pose; }
  if (!fr.tmp) {                                     // a file frame retires the router's in-flight tracks
    for (const k of Object.keys(n.tr)) if (k.startsWith("r:")) delete n.tr[k];
    for (const k of Object.keys(n.vi)) if (k.startsWith("r:")) delete n.vi[k];
  }
  for (const k of fr["tr-"] || []) delete n.tr[k];
  for (const r of fr["tr+"] || []) n.tr[r[0]] = r.slice(1);
  for (const k of fr["vi-"] || []) delete n.vi[k];
  for (const r of fr["vi+"] || []) n.vi[r[0]] = r.slice(1);
  for (const [k, z] of Object.entries(fr.zn || {})) { if (z === null) delete n.zn[k]; else n.zn[k] = z; }
  if (fr.ol && fr.ol.length) n.ol = fr.ol;
  return n;
}

// What a frame changed, for the glow: parts that moved, copper that appeared.
function changes(prev, fr) {
  const fp = new Set(Object.keys(fr.fp || {}).filter((r) => !prev || !prev.fp[r] || JSON.stringify(prev.fp[r]) !== JSON.stringify(fr.fp[r])));
  return { fp: fr.k ? new Set() : fp, tr: new Set((fr["tr+"] || []).map((r) => r[0])), vi: new Set((fr["vi+"] || []).map((r) => r[0])) };
}

// ------------------------------------------------------------------ the parts' own shapes
// Each footprint's pads and courtyard in its own coordinates (from the board as it is now), so any
// frame's position, rotation and side can put it anywhere. KiCad: y down, angles counter-clockwise.
export function partShapes(board) {
  const out = {};
  for (const f of (board && board.footprints) || []) {
    const a = (f.a || 0) * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
    const loc = ([X, Y]) => { const dx = X - f.x, dy = Y - f.y; return [dx * c - dy * s, dx * s + dy * c]; };
    const pads = [];
    for (const p of f.pads || []) {
      let poly = p.p && p.p[0] && p.p[0].length >= 3 ? p.p[0] : null;
      if (!poly) {
        const pa = (p.a || 0) * Math.PI / 180, pc = Math.cos(pa), ps = Math.sin(pa), w = (p.w || 0.5) / 2, hh = (p.h || 0.5) / 2;
        poly = [[-w, -hh], [w, -hh], [w, hh], [-w, hh]].map(([x, y]) => [p.x + x * pc + y * ps, p.y - x * ps + y * pc]);
      }
      const layers = p.l || [];
      pads.push({ poly: poly.map(loc), th: !!p.d, top: layers.some((l) => l.startsWith("F.") || l === "*.Cu"), bot: layers.some((l) => l.startsWith("B.") || l === "*.Cu") });
    }
    out[f.ref] = { pads, cy: (f.cy || []).map((l) => l.map(loc)), side: f.side || "F" };
  }
  return out;
}

// ------------------------------------------------------------------ the renderer
export class Film {
  // inset: room kept clear around the drawing for overlays, in CSS px [left, top, right, bottom]
  constructor(canvas, inset) { this.canvas = canvas; this.ctx = canvas.getContext("2d"); this.shapes = {}; this.bounds = null; this.labels = true; this.inset = inset || [0, 0, 0, 0]; }

  setShapes(s) { this.shapes = s || {}; }

  // the drawing's extent: the board outline, else everything on it
  fit(st) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y) => { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; };
    for (const loop of st.ol || []) for (const [x, y] of loop) add(x, y);
    if (!isFinite(x0)) {
      for (const p of Object.values(st.fp || {})) add(p[0], p[1]);
      for (const t of Object.values(st.tr || {})) { add(t[0], t[1]); add(t[2], t[3]); }
    }
    if (!isFinite(x0)) { x0 = 0; y0 = 0; x1 = 50; y1 = 50; }
    const m = Math.max(x1 - x0, y1 - y0) * 0.06 + 2;
    this.bounds = [x0 - m, y0 - m, x1 + m, y1 + m];
  }

  size() {
    const r = this.canvas.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== W || this.canvas.height !== H) { this.canvas.width = W; this.canvas.height = H; }
    return [W, H];
  }

  // st: the picture; o: {prev (tween from), t (0..1), glow ({fp, tr, vi} Sets), g (glow 0..1), fixed ([W, H] for export)}
  draw(st, o = {}) {
    if (!st) return;
    if (!this.bounds) this.fit(st);
    const [W, H] = o.fixed || this.size();
    if (o.fixed && (this.canvas.width !== W || this.canvas.height !== H)) { this.canvas.width = W; this.canvas.height = H; }
    const c = this.ctx, [bx0, by0, bx1, by1] = this.bounds;
    const dpr = o.fixed ? 1 : Math.min(2, window.devicePixelRatio || 1);
    let [il, it, ir, ib] = o.fixed ? [0, 0, 0, 0] : this.inset.map((v) => v * dpr);
    if (W - il - ir < W * 0.55) { il = ir = 0; }                    // too narrow to keep the overlays clear
    const aw = W - il - ir, ah = H - it - ib;
    const k = Math.min(aw / (bx1 - bx0), ah / (by1 - by0));
    const ox = il + (aw - (bx1 - bx0) * k) / 2 - bx0 * k, oy = it + (ah - (by1 - by0) * k) / 2 - by0 * k;
    const X = (x) => ox + x * k, Y = (y) => oy + y * k;
    this.xf = { ox, oy, k, W, H };                      // for drawing over it (Compare versions)
    const dim = o.dim ?? 1;
    const t = o.prev ? easeInOut(Math.max(0, Math.min(1, o.t ?? 1))) : 1;
    const glow = o.glow || { fp: new Set(), tr: new Set(), vi: new Set() }, g = o.g ?? 0;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = "#0d0e11"; c.fillRect(0, 0, W, H);
    // the board
    if ((st.ol || []).length) {
      c.beginPath();
      for (const loop of st.ol) loop.forEach(([x, y], i) => (i ? c.lineTo(X(x), Y(y)) : c.moveTo(X(x), Y(y))));
      c.closePath();
      c.fillStyle = "#2b1d1b"; c.fill("evenodd");
      c.lineWidth = Math.max(1.2, 0.15 * k); c.strokeStyle = rgba(COL.edge, 0.9); c.stroke();
    }
    // pours
    for (const z of Object.values(st.zn || {})) {
      for (const [l, polys] of Object.entries(z.l || {})) {
        c.beginPath();
        for (const pl of polys) pl.forEach(([x, y], i) => (i ? c.lineTo(X(x), Y(y)) : c.moveTo(X(x), Y(y))));
        c.fillStyle = rgba(layerCol(l), (l === "B.Cu" ? 0.13 : 0.17) * dim); c.fill("evenodd");
      }
    }
    // copper: bottom first, the top over it
    const order = (l) => l === "B.Cu" ? 0 : l === "F.Cu" ? 2 : 1;
    const tracks = Object.entries(st.tr || {}).sort((a, b) => order(a[1][5]) - order(b[1][5]));
    c.lineCap = "round"; c.lineJoin = "round";
    for (const [id, r] of tracks) {
      const [x1, y1, x2, y2, w, l] = r;
      const grow = glow.tr.has(id) && o.prev ? t : 1;          // new copper draws itself in
      const ex = x1 + (x2 - x1) * grow, ey = y1 + (y2 - y1) * grow;
      c.strokeStyle = rgba(layerCol(l), (l === "B.Cu" ? 0.78 : 0.92) * dim);
      c.lineWidth = Math.max(0.8, w * k);
      c.beginPath(); c.moveTo(X(x1), Y(y1));
      if (r.length > 7 && grow === 1) c.quadraticCurveTo(X(2 * r[7] - (x1 + x2) / 2), Y(2 * r[8] - (y1 + y2) / 2), X(x2), Y(y2));
      else c.lineTo(X(ex), Y(ey));
      c.stroke();
    }
    if (g > 0) {                                        // this frame's new copper, glowing
      c.save(); c.shadowColor = rgba(COL.glow, 0.9 * g); c.shadowBlur = 14;
      for (const [id, r] of tracks) {
        if (!glow.tr.has(id)) continue;
        const grow = o.prev ? t : 1;
        c.strokeStyle = rgba(COL.glow, 0.55 * g); c.lineWidth = Math.max(1.4, r[4] * k * 1.25);
        c.beginPath(); c.moveTo(X(r[0]), Y(r[1])); c.lineTo(X(r[0] + (r[2] - r[0]) * grow), Y(r[1] + (r[3] - r[1]) * grow)); c.stroke();
      }
      c.restore();
    }
    // parts: their pads and courtyards where this frame puts them (gliding from the last)
    const pose = (ref) => {
      const p = st.fp[ref];
      const q = o.prev && o.prev.fp[ref];
      if (!q || t >= 1 || !glow.fp.has(ref)) return p;
      let da = ((p[2] - q[2] + 540) % 360) - 180;
      return [q[0] + (p[0] - q[0]) * t, q[1] + (p[1] - q[1]) * t, q[2] + da * t, t > 0.5 ? p[3] : q[3]];
    };
    const moving = [];
    for (const ref of Object.keys(st.fp || {})) {
      const sh = this.shapes[ref], p = pose(ref);
      if (!p) continue;
      const a = -(p[2] || 0) * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
      const flip = sh && sh.side !== p[3] ? -1 : 1;
      const T = ([lx, ly]) => { lx *= flip; return [X(p[0] + lx * ca - ly * sa), Y(p[1] + lx * sa + ly * ca)]; };
      const hot = glow.fp.has(ref) && g > 0;
      if (!sh) {                                        // a part the board no longer has: a dot
        c.fillStyle = rgba(COL.pad, 0.8); c.beginPath(); c.arc(X(p[0]), Y(p[1]), Math.max(2, 0.5 * k), 0, 7); c.fill();
        continue;
      }
      const bottom = p[3] === "B";
      for (const pad of sh.pads) {
        c.beginPath();
        pad.poly.forEach((pt, i) => { const [x, y] = T(pt); i ? c.lineTo(x, y) : c.moveTo(x, y); });
        c.closePath();
        c.fillStyle = rgba(bottom && !pad.th ? COL.padB : COL.pad, (bottom && !pad.th ? 0.75 : 0.95) * (dim < 1 ? 0.55 + dim * 0.45 : 1));
        c.fill();
      }
      for (const loop of sh.cy) {
        c.beginPath();
        loop.forEach((pt, i) => { const [x, y] = T(pt); i ? c.lineTo(x, y) : c.moveTo(x, y); });
        c.strokeStyle = hot ? rgba(COL.glow, 0.85 * g) : "rgba(255,255,255,.10)";
        c.lineWidth = hot ? 1.6 : 1;
        c.stroke();
      }
      if (hot) moving.push([ref, X(p[0]), Y(p[1])]);
    }
    // vias
    for (const [id, v] of Object.entries(st.vi || {})) {
      const pop = glow.vi.has(id) && o.prev ? Math.min(1, t * 1.3) : 1;
      const r = Math.max(1.3, (v[2] / 2) * k) * pop;
      c.fillStyle = rgba(COL.via, 0.95 * dim); c.beginPath(); c.arc(X(v[0]), Y(v[1]), r, 0, 7); c.fill();
      c.fillStyle = "#15161a"; c.beginPath(); c.arc(X(v[0]), Y(v[1]), Math.max(0.6, (v[3] / 2) * k) * pop, 0, 7); c.fill();
    }
    // the parts that moved in this frame, named
    if (this.labels && moving.length && moving.length <= 24) {
      c.font = `600 ${Math.max(10, Math.min(14, 1.2 * k))}px -apple-system, system-ui, sans-serif`;
      c.textAlign = "center"; c.textBaseline = "middle";
      for (const [ref, x, y] of moving) {
        const w = c.measureText(ref).width + 10;
        c.fillStyle = `rgba(13,14,17,${0.75 * g})`; c.fillRect(x - w / 2, y - 9, w, 18);
        c.fillStyle = rgba(COL.glow, g); c.fillText(ref, x, y);
      }
    }
  }
}

// ------------------------------------------------------------------ the player
export class TimelapsePlayer {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.frames = []; this.i = -1; this.st = null; this.playing = false; this.speed = 2; this.keys = new Map();
    this.onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); this.close(); }
      else if (e.key === " " && e.target.tagName !== "INPUT") { e.preventDefault(); this.toggle(); }
      else if (e.key === "ArrowRight") { e.preventDefault(); this.seek(this.i + 1, true); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); this.seek(this.i - 1); }
    };
  }

  async open() {
    if (this.el) return;
    this.canvas = h("canvas.tl-canvas");
    this.film = new Film(this.canvas, [186, 14, 14, 58]);
    this.statsEl = h("div.tl-stats");
    this.noteEl = h("div.tl-note");
    this.marks = h("canvas.tl-marks");
    this.slider = h("input.tl-slider", { type: "range", min: 0, max: 0, value: 0, oninput: () => { this.pause(); this.seek(Number(this.slider.value)); } });
    this.playBtn = btn("play", null, { onclick: () => this.toggle(), "data-tip": "Play", "data-kbd": "space" }, "tl-play");
    this.countEl = h("span.tl-count");
    this.whenEl = h("span.tl-when");
    this.speedEl = h("div.seg.tl-speed", [1, 2, 4, 8, 16].map((s) => h("button" + (s === this.speed ? ".on" : ""), { onclick: (e) => {
      this.speed = s; for (const b of this.speedEl.children) b.classList.toggle("on", b === e.currentTarget); } }, `${s}×`)));
    this.histBtn = h("button.btn.sm", { onclick: () => this.fromHistory() }, icon("history", 14), "Rebuild from history");
    this.exportBtn = h("button.btn.sm", { onclick: () => this.exportVideo() }, icon("download", 14), "Export video");
    this.emptyEl = h("div.tl-empty");
    this.el = h("div.tl-overlay", { onclick: (e) => { if (e.target === this.el) this.close(); } },
      h("div.tl-win",
        h("div.tl-head", h("div.tl-title", icon("history", 16), h("b", "Timelapse"), h("span.muted", this.ws.p.name)), h("div.grow"),
          this.histBtn, this.exportBtn, btn("x", null, { onclick: () => this.close(), "data-tip": "Close", "data-kbd": "esc" }, "sm ghost")),
        h("div.tl-stage", this.canvas, this.statsEl, this.noteEl, this.emptyEl),
        h("div.tl-bar",
          h("div.tl-track", this.marks, this.slider),
          h("div.tl-ctl", btn("chevron-left", null, { onclick: () => this.seek(0), "data-tip": "Back to the start" }, "sm ghost"), this.playBtn,
            btn("chevron-right", null, { onclick: () => this.seek(this.frames.length - 1), "data-tip": "The board now" }, "sm ghost"),
            this.countEl, this.whenEl, h("div.grow"), h("span.tiny.muted", "Speed"), this.speedEl,
            h("div.tl-legend", Object.entries(SRC).filter(([k]) => k !== "start").map(([, [name, css]]) => h("span", h("i", { style: { background: css } }), name)))))));
    document.body.appendChild(this.el);
    requestAnimationFrame(() => this.el.classList.add("on"));
    addEventListener("keydown", this.onKey, true);
    this.ro = new ResizeObserver(() => { this.redraw(); this.drawMarks(); });
    this.ro.observe(this.canvas);
    this.off = [this.ws.ev.on("timelapse.frame", () => this.more()),
      this.ws.ev.on("timelapse.progress", (e) => this.building(e)),
      this.ws.ev.on("timelapse.changed", (e) => { if (e.error) toast(e.error, "error"); this.load(); })];
    await this.load();
  }

  close() {
    if (!this.el) return;
    this.pause();
    removeEventListener("keydown", this.onKey, true);
    this.ro.disconnect();
    for (const off of this.off || []) if (typeof off === "function") off();
    this.el.classList.remove("on");
    const el = this.el; this.el = null;
    setTimeout(() => el.remove(), 180);
  }

  async load() {
    const [r, board] = await Promise.all([api(`/api/projects/${encodeURIComponent(this.pid)}/timelapse`),
      api(`/api/projects/${encodeURIComponent(this.pid)}/board`).catch(() => null)]);
    this.film.setShapes(partShapes(board));
    this.frames = r.frames || []; this.history = r.history || 0; this.keys = new Map();
    this.film.bounds = null;
    this.slider.max = Math.max(0, this.frames.length - 1);
    this.drawMarks();
    this.renderEmpty(r.building);
    const last = this.frames.length - 1;
    if (last >= 0) { this.film.fit(this.stateAt(last)); this.seek(Math.min(last, Math.max(0, last)), false); }
    if (this.frames.length > 1 && !r.building) { this.seek(0); this.play(); }
  }

  renderEmpty(building) {
    clear(this.emptyEl);
    this.emptyEl.style.display = this.frames.length > 1 && !building ? "none" : "";
    this.histBtn.disabled = !!building;
    if (building) { this.emptyEl.append(h("span.spinner"), h("b", "Rebuilding from history…"), this.progEl = h("span.muted", "")); return; }
    if (this.frames.length <= 1) this.emptyEl.append(icon("history", 22), h("b", "No frames yet"),
      h("span.muted", "Each change to the board is recorded as a frame."),
      this.history > 1 ? h("button.btn.primary", { onclick: () => this.fromHistory() }, icon("history", 14), `Build from ${this.history} checkpoints`) : null);
  }

  building(e) { if (this.progEl) this.progEl.textContent = `${e.done} of ${e.total} versions`; }

  async fromHistory() {
    try { await api(`/api/projects/${encodeURIComponent(this.pid)}/timelapse/history`, { body: {} }); this.renderEmpty(true); }
    catch (e) { toast(e.message, "error"); }
  }

  // new frames while the player is open (Claude or the router at work): follow them at the end
  async more() {
    if (this.fetching) return;
    this.fetching = true;
    try {
      const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/timelapse?since=${this.frames.length}`);
      const atEnd = this.i >= this.frames.length - 1;
      this.frames.push(...(r.frames || []));
      this.slider.max = Math.max(0, this.frames.length - 1);
      this.drawMarks();
      this.renderEmpty(r.building);
      if (atEnd && !this.playing) this.seek(this.frames.length - 1, true);
    } finally { this.fetching = false; }
  }

  // the picture at frame i: from the nearest remembered keyframe forward
  stateAt(i) {
    if (i < 0 || !this.frames.length) return null;
    if (i === this.i && this.st) return this.st;
    let k = i;
    while (k > 0 && !this.frames[k].k && !this.keys.has(k)) k--;
    let st = this.keys.get(k) || null;
    for (let j = this.keys.has(k) ? k + 1 : k; j <= i; j++) {
      st = applyFrame(st, this.frames[j]);
      if (j % 40 === 0 && !this.keys.has(j)) { this.keys.set(j, st); if (this.keys.size > 60) this.keys.delete(this.keys.keys().next().value); }
    }
    return st;
  }

  seek(i, animate) {
    if (!this.frames.length) { this.redraw(); return; }
    i = Math.max(0, Math.min(this.frames.length - 1, i));
    const prev = animate && i === this.i + 1 ? this.st : null;
    const st = prev ? applyFrame(prev, this.frames[i]) : this.stateAt(i);
    this.i = i; this.st = st;
    this.glow = changes(prev || this.stateAt(i - 1), this.frames[i]);
    this.slider.value = i;
    this.renderInfo();
    cancelAnimationFrame(this.anim);
    if (!prev) { this.film.draw(st, { glow: this.glow, g: 0.7 }); return; }
    const dur = this.dur(this.frames[i]), t0 = performance.now();
    const step = (now) => {
      const u = Math.min(1, (now - t0) / dur);
      this.film.draw(st, { prev, t: Math.min(1, u * 1.6), glow: this.glow, g: 1 - u * 0.6 });
      if (u < 1) this.anim = requestAnimationFrame(step);
      else if (this.playing) this.timer = setTimeout(() => this.advance(), 0);
    };
    this.anim = requestAnimationFrame(step);
  }

  dur(fr) { return (fr.tmp ? 140 : fr.k ? 500 : 700) / this.speed; }

  redraw() { if (this.st) this.film.draw(this.st, { glow: this.glow, g: 0.7 }); }

  advance() {
    if (!this.playing) return;
    if (this.i >= this.frames.length - 1) { this.pause(); return; }
    this.seek(this.i + 1, true);
  }

  play() {
    if (!this.frames.length) return;
    if (this.i >= this.frames.length - 1) this.seek(0);
    this.playing = true;
    clear(this.playBtn).appendChild(icon("square", 13)); this.playBtn.dataset.tip = "Pause";
    this.advance();
  }

  pause() { this.playing = false; clearTimeout(this.timer); if (this.playBtn) { clear(this.playBtn).appendChild(icon("play", 14)); this.playBtn.dataset.tip = "Play"; } }
  toggle() { this.playing ? this.pause() : this.play(); }

  renderInfo() {
    const fr = this.frames[this.i] || {}, s = (this.st && this.st.stats) || {};
    const [name, css] = SRC[fr.src] || [fr.src || "", "var(--text-3)"];
    this.countEl.textContent = `${this.i + 1} / ${this.frames.length}`;
    this.whenEl.textContent = fr.t ? new Date(fr.t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
    clear(this.noteEl).append(h("span.tl-src", { style: { background: css } }, name), h("span", fr.note || (fr.k ? "the board" : "")));
    const pct = s.nets ? Math.round(100 * (s.routed || 0) / s.nets) : 0;
    clear(this.statsEl).append(
      stat("Parts on the board", `${s.on ?? "?"}`, s.parts ? `of ${s.parts}` : ""), stat("Tracks", s.tracks ?? 0), stat("Vias", s.vias ?? 0),
      stat("Copper", `${s.len ?? 0}`, "mm"),
      h("div.tl-stat", h("span", "Nets with copper"), h("b", `${s.routed ?? 0}`, h("small", ` of ${s.nets ?? 0}`)), h("div.tl-meter", h("i", { style: { width: pct + "%" } }))));
  }

  // who changed what, along the timeline
  drawMarks() {
    const cv = this.marks;
    if (!cv) return;
    const r = cv.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.max(1, Math.round(r.width * dpr)); cv.height = Math.max(1, Math.round(r.height * dpr));
    const c = cv.getContext("2d"), n = this.frames.length;
    c.clearRect(0, 0, cv.width, cv.height);
    if (n < 2) return;
    for (let i = 0; i < n; i++) {
      const fr = this.frames[i], col = (SRC[fr.src] || SRC.history)[2];
      const x = (i / (n - 1)) * (cv.width - 2 * dpr) + dpr;
      c.fillStyle = rgba(col, fr.tmp ? 0.45 : 0.95);
      c.fillRect(x - dpr / 2, fr.tmp ? cv.height * 0.45 : 0, Math.max(1, dpr), fr.tmp ? cv.height * 0.55 : cv.height);
    }
  }

  // The whole timeline as a video (the canvas recorded while it plays), saved in build/timelapse.
  async exportVideo() {
    if (!window.MediaRecorder || !this.canvas.captureStream) { toast("Video recording isn't supported here", "error"); return; }
    if (this.frames.length < 2) { toast("Nothing to export yet", "warn"); return; }
    const mime = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm"].find((m) => MediaRecorder.isTypeSupported(m));
    if (!mime) { toast("No supported video format", "error"); return; }
    this.pause();
    const W = 1600, H = 1000;
    const out = h("canvas"); out.width = W; out.height = H;
    const film = new Film(out); film.setShapes(this.film.shapes); film.bounds = this.film.bounds;
    const stream = out.captureStream(30);
    const rec = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 5_000_000 });
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    const done = new Promise((res) => { rec.onstop = res; });
    const oc = out.getContext("2d");
    const caption = (fr, st) => {
      const [name] = SRC[fr.src] || [fr.src || ""];
      oc.setTransform(1, 0, 0, 1, 0, 0);
      oc.fillStyle = "rgba(13,14,17,.8)"; oc.fillRect(0, H - 56, W, 56);
      oc.font = "600 22px -apple-system, system-ui, sans-serif"; oc.fillStyle = "#ececee"; oc.textBaseline = "middle"; oc.textAlign = "left";
      oc.fillText(`${this.ws.p.name}  ·  ${name}: ${fr.note || ""}`.slice(0, 110), 24, H - 28);
      const s = st.stats || {};
      oc.textAlign = "right"; oc.fillStyle = "#b9bac1";
      oc.fillText(`${s.on ?? "?"} parts · ${s.tracks ?? 0} tracks · ${s.vias ?? 0} vias · ${s.routed ?? 0}/${s.nets ?? 0} nets`, W - 24, H - 28);
    };
    const bar = h("div.tl-exporting", h("span.spinner"), h("b", "Recording the timelapse…"), h("span.muted.pct", "0%"));
    this.el.querySelector(".tl-stage").appendChild(bar);
    rec.start(250);
    let st = null;
    const n = this.frames.length;
    const per = Math.max(2, Math.min(18, Math.round(1800 / n)));    // frames of video per board frame: ~1 minute at most
    for (let i = 0; i < n; i++) {
      const prev = st;
      st = applyFrame(st, this.frames[i]);
      const glow = changes(prev, this.frames[i]);
      for (let f = 0; f < per; f++) {
        const u = (f + 1) / per;
        film.draw(st, { prev: prev && !this.frames[i].k ? prev : null, t: Math.min(1, u * 1.6), glow, g: 1 - u * 0.6, fixed: [W, H] });
        caption(this.frames[i], st);
        await new Promise((r) => setTimeout(r, 1000 / 30));
      }
      bar.querySelector(".pct").textContent = `${Math.round(100 * (i + 1) / n)}%`;
      if (!this.el) { rec.stop(); return; }
    }
    for (let f = 0; f < 45; f++) { film.draw(st, { fixed: [W, H] }); caption(this.frames[n - 1], st); await new Promise((r) => setTimeout(r, 1000 / 30)); }
    rec.stop();
    await done;
    bar.querySelector("b").textContent = "Saving…";
    const ext = mime.startsWith("video/mp4") ? "mp4" : "webm";
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(this.pid)}/timelapse/video?ext=${ext}`, { method: "POST", body: new Blob(chunks, { type: mime.split(";")[0] }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || r.statusText);
      bar.remove();
      toast(`Saved ${d.path}`, "ok");
      if (isNative) native.reveal(d.abs); else window.open(`/api/projects/${encodeURIComponent(this.pid)}/file?raw=1&path=${encodeURIComponent(d.path)}`, "_blank");
    } catch (e) { bar.remove(); toast(e.message, "error"); }
  }
}

function stat(label, value, unit) { return h("div.tl-stat", h("span", label), h("b", String(value), unit ? h("small", " " + unit) : null)); }
