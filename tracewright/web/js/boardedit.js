// Editing the board in the app: parts moved, turned, flipped and locked; tracks routed round what is in the way, with
// vias; segments and vias dragged; pours and keep-outs drawn, reshaped and moved; copper deleted. Each change is one
// undo step, saved to the board (live in KiCad when it has the board open) and told to Claude at its next turn.
// Editing waits while Claude works on the design: two writers on one board would lose one's work.
import { h, clear, api, toast } from "./util.js";
import { icon } from "./icons.js";
import { Obstacles, walk, posture, netClass, segDist, inPoly, polysMeet, bboxOf, fwd } from "./boardgeom.js";

const enc = encodeURIComponent;
const GRIDS = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 1.27, 2.54];
const WIDTHS = [0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.8, 1, 1.5, 2];
const HIT = 5;                                          // px: how near the cursor has to be to pick a thing
const short = (n) => (n || "no net").split("/").pop();
const near = (a, b, e = 1e-3) => Math.abs(a[0] - b[0]) < e && Math.abs(a[1] - b[1]) < e;
const fmt = (v) => (Math.round(v * 1000) / 1000).toString();

export class BoardEditor {
  constructor(v) {
    this.v = v; this.ws = v.ws; this.pid = v.pid;
    this.on = false;
    this.tool = "select";
    this.items = new Set();                             // the copper picked: "t:<id>", "v:<id>", "z:<id>"
    this.hist = { undo: 0, redo: 0, undo_label: "", redo_label: "" };
    this.busy = !!(this.ws.chat && this.ws.chat.busy);
    this.grid = parseFloat(localStorage.getItem("tw.edit.grid")) || 0.1;
    this.width = 0;                                     // 0: the net class's width
    this.layer = null;                                  // the layer new copper goes on
    this.drag = null; this.route = null; this.shape = null; this.pending = null; this.nudge = null;
    this.saving = 0; this.status = ""; this.cursor = null; this.pop = null;
    this.build();
    if (this.ws.ev) this.ws.ev.on("agent.status", (e) => this.setBusy(!!e.busy));
  }

  // ------------------------------------------------------------------ the edit bar
  build() {
    this.btn = h("button.tbtn.editbtn", { onclick: () => this.toggle(), "data-tip": "Edit the board", "data-kbd": "e" }, icon("pencil", 15), h("span", "Edit"));
    this.barBox = h("div.hudbox.editbar");
    this.bar = h("div.hud.bc", { style: { display: "none" } }, this.barBox);
    this.hint = h("div.vhint.ehint", { style: { display: "none" } });
    this.toolBtns = {};
  }

  mount() {
    this.v.viewer.appendChild(this.bar);
    this.v.hudTc.appendChild(this.hint);
  }

  toggle(on = !this.on) {
    if (on === this.on) return;
    this.on = on;
    this.v.viewer.classList.toggle("editing", on);
    this.btn.classList.toggle("on", on);
    if (!on) { this.cancelAll(); this.items.clear(); this.v.renderSelBar(); }
    else {
      if (this.v.flags.active) this.v.flags.toggle(false);
      if (this.v.tool !== "select") this.v.setTool("select");
      this.loadHistory();
    }
    this.bar.style.display = on ? "" : "none";
    this.renderBar();
    this.showHint();
    this.v.dirty();
  }

  async loadHistory() {
    try { this.hist = await api(`/api/projects/${enc(this.pid)}/board/history`); } catch { /* no board */ }
    this.renderBar();
  }

  setBusy(b) {
    if (b === this.busy) return;
    this.busy = b;
    if (b) this.cancelAll();
    this.renderBar();
    this.showHint();
  }

  cancelAll() {
    if (this.drag) this.dropDrag();
    this.route = null; this.shape = null; this.closePop();
    if (this.nudge) { clearTimeout(this.nudge.t); this.flushNudge(); }
    if (this.tool !== "select") this.tool = "select";
    this.v.dirty();
  }

  setTool(t) {
    if (!this.on) this.toggle(true);
    if (this.busy) return;
    if (this.tool === t && t !== "select") t = "select";
    this.route = null; this.shape = null; this.closePop();
    this.tool = t;
    if (t !== "select") { this.items.clear(); this.v.sel.clear(); this.v.selNet = null; this.v.updateSel(); }
    this.v.viewer.classList.toggle("tool-draw", t !== "select");
    this.renderBar();
    this.showHint();
    this.v.dirty();
  }

  copperLayers() { return (this.v.data && this.v.data.copper) || ["F.Cu", "B.Cu"]; }
  curLayer() {
    const cu = this.copperLayers();
    if (!this.layer || !cu.includes(this.layer)) this.layer = this.v.side === "B" ? cu[cu.length - 1] : cu[0];
    return this.layer;
  }

  renderBar() {
    const b = clear(this.barBox);
    if (!this.on) return;
    const dis = this.busy || !this.v.data;
    const tb = (key, ic, label, kbd) => {
      const el = h("button.tbtn" + (this.tool === key ? ".on" : ""), { onclick: () => this.setTool(key), "data-tip": label, "data-kbd": kbd, disabled: dis || undefined }, icon(ic, 15));
      this.toolBtns[key] = el;
      return el;
    };
    const cu = this.copperLayers();
    const layerSel = h("select.esel", { "data-tip": "The layer new copper goes on", disabled: dis || undefined,
      onchange: (e) => this.pickLayer(e.target.value) }, cu.map((l) => {
        const role = cu.length > 2 ? this.v.layerRole(l) : "";
        return h("option", { value: l, selected: l === this.curLayer() || undefined }, role && role !== "signal" ? `${l} (${role})` : l);
      }));
    const sw = h("i.lsw", { style: { background: this.v.layerColor(this.curLayer()) } });
    const widthSel = h("select.esel", { "data-tip": "Track width", disabled: dis || undefined, onchange: (e) => { this.width = parseFloat(e.target.value) || 0; if (this.route) this.routeWidth(); this.renderBar(); } },
      h("option", { value: "0", selected: !this.width || undefined }, "Width: net class"), WIDTHS.map((w) => h("option", { value: String(w), selected: this.width === w || undefined }, `${w} mm`)));
    const gridSel = h("select.esel", { "data-tip": "Grid", disabled: dis || undefined, onchange: (e) => { this.grid = parseFloat(e.target.value); localStorage.setItem("tw.edit.grid", String(this.grid)); } },
      GRIDS.map((g) => h("option", { value: String(g), selected: Math.abs(g - this.grid) < 1e-9 || undefined }, `Grid ${g} mm`)));
    const undo = h("button.tbtn", { onclick: () => this.undo(), disabled: dis || !this.hist.undo || undefined, "data-tip": this.hist.undo ? `Undo: ${this.hist.undo_label}` : "Nothing to undo", "data-kbd": "mod+z" }, icon("undo-2", 15));
    const redo = h("button.tbtn", { onclick: () => this.redo(), disabled: dis || !this.hist.redo || undefined, "data-tip": this.hist.redo ? `Redo: ${this.hist.redo_label}` : "Nothing to redo", "data-kbd": "mod+shift+z" }, icon("redo-2", 15));
    const st = this.busy ? h("span.estat.wait", icon("hourglass", 13), "Claude is working: editing waits until it is done")
      : this.saving ? h("span.estat", h("span.spinner"), this.status || "Saving")
      : this.status ? h("span.estat.ok", icon("check", 13), this.status) : null;
    b.append(
      tb("select", "mouse-pointer-2", "Select and move"), tb("route", "route", "Route a track", "x"), tb("pour", "paint-bucket", "Draw a pour", "p"),
      tb("keepout", "ban", "Draw a keep-out", "k"), h("div.tsep"),
      h("label.eslab", sw, layerSel), widthSel, gridSel, h("div.tsep"), undo, redo,
      st ? h("div.tsep") : null, st,
      h("div.tsep"), h("button.tbtn", { onclick: () => this.toggle(false), "data-tip": "Stop editing", "data-kbd": "e" }, "Done"));
  }

  pickLayer(l) {
    if (this.route && this.route.layer !== l) { this.routeVia(l); return; }
    this.layer = l;
    this.renderBar();
  }

  showHint(text) {
    const t = text || this.hintText();
    clear(this.hint);
    if (!t || !this.on) { this.hint.style.display = "none"; return; }
    this.hint.style.display = "";
    this.hint.append(icon(this.busy ? "hourglass" : this.tool === "route" ? "route" : this.tool === "select" ? "mouse-pointer-2" : this.tool === "pour" ? "paint-bucket" : "ban", 14), h("span", t));
  }

  hintText() {
    if (this.busy) return "Claude is working on the design. Leave a flag, or edit when it is done.";
    if (this.tool === "route") {
      if (!this.route) return "Click a pad, track or via to start";
      return `${short(this.route.net)} on ${this.route.layer}, ${fmt(this.route.w)} mm · click to fix · V via · / bend · Backspace back · Enter or double-click finishes · Esc cancels`;
    }
    if (this.tool === "pour" || this.tool === "keepout") {
      const what = this.tool === "pour" ? "pour" : "keep-out";
      if (!this.shape) return `Click the corners of the ${what}`;
      return `Click to add corners · click the first one, double-click or Enter to close · Backspace takes one back · Esc cancels`;
    }
    return "";
  }

  // ------------------------------------------------------------------ picking
  pickCopper(x, y) {
    const d = this.v.data;
    if (!d) return null;
    const tol = HIT / this.v.scale;
    let best = null;
    d.vias.forEach((v, i) => {
      const dd = Math.hypot(x - v[0], y - v[1]) - v[2] / 2;
      if (dd < tol && (!best || dd < best.d - 1e-6)) best = { kind: "via", i, d: dd, id: d.vid[i] };
    });
    if (best) return best;
    const layers = this.v.visibleCopper();
    d.tracks.forEach((t, i) => {
      if (!layers.includes(t[5])) return;
      const dd = segDist(x, y, t[0], t[1], t[2], t[3]) - t[4] / 2;
      if (dd < tol && (!best || dd < best.d || (Math.abs(dd - best.d) < 1e-6 && layers.indexOf(t[5]) > layers.indexOf(d.tracks[best.i][5])))) best = { kind: "track", i, d: dd, id: d.tid[i] };
    });
    return best;
  }

  // a pour or keep-out by its outline (or, with inside, anywhere in it), smallest first
  pickZone(x, y, inside) {
    const d = this.v.data;
    if (!d) return null;
    const tol = HIT / this.v.scale, vis = this.v.visibleCopper();
    let best = null;
    for (const z of d.zones) {
      if (!z.o || !z.o.length || !(z.l || []).some((l) => vis.includes(l) || l === "*.Cu" || l === "F&B.Cu")) continue;
      const o = z.o[0];
      let edge = Infinity;
      for (let i = 0, j = o.length - 1; i < o.length; j = i++) edge = Math.min(edge, segDist(x, y, o[j][0], o[j][1], o[i][0], o[i][1]));
      const hit = edge < tol || (inside && inPoly(x, y, o));
      if (!hit) continue;
      const bb = bboxOf(o), area = (bb[2] - bb[0]) * (bb[3] - bb[1]);
      if (!best || area < best.area) best = { z, area };
    }
    return best ? best.z : null;
  }

  padAt(x, y) {
    const f = this.v.pickFp(x, y);
    if (!f) return null;
    const pose = this.v.pose(f);
    const [lx, ly] = pose ? inv(pose, f, x, y) : [x, y];
    for (const p of f.pads) for (const pl of p.p) if (inPoly(lx, ly, pl)) {
      const c = pose ? fwd(pose, f, p.x, p.y) : [p.x, p.y];
      return { f, p, c, polys: p.p.map((q) => (pose ? q.map(([a, b]) => fwd(pose, f, a, b)) : q)) };
    }
    return null;
  }

  zoneById(id) { return (this.v.data && this.v.data.zones.find((z) => z.id === id)) || null; }
  selZone() {
    if (this.items.size !== 1) return null;
    const k = [...this.items][0];
    return k.startsWith("z:") ? this.zoneById(k.slice(2)) : null;
  }

  snap(x, y, e) {
    if (e && e.altKey) return [x, y];
    const g = this.grid;
    return [Math.round(x / g) * g, Math.round(y / g) * g];
  }

  // ------------------------------------------------------------------ keys
  key(e) {
    const k = e.key, low = k.length === 1 ? k.toLowerCase() : k, mod = e.metaKey || e.ctrlKey;
    if (!this.on) {
      if (low === "e" && !mod && !e.altKey && this.v.data) { this.toggle(true); return true; }
      return false;
    }
    if (mod && low === "z") { if (e.shiftKey) this.redo(); else this.undo(); return true; }
    if (mod && low === "y") { this.redo(); return true; }
    if (mod || e.altKey) return false;
    if (low === "e" && !this.route && !this.shape) { this.toggle(false); return true; }
    if (this.busy) return false;
    if (this.pop) { if (k === "Escape") { this.closePop(); this.shape = null; this.showHint(); this.v.dirty(); return true; } return false; }
    if (this.route) {
      if (low === "v") { this.routeVia(); return true; }
      if (k === "Backspace" || k === "Delete") { this.routeBack(); return true; }
      if (k === "Enter") { this.routeFinish(); return true; }
      if (k === "Escape") { this.route = null; this.showHint(); this.v.dirty(); return true; }
      if (k === "/") { this.route.diag = !this.route.diag; this.routeUpdate(); return true; }
    }
    if (this.shape) {
      if (k === "Enter") { this.shapeClose(); return true; }
      if (k === "Backspace" || k === "Delete") { this.shape.pts.pop(); if (!this.shape.pts.length) this.shape = null; this.showHint(); this.v.dirty(); return true; }
      if (k === "Escape") { this.shape = null; this.showHint(); this.v.dirty(); return true; }
    }
    if (this.drag) {
      if (low === "r" && this.drag.kind === "fp") { this.drag.rot = (this.drag.rot + 90) % 360; this.dragMove(...this.drag.last, null); return true; }
      if (k === "Escape") { this.dropDrag(); return true; }
      return true;
    }
    if (low === "x") { this.setTool("route"); return true; }
    if (low === "p") { this.setTool("pour"); return true; }
    if (low === "k") { this.setTool("keepout"); return true; }
    if (low === "r" && this.v.sel.size) { this.rotateSel(); return true; }
    if (low === "l" && this.v.sel.size) { this.lockSel(); return true; }
    if ((k === "Delete" || k === "Backspace") && (this.items.size || this.v.sel.size)) { this.deleteSel(); return true; }
    if (k.startsWith("Arrow") && this.v.sel.size && this.tool === "select") {
      const s = this.grid * (e.shiftKey ? 10 : 1);
      this.nudgeBy(k === "ArrowLeft" ? -s : k === "ArrowRight" ? s : 0, k === "ArrowUp" ? -s : k === "ArrowDown" ? s : 0);
      return true;
    }
    if (k === "Escape") {
      if (this.tool !== "select") { this.setTool("select"); return true; }
      if (this.items.size) { this.items.clear(); this.v.renderSelBar(); this.v.dirty(); return true; }
    }
    return false;
  }

  // ------------------------------------------------------------------ the mouse
  down(e, px, py) {
    if (!this.on || this.busy || e.button !== 0 || !this.v.data || this.tool !== "select" || this.pop) return false;
    const [x, y] = this.v.toWorld(px, py);
    const z = this.selZone();
    if (z) {                                           // a corner or an edge's middle of the picked zone
      const hnd = this.zoneHandle(z, px, py);
      if (hnd) { this.startVertexDrag(z, hnd, x, y); return true; }
    }
    const under = this.v.pickFp(x, y);
    const c = under && this.v.sel.has(under.ref) ? null : this.pickCopper(x, y);   // a picked part wins over copper on it
    if (c) {
      const key = (c.kind === "via" ? "v:" : "t:") + c.id;
      if (e.shiftKey) { if (this.items.has(key)) this.items.delete(key); else this.items.add(key); }
      else if (!this.items.has(key)) { this.items = new Set([key]); }
      this.v.sel.clear(); this.v.selNet = null; this.v.updateSel();
      if (!e.shiftKey && this.items.size === 1) {
        if (c.kind === "track") this.startSegDrag(c.i, x, y);
        else this.startViaDrag(c.i, x, y);
      }
      this.v.dirty();
      return true;
    }
    const ze = this.pickZone(x, y, false);             // a pour's or keep-out's edge, right under the cursor
    if (ze) {
      const key = "z:" + ze.id, was = this.items.has(key);
      this.items = e.shiftKey ? new Set([...this.items, key]) : new Set([key]);
      this.v.sel.clear(); this.v.selNet = null; this.v.updateSel();
      if (was && !e.shiftKey) this.startZoneMove(ze, x, y);
      this.v.dirty();
      return true;
    }
    const f = under;
    if (f) {
      this.items.clear();
      if (e.shiftKey) { if (this.v.sel.has(f.ref)) this.v.sel.delete(f.ref); else this.v.sel.add(f.ref); this.v.selNet = null; this.v.updateSel(); this.v.dirty(); return true; }
      if (!this.v.sel.has(f.ref)) { this.v.sel = new Set([f.ref]); this.v.selNet = null; this.v.updateSel(); }
      const refs = [...this.v.sel].filter((r) => this.v.byRef[r]);
      const locked = refs.filter((r) => this.v.byRef[r].locked);
      if (locked.length) { this.flash(`${locked.join(", ")} ${locked.length === 1 ? "is" : "are"} locked. Unlock to move (L).`); this.v.dirty(); return true; }
      this.drag = { kind: "fp", refs, x0: x, y0: y, rot: 0, moved: false, last: [x, y] };
      this.v.dirty();
      return true;
    }
    if (z && inPoly(x, y, z.o[0])) { this.startZoneMove(z, x, y); return true; }
    return false;
  }

  move(e, px, py) {
    if (!this.on || !this.v.data) return false;
    const [x, y] = this.v.toWorld(px, py);
    this.cursor = [x, y];
    if (this.drag) { this.dragMove(x, y, e); return true; }
    if (this.route) this.routeHover(x, y, e);
    if (this.shape) { this.shape.cur = this.shapePoint(x, y, e); this.v.dirty(); }
    return false;
  }

  up(e) {
    if (!this.drag) return false;
    const d = this.drag;
    this.drag = null;
    this.v.viewer.classList.remove("dragging");
    if (!d.moved && !(d.kind === "fp" && d.rot)) { this.restoreDrag(d); this.v.renderSelBar(); this.v.dirty(); return true; }
    this.finishDrag(d);
    return true;
  }

  // a click the view did not use for panning: the drawing tools, and picking a pour or keep-out by its edge
  click(px, py, shift, e) {
    if (!this.on || !this.v.data || this.busy) return false;
    const [x, y] = this.v.toWorld(px, py);
    if (this.tool === "route") { this.routeClick(x, y, e); return true; }
    if (this.tool === "pour" || this.tool === "keepout") { this.shapeClick(x, y, e); return true; }
    const z = this.pickZone(x, y, false);
    if (z) {
      const key = "z:" + z.id;
      this.items = shift ? new Set([...this.items, key]) : new Set([key]);
      this.v.sel.clear(); this.v.selNet = null; this.v.updateSel();
      this.v.dirty();
      return true;
    }
    if (this.items.size && !shift) { this.items.clear(); this.v.renderSelBar(); this.v.dirty(); }
    return false;
  }

  dbl(e, px, py) {
    if (!this.on || !this.v.data || this.busy) return false;
    const [x, y] = this.v.toWorld(px, py);
    if (this.route) { this.routeFinish(); return true; }
    if (this.shape) { this.shapeClose(); return true; }
    if (this.tool === "select") {                      // a pour picked from inside it
      const z = this.pickZone(x, y, true);
      if (z) { this.items = new Set(["z:" + z.id]); this.v.sel.clear(); this.v.updateSel(); this.v.dirty(); return true; }
    }
    return false;
  }

  // ------------------------------------------------------------------ moving parts
  dragMove(x, y, e) {
    const d = this.drag;
    d.last = [x, y];
    const g = (e && e.altKey) ? 0 : this.grid;
    const sn = (v) => (g ? Math.round(v / g) * g : v);
    if (Math.abs(x - d.x0) * this.v.scale + Math.abs(y - d.y0) * this.v.scale > 3) d.moved = true;
    if (!d.moved && !(d.kind === "fp" && d.rot)) return;
    this.v.viewer.classList.add("dragging");
    this.v.tip.style.display = "none";
    if (d.kind === "fp") {
      const dx = sn(x - d.x0), dy = sn(y - d.y0);
      for (const r of d.refs) {
        const f = this.v.byRef[r];
        this.v.override[r] = { x: f.x + dx, y: f.y + dy, rot: (f.a + d.rot) % 360 };
      }
      d.dx = dx; d.dy = dy;
      d.bad = this.partConflicts(d.refs);
      this.dragTip(`${d.refs.length === 1 ? d.refs[0] : d.refs.length + " parts"}: ${dx >= 0 ? "+" : ""}${fmt(dx)}, ${dy >= 0 ? "+" : ""}${fmt(dy)} mm${d.rot ? ` · turned ${d.rot}°` : ""}${d.bad.length ? " · " + d.bad[0] : ""}`, !!d.bad.length);
    } else if (d.kind === "seg") this.segDragMove(d, x, y, sn);
    else if (d.kind === "via") this.viaDragMove(d, x, y, sn);
    else if (d.kind === "vertex") { d.pts[d.k] = [sn(x), sn(y)]; d.moved = true; }
    else if (d.kind === "zone") { const dx = sn(x - d.x0), dy = sn(y - d.y0); d.pts = d.orig.map(([a, b]) => [a + dx, b + dy]); }
    this.v.dirty();
  }

  dragTip(text, bad) {
    const t = this.v.tip;
    if (!this.cursor) return;
    const [sx, sy] = this.v.toScreen(...this.cursor);
    t.innerHTML = "";
    t.append(h("span", { style: { color: bad ? "#ff9a95" : "" } }, text));
    t.style.display = "block";
    t.style.left = Math.min(sx + 18, this.v.w - 300) + "px";
    t.style.top = Math.min(sy + 16, this.v.h - 40) + "px";
  }

  // what the moved parts run into: another part's courtyard on the same side, or the board's edge
  partConflicts(refs) {
    const out = [], moved = new Set(refs);
    const shapeOf = (f) => {
      const p = this.v.pose(f);
      const loops = (f.cy && f.cy.length ? f.cy : [[[f.bbox[0], f.bbox[1]], [f.bbox[2], f.bbox[1]], [f.bbox[2], f.bbox[3]], [f.bbox[0], f.bbox[3]]]]);
      return loops.map((l) => (p ? l.map(([x, y]) => fwd(p, f, x, y)) : l));
    };
    const obs = this.edgeObs || (this.edgeObs = new Obstacles({ data: { ...this.v.data, tracks: [], vias: [], zones: [] }, fps: [], pose: () => null }, "F.Cu", ""));
    for (const r of refs) {
      const f = this.v.byRef[r];
      if (!f) continue;
      const mine = shapeOf(f);
      const bb = bboxOf(mine.flat());
      if (mine.flat().some(([x, y]) => !obs.inside(x, y))) { out.push(`${r} is past the board's edge`); continue; }
      for (const o of this.v.fps) {
        if (moved.has(o.ref) || o.side !== f.side || !o.pads.length) continue;
        const ob = o.bbox;
        if (ob[0] > bb[2] || ob[2] < bb[0] || ob[1] > bb[3] || ob[3] < bb[1]) continue;
        const theirs = shapeOf(o);
        if (mine.some((a) => theirs.some((b) => polysMeet(a, b)))) { out.push(`${r} overlaps ${o.ref}`); break; }
      }
    }
    return out;
  }

  restoreDrag(d) {
    if (d.kind === "fp") for (const r of d.refs) delete this.v.override[r];
    this.v.tip.style.display = "none";
  }

  dropDrag() {
    const d = this.drag;
    this.drag = null;
    if (d) this.restoreDrag(d);
    this.v.viewer.classList.remove("dragging");
    this.v.dirty();
  }

  async finishDrag(d) {
    this.v.tip.style.display = "none";
    if (d.kind === "fp") {
      const ops = d.refs.map((r) => { const o = this.v.override[r]; return { op: "move", ref: r, x: round(o.x), y: round(o.y), rot: round(o.rot) }; });
      const label = d.refs.length === 1 ? `Move ${d.refs[0]}` : `Move ${d.refs.length} parts`;
      if (d.bad && d.bad.length) this.flash(d.bad.slice(0, 2).join("; "));
      const r = await this.commit(ops, d.rot && !d.dx && !d.dy ? label.replace("Move", "Turn") : label, { refill: true });
      if (!r) { for (const ref of d.refs) delete this.v.override[ref]; this.v.dirty(); }
      return;
    }
    if (d.kind === "seg" || d.kind === "via") {
      if (d.hit) { this.flash(`Too close to ${describeHit(d.hit)}`); this.v.dirty(); return; }
      if (!d.ops || !d.ops.length) { this.v.dirty(); return; }
      this.pending = { tracks: d.preview || [], vias: d.viaPreview ? [d.viaPreview] : [], hide: d.hide };
      const r = await this.commit(d.ops, d.kind === "seg" ? `Drag a ${short(d.net)} track` : `Move a ${short(d.net)} via`, { refill: true });
      if (!r) this.pending = null;
      this.v.dirty();
      return;
    }
    if (d.kind === "vertex" || d.kind === "zone") {
      const z = d.z;
      this.pending = { zone: { id: z.id, pts: d.pts } };
      const r = await this.commit([{ op: "zone_set", uuid: z.id, polygon: d.pts.map(([x, y]) => [round(x), round(y)]) }],
        `${d.kind === "zone" ? "Move" : "Reshape"} the ${z.rule ? "keep-out" : short(z.net) + " pour"}`, { refill: true });
      if (!r) this.pending = null;
      this.v.dirty();
    }
  }

  async rotateSel() {
    const refs = [...this.v.sel].filter((r) => this.v.byRef[r]);
    const locked = refs.filter((r) => this.v.byRef[r].locked);
    if (locked.length) { this.flash(`${locked.join(", ")} ${locked.length === 1 ? "is" : "are"} locked`); return; }
    const ops = refs.map((r) => { const f = this.v.byRef[r]; const p = this.v.pose(f) || { x: f.x, y: f.y, rot: f.a }; this.v.override[r] = { ...p, rot: (p.rot + 90) % 360 }; return { op: "move", ref: r, x: round(p.x), y: round(p.y), rot: round((p.rot + 90) % 360) }; });
    this.v.dirty();
    const r = await this.commit(ops, refs.length === 1 ? `Turn ${refs[0]}` : `Turn ${refs.length} parts`, { refill: true });
    if (!r) { for (const ref of refs) delete this.v.override[ref]; this.v.dirty(); }
  }

  async flipSel() {
    const refs = [...this.v.sel].filter((r) => this.v.byRef[r]);
    if (refs.some((r) => this.v.byRef[r].locked)) { this.flash("Unlock the parts to flip them"); return; }
    const ops = refs.map((r) => { const f = this.v.byRef[r]; return { op: "move", ref: r, x: round(f.x), y: round(f.y), side: f.side === "F" ? "B" : "F" }; });
    await this.commit(ops, refs.length === 1 ? `Flip ${refs[0]} to the ${this.v.byRef[refs[0]].side === "F" ? "bottom" : "top"}` : `Flip ${refs.length} parts`, { refill: true });
  }

  async lockSel() {
    const refs = [...this.v.sel].filter((r) => this.v.byRef[r]);
    if (!refs.length) return;
    const lock = !refs.every((r) => this.v.byRef[r].locked);
    const r = await this.commit([{ op: "lock", refs, locked: lock }], `${lock ? "Lock" : "Unlock"} ${refs.length === 1 ? refs[0] : refs.length + " parts"}`);
    if (r) for (const ref of refs) this.v.byRef[ref].locked = lock;
    this.v.renderSelBar();
  }

  nudgeBy(dx, dy) {
    const refs = [...this.v.sel].filter((r) => this.v.byRef[r]);
    if (refs.some((r) => this.v.byRef[r].locked)) { this.flash("Unlock the parts to move them"); return; }
    if (!this.nudge || this.nudge.refs.join() !== refs.join()) { if (this.nudge) { clearTimeout(this.nudge.t); this.flushNudge(); } this.nudge = { refs, dx: 0, dy: 0 }; }
    const n = this.nudge;
    n.dx += dx; n.dy += dy;
    for (const r of refs) { const f = this.v.byRef[r]; this.v.override[r] = { x: f.x + n.dx, y: f.y + n.dy, rot: f.a }; }
    this.v.dirty();
    clearTimeout(n.t);
    n.t = setTimeout(() => this.flushNudge(), 450);
  }

  async flushNudge() {
    const n = this.nudge;
    this.nudge = null;
    if (!n || (!n.dx && !n.dy)) return;
    const ops = n.refs.map((r) => { const o = this.v.override[r]; return { op: "move", ref: r, x: round(o.x), y: round(o.y), rot: round(o.rot) }; });
    const ok = await this.commit(ops, n.refs.length === 1 ? `Move ${n.refs[0]}` : `Move ${n.refs.length} parts`, { refill: true });
    if (!ok) { for (const r of n.refs) delete this.v.override[r]; this.v.dirty(); }
  }

  // ------------------------------------------------------------------ dragging a segment or a via
  // A segment moves square to itself; a neighbour meeting it at an angle slides along its own line to stay joined,
  // as KiCad drags at 45°; an end held by a pad, a via or a branch gets a short new segment instead.
  startSegDrag(i, x, y) {
    const d = this.v.data, t = d.tracks[i];
    if (t.length > 7) return;                          // an arc
    const A = [t[0], t[1]], B = [t[2], t[3]], L = Math.hypot(B[0] - A[0], B[1] - A[1]);
    if (L < 1e-6) return;
    const dir = [(B[0] - A[0]) / L, (B[1] - A[1]) / L], n = [-dir[1], dir[0]];
    const ends = [A, B].map((P) => this.endInfo(i, P, t));
    this.drag = { kind: "seg", i, net: t[6], layer: t[5], w: t[4], A, B, dir, n, ends, x0: x, y0: y, moved: false, last: [x, y] };
  }

  // what holds a track's end: its neighbours (same net and layer, meeting there), a pad or a via
  endInfo(i, P, t) {
    const d = this.v.data, nb = [];
    d.tracks.forEach((q, j) => {
      if (j === i || q[6] !== t[6] || q[5] !== t[5] || q.length > 7) return;
      if (near([q[0], q[1]], P)) nb.push({ j, end: "a", other: [q[2], q[3]] });
      else if (near([q[2], q[3]], P)) nb.push({ j, end: "b", other: [q[0], q[1]] });
    });
    const held = d.vias.some((v) => near([v[0], v[1]], P, 0.01)) || this.v.fps.some((f) => f.pads.some((p) => p.net === t[6] && p.p.some((pl) => inPoly(P[0], P[1], pl))));
    return { P, nb, held };
  }

  segDragMove(d, x, y, sn) {
    let delta = (x - d.x0) * d.n[0] + (y - d.y0) * d.n[1];
    delta = sn(delta);
    const off = [d.n[0] * delta, d.n[1] * delta];
    const ops = [], preview = [], hide = new Set(["t:" + this.v.data.tid[d.i]]);
    const newEnd = (e) => {
      const P2 = [e.P[0] + off[0], e.P[1] + off[1]];
      if (!e.held && e.nb.length === 1) {
        const nbt = e.nb[0], C = nbt.other;
        const ed = [e.P[0] - C[0], e.P[1] - C[1]];
        const cr = d.dir[0] * ed[1] - d.dir[1] * ed[0];
        if (Math.abs(cr) > 1e-6) {                     // where the moved segment's line meets the neighbour's
          const s = ((C[0] - P2[0]) * ed[1] - (C[1] - P2[1]) * ed[0]) / cr;
          const X = [P2[0] + d.dir[0] * s, P2[1] + d.dir[1] * s];
          const tt = Math.abs(ed[0]) > Math.abs(ed[1]) ? (X[0] - C[0]) / ed[0] : (X[1] - C[1]) / ed[1];
          if (tt > 0.02) return { at: X, slide: nbt };
        }
      }
      return { at: P2, link: e.P };
    };
    const ea = newEnd(d.ends[0]), eb = newEnd(d.ends[1]);
    const A2 = ea.at, B2 = eb.at;
    preview.push([A2, B2]);
    ops.push({ op: "track_set", uuid: this.v.data.tid[d.i], a: rnd(A2), b: rnd(B2) });
    for (const e of [ea, eb]) {
      if (e.slide) {
        hide.add("t:" + this.v.data.tid[e.slide.j]);
        preview.push([e.slide.other, e.at]);
        ops.push({ op: "track_set", uuid: this.v.data.tid[e.slide.j], [e.slide.end]: rnd(e.at) });
      } else if (Math.hypot(e.at[0] - e.link[0], e.at[1] - e.link[1]) > 1e-4) {
        preview.push([e.link, e.at]);
        ops.push({ op: "track", net: d.net, layer: d.layer, a: rnd(e.link), b: rnd(e.at), w: d.w });
      }
    }
    if (Math.abs(delta) < 1e-6) { d.ops = []; d.preview = null; d.hit = null; d.hide = null; return; }
    const obs = this.obstacles(d.layer, d.net);
    d.hit = null;
    for (const [a, b] of preview) { const hh = obs.hitSeg(a, b, d.w, netClass(this.v.data.rules || {}, d.net).cl); if (hh) { d.hit = hh; break; } }
    d.ops = ops; d.preview = preview.map(([a, b]) => ({ a, b, layer: d.layer, w: d.w })); d.hide = hide;
    this.dragTip(`${short(d.net)}: moved ${fmt(Math.abs(delta))} mm${d.hit ? " · too close to " + describeHit(d.hit) : ""}`, !!d.hit);
  }

  startViaDrag(i, x, y) {
    const d = this.v.data, v = d.vias[i], P = [v[0], v[1]];
    const ends = [];
    d.tracks.forEach((t, j) => {
      if (t[6] !== v[4] || t.length > 7) return;
      if (near([t[0], t[1]], P, 0.01)) ends.push({ j, end: "a", other: [t[2], t[3]], layer: t[5], w: t[4] });
      else if (near([t[2], t[3]], P, 0.01)) ends.push({ j, end: "b", other: [t[0], t[1]], layer: t[5], w: t[4] });
    });
    this.drag = { kind: "via", i, net: v[4], P, ends, dia: v[2], x0: x, y0: y, moved: false, last: [x, y] };
  }

  viaDragMove(d, x, y, sn) {
    const Q = [sn(d.P[0] + (x - d.x0)), sn(d.P[1] + (y - d.y0))];
    const D = this.v.data, rules = D.rules || {}, cl = netClass(rules, d.net).cl;
    const ops = [{ op: "via_set", uuid: D.vid[d.i], x: round(Q[0]), y: round(Q[1]) }], preview = [], hide = new Set(["v:" + D.vid[d.i]]);
    for (const e of d.ends) {
      hide.add("t:" + D.tid[e.j]);
      preview.push({ a: e.other, b: Q, layer: e.layer, w: e.w });
      ops.push({ op: "track_set", uuid: D.tid[e.j], [e.end]: rnd(Q) });
    }
    d.hit = null;
    const ignore = new Set(hide);
    for (const l of this.copperLayers()) {
      const obs = new Obstacles(this.v, l, d.net, { ignore });
      const hh = obs.hitCirc(Q, d.dia / 2, cl) || preview.filter((p) => p.layer === l).map((p) => obs.hitSeg(p.a, p.b, p.w, cl)).find(Boolean);
      if (hh) { d.hit = hh; break; }
    }
    d.ops = ops; d.preview = preview; d.viaPreview = { x: Q[0], y: Q[1], d: d.dia }; d.hide = hide;
    this.dragTip(`${short(d.net)} via${d.hit ? " · too close to " + describeHit(d.hit) : ""}`, !!d.hit);
  }

  // ------------------------------------------------------------------ pours and keep-outs: corners, edges, moving
  zoneHandle(z, px, py) {
    const o = z.o[0];
    for (let k = 0; k < o.length; k++) {
      const [sx, sy] = this.v.toScreen(o[k][0], o[k][1]);
      if (Math.abs(sx - px) <= 6 && Math.abs(sy - py) <= 6) return { k, insert: false };
    }
    for (let k = 0; k < o.length; k++) {
      const a = o[k], b = o[(k + 1) % o.length];
      const [sx, sy] = this.v.toScreen((a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
      if (Math.abs(sx - px) <= 5 && Math.abs(sy - py) <= 5) return { k: k + 1, insert: true };
    }
    return null;
  }

  startVertexDrag(z, hnd, x, y) {
    const pts = z.o[0].map((p) => p.slice());
    if (hnd.insert) pts.splice(hnd.k, 0, [x, y]);
    this.drag = { kind: "vertex", z, k: hnd.k, pts, x0: x, y0: y, moved: !!hnd.insert, last: [x, y] };
  }

  startZoneMove(z, x, y) {
    this.drag = { kind: "zone", z, orig: z.o[0].map((p) => p.slice()), pts: z.o[0].map((p) => p.slice()), x0: x, y0: y, moved: false, last: [x, y] };
  }

  shapePoint(x, y, e) {
    let p = this.snap(x, y, e);
    const s = this.shape;
    if (s && s.pts.length && e && e.shiftKey) {        // Shift: along 45° steps from the last corner
      const q = s.pts[s.pts.length - 1], dx = p[0] - q[0], dy = p[1] - q[1];
      const a = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) * (Math.PI / 4), L = Math.hypot(dx, dy);
      p = [q[0] + Math.cos(a) * L, q[1] + Math.sin(a) * L];
    }
    return p;
  }

  shapeClick(x, y, e) {
    if (this.pop) return;
    const p = this.shapePoint(x, y, e);
    if (!this.shape) { this.shape = { kind: this.tool, pts: [p], cur: p }; this.showHint(); this.v.dirty(); return; }
    const s = this.shape, f = s.pts[0], [fx, fy] = this.v.toScreen(f[0], f[1]), [px, py] = this.v.toScreen(p[0], p[1]);
    if (s.pts.length >= 3 && Math.hypot(fx - px, fy - py) < 8) { this.shapeClose(); return; }
    const last = s.pts[s.pts.length - 1];
    if (!near(last, p, 1e-6)) s.pts.push(p);
    this.v.dirty();
  }

  shapeClose() {
    const s = this.shape;
    if (!s) return;
    if (s.pts.length < 3) { this.flash("A pour or keep-out needs at least three corners"); return; }
    if (Math.abs(area(s.pts)) < 0.05) { this.flash("That shape has no area"); return; }
    s.closed = true;
    this.v.dirty();
    this.openShapeForm(s);
  }

  openShapeForm(s) {
    const d = this.v.data, cu = this.copperLayers();
    const bb = bboxOf(s.pts), [sx, sy] = this.v.toScreen((bb[0] + bb[2]) / 2, bb[3]);
    let body;
    const done = async (ops, label) => {
      this.closePop();
      this.pending = { shape: s };
      const r = await this.commit(ops, label, { fill: true });
      this.pending = null;
      if (r) { this.shape = null; this.showHint(); }
      else s.closed = false;
      this.v.dirty();
    };
    const cancel = () => { this.closePop(); this.shape = null; this.showHint(); this.v.dirty(); };
    if (s.kind === "pour") {
      const nets = (d.nets || []).filter((n) => n && !n.startsWith("unconnected-"));
      const gnd = nets.find((n) => /^(GND|0V|VSS)$/i.test(short(n))) || nets[0];
      const netSel = h("select.esel.wide", nets.map((n) => h("option", { value: n, selected: n === gnd || undefined }, short(n))));
      const layerSel = h("select.esel.wide", cu.map((l) => h("option", { value: l, selected: l === this.curLayer() || undefined }, l)));
      const pri = h("input.einp", { type: "number", min: 0, max: 99, value: "0", style: { width: "56px" } });
      body = [h("div.ep-h", icon("paint-bucket", 14), "New pour"),
        h("label.ep-r", h("span", "Net"), netSel), h("label.ep-r", h("span", "Layer"), layerSel),
        h("label.ep-r", h("span", "Priority"), pri, h("span.ep-k", "higher fills first")),
        h("div.ep-b", h("button.btn.sm", { onclick: cancel }, "Cancel"), h("button.btn.sm.primary", { onclick: () => {
          const net = netSel.value, cls = netClass(d.rules || {}, net);
          done([{ op: "zone", net, layers: [layerSel.value], polygon: s.pts.map(rnd), name: short(net), priority: Math.max(0, parseInt(pri.value, 10) || 0),
            clearance: round(Math.max(cls.cl, 0.2)), min_width: 0.25, add: true }], `Pour ${short(net)} on ${layerSel.value}`);
        } }, "Add the pour"))];
    } else {
      const boxes = cu.map((l) => h("label.ep-c", h("input", { type: "checkbox", value: l, checked: l === this.curLayer() || undefined }), l));
      const what = [["no_tracks", "Tracks", true], ["no_vias", "Vias", true], ["no_pour", "Pours", true], ["no_footprints", "Parts", false]]
        .map(([k, t, on]) => h("label.ep-c", h("input", { type: "checkbox", value: k, checked: on || undefined }), t));
      body = [h("div.ep-h", icon("ban", 14), "New keep-out"),
        h("div.ep-r", h("span", "Layers"), h("div.ep-cs", boxes)), h("div.ep-r", h("span", "Keeps out"), h("div.ep-cs", what)),
        h("div.ep-b", h("button.btn.sm", { onclick: cancel }, "Cancel"), h("button.btn.sm.primary", { onclick: () => {
          const layers = boxes.map((b) => b.firstChild).filter((i) => i.checked).map((i) => i.value);
          if (!layers.length) { this.flash("Pick at least one layer"); return; }
          const flags = Object.fromEntries(what.map((b) => [b.firstChild.value, b.firstChild.checked]));
          done([{ op: "rule_area", name: "Keep-out", layers, polygon: s.pts.map(rnd), ...flags, add: true }], `Keep-out on ${layers.join(", ")}`);
        } }, "Add the keep-out"))];
    }
    this.pop = h("div.epop", { onmousedown: (e) => e.stopPropagation() }, ...body);
    this.pop.style.left = Math.max(10, Math.min(sx - 140, this.v.w - 300)) + "px";
    this.pop.style.top = Math.max(60, Math.min(sy + 12, this.v.h - 220)) + "px";
    this.v.viewer.appendChild(this.pop);
  }

  closePop() { if (this.pop) { this.pop.remove(); this.pop = null; } }

  // ------------------------------------------------------------------ routing
  obstacles(layer, net) {
    const k = layer + "|" + net + "|" + (this.v.data.version || 0);
    if (!this.obsCache || this.obsCache.k !== k) this.obsCache = { k, o: new Obstacles(this.v, layer, net) };
    return this.obsCache.o;
  }

  routeWidth() {
    const r = this.route;
    if (!r) return;
    r.w = this.width || netClass(this.v.data.rules || {}, r.net).w;
    this.routeUpdate();
    this.showHint();
  }

  // where a route can start: a pad (its centre), a via, or a track (its end, or a point along it)
  routeStartAt(x, y) {
    const c = this.pickCopper(x, y);
    const d = this.v.data;
    if (c && c.kind === "via") { const v = d.vias[c.i]; return { net: v[4], at: [v[0], v[1]], layer: this.curLayer(), ends: [] }; }
    if (c && c.kind === "track") {
      const t = d.tracks[c.i], tol = 2 * HIT / this.v.scale;
      let at;
      if (Math.hypot(x - t[0], y - t[1]) < tol) at = [t[0], t[1]];
      else if (Math.hypot(x - t[2], y - t[3]) < tol) at = [t[2], t[3]];
      else {                                           // a branch from the middle of the segment
        const dx = t[2] - t[0], dy = t[3] - t[1], L2 = dx * dx + dy * dy, u = Math.max(0, Math.min(1, ((x - t[0]) * dx + (y - t[1]) * dy) / L2));
        at = [t[0] + dx * u, t[1] + dy * u];
      }
      return { net: t[6], at, layer: t[5], ends: [] };
    }
    const pd = this.padAt(x, y);
    if (pd) {
      if (!pd.p.net || pd.p.net.startsWith("unconnected-")) return { why: `${pd.f.ref} pad ${pd.p.n} is not connected to anything in the schematic` };
      const thru = pd.p.l.includes("*.Cu") || (pd.p.l.includes("F.Cu") && pd.p.l.includes("B.Cu"));
      const layer = thru ? this.curLayer() : pd.p.l.find((l) => l.endsWith(".Cu")) || this.curLayer();
      return { net: pd.p.net, at: pd.c, layer, ends: pd.polys, pad: pd };
    }
    return null;
  }

  routeClick(x, y, e) {
    if (!this.route) {
      const s = this.routeStartAt(x, y);
      if (!s) { this.flash("Start on a pad, a track or a via"); return; }
      if (s.why) { this.flash(s.why); return; }
      const cls = netClass(this.v.data.rules || {}, s.net);
      this.layer = s.layer;
      this.route = { net: s.net, layer: s.layer, at: s.at, startEnds: s.ends, steps: [], segs: [], vias: [], w: this.width || cls.w, cl: cls.cl,
                     vd: cls.vd, vdrill: cls.vdrill, diag: true, preview: null, blocked: false };
      this.renderBar();
      this.showHint();
      this.routeHover(x, y, e);
      return;
    }
    const r = this.route;
    this.routeHover(x, y, e, true);
    if (!r.preview || r.preview.length < 2) return;
    if (r.blocked) { this.flash(r.why ? `No clear way: ${r.why}` : "No clear way here: move the cursor, add a via (V) or change layer"); return; }
    this.fixPreview();
    if (r.target) { this.routeFinish(); return; }
    this.v.dirty();
  }

  fixPreview() {
    const r = this.route, segs = [];
    for (let i = 0; i + 1 < r.preview.length; i++) segs.push({ a: r.preview[i], b: r.preview[i + 1], layer: r.layer });
    r.steps.push({ kind: "segs", n: segs.length, at: r.at, startEnds: r.startEnds });
    r.segs.push(...segs);
    r.at = r.preview[r.preview.length - 1];
    r.startEnds = r.targetEnds || [];
    r.preview = null;
  }

  // the cursor while routing: snapped to the net's own copper under it (the end), else to the grid; the way there
  routeHover(x, y, e, now) {
    const r = this.route;
    r.want = [x, y, e && e.altKey];
    if (now) { this.routeUpdate(); return; }
    if (this.routeRaf) return;
    this.routeRaf = requestAnimationFrame(() => { this.routeRaf = null; if (this.route) this.routeUpdate(); });
  }

  routeUpdate() {
    const r = this.route, d = this.v.data;
    if (!r || !r.want) { this.v.dirty(); return; }
    const [x, y, free] = r.want;
    let to = null, target = null, targetEnds = [], foreign = null;
    const pd = this.padAt(x, y);
    if (pd && pd.p.net === r.net) {
      const onLayer = pd.p.l.includes("*.Cu") || pd.p.l.includes(r.layer) || (pd.p.l.includes("F&B.Cu") && (r.layer === "F.Cu" || r.layer === "B.Cu"));
      if (onLayer) { to = pd.c; target = "pad"; targetEnds = pd.polys; }
    } else if (pd && pd.p.net) foreign = `${pd.f.ref} pad ${pd.p.n} is on ${short(pd.p.net)}, not ${short(r.net)}`;
    if (!to) {
      const c = this.pickCopper(x, y);
      if (c && c.kind === "via" && d.vias[c.i][4] === r.net) { to = [d.vias[c.i][0], d.vias[c.i][1]]; target = "via"; }
      else if (c && c.kind === "track" && d.tracks[c.i][6] === r.net && d.tracks[c.i][5] === r.layer) {
        const t = d.tracks[c.i], tol = 2 * HIT / this.v.scale;
        if (Math.hypot(x - t[0], y - t[1]) < tol) to = [t[0], t[1]];
        else if (Math.hypot(x - t[2], y - t[3]) < tol) to = [t[2], t[3]];
        else { const dx = t[2] - t[0], dy = t[3] - t[1], L2 = dx * dx + dy * dy, u = Math.max(0, Math.min(1, ((x - t[0]) * dx + (y - t[1]) * dy) / L2)); to = [t[0] + dx * u, t[1] + dy * u]; }
        target = "track";
      }
    }
    if (!to) to = free ? [x, y] : this.snap(x, y);
    if (near(to, r.at, 1e-6)) { r.preview = null; r.blocked = false; r.target = null; this.v.dirty(); return; }
    const obs = this.obstacles(r.layer, r.net);
    let path = posture(r.at, to, r.diag);
    let hit = obs.hitPath(path, r.w, r.cl);
    if (hit) {
      const alt = posture(r.at, to, !r.diag);
      if (!obs.hitPath(alt, r.w, r.cl)) { path = alt; hit = null; }
    }
    if (hit) {
      const p2 = walk(obs, r.at, to, r.w, r.cl, { ends: [...(r.startEnds || []), ...targetEnds] });
      if (p2) { path = p2; hit = null; }
    }
    r.preview = path; r.blocked = !!hit; r.target = hit ? null : target; r.targetEnds = targetEnds;
    r.why = hit ? (foreign || `too close to ${describeHit(hit)}`) : foreign;
    this.v.dirty();
  }

  routeVia(toLayer) {
    const r = this.route;
    if (!r) return;
    if (r.preview && r.preview.length >= 2) {
      if (r.blocked) { this.flash("No clear way to the via's spot"); return; }
      this.fixPreview();
    }
    const cu = this.copperLayers();
    const next = toLayer || (r.layer === cu[0] ? cu[cu.length - 1] : cu[0]);
    if (next === r.layer) return;
    const at = r.at;
    for (const l of cu) {
      const hh = new Obstacles(this.v, l, r.net).hitCirc(at, r.vd / 2, r.cl);
      if (hh) { this.flash(`No room for a via here: too close to ${describeHit(hh)}`); return; }
    }
    if (r.vias.some((v) => near([v.x, v.y], at, 1e-3))) return;
    r.steps.push({ kind: "via", layer: r.layer, startEnds: r.startEnds });
    r.vias.push({ x: at[0], y: at[1] });
    r.layer = next; this.layer = next;
    r.startEnds = [];
    this.renderBar(); this.showHint();
    this.routeUpdate();
  }

  routeBack() {
    const r = this.route;
    const s = r.steps.pop();
    if (!s) { this.route = null; this.showHint(); this.v.dirty(); return; }
    if (s.kind === "via") { r.vias.pop(); r.layer = s.layer; this.layer = s.layer; r.startEnds = s.startEnds; this.renderBar(); }
    else { r.segs.splice(r.segs.length - s.n, s.n); r.at = s.at; r.startEnds = s.startEnds; }
    this.showHint();
    this.routeUpdate();
  }

  async routeFinish() {
    const r = this.route;
    if (!r) return;
    if (!r.segs.length) { this.route = null; this.showHint(); this.v.dirty(); return; }
    while (r.vias.length && r.steps.length && r.steps[r.steps.length - 1].kind === "via") { r.steps.pop(); r.vias.pop(); }   // no via at the very end
    const ops = [{ op: "tracks", items: r.segs.map((s) => ({ net: r.net, layer: s.layer, a: rnd(s.a), b: rnd(s.b), w: r.w })) }];
    if (r.vias.length) ops.push({ op: "vias", items: r.vias.map((v) => ({ net: r.net, x: round(v.x), y: round(v.y), d: r.vd, drill: r.vdrill })) });
    this.route = null;
    this.pending = { tracks: r.segs.map((s) => ({ ...s, w: r.w })), vias: r.vias.map((v) => ({ ...v, d: r.vd })) };
    this.showHint();
    this.v.dirty();
    const res = await this.commit(ops, `Route ${short(r.net)}`, { refill: true });
    if (!res) this.pending = null;
    this.v.dirty();
  }

  // ------------------------------------------------------------------ deleting, the selection bar
  async deleteSel() {
    if (this.v.sel.size && !this.items.size) { this.flash("Parts come from the schematic: remove them there, or ask Claude"); return; }
    const uuids = [...this.items].map((k) => k.slice(2));
    if (!uuids.length) return;
    const nT = [...this.items].filter((k) => k.startsWith("t:")).length, nV = [...this.items].filter((k) => k.startsWith("v:")).length;
    const zs = [...this.items].filter((k) => k.startsWith("z:")).map((k) => this.zoneById(k.slice(2))).filter(Boolean);
    const label = zs.length === 1 && !nT && !nV ? `Delete the ${zs[0].rule ? "keep-out" : short(zs[0].net) + " pour"}`
      : `Delete ${[nT ? `${nT} track${nT === 1 ? "" : "s"}` : "", nV ? `${nV} via${nV === 1 ? "" : "s"}` : "", zs.length ? `${zs.length} pour${zs.length === 1 ? "" : "s"}` : ""].filter(Boolean).join(", ")}`;
    this.pending = { hide: new Set(this.items) };
    this.items.clear();
    this.v.renderSelBar();
    this.v.dirty();
    const r = await this.commit([{ op: "delete", uuids, kinds: ["track", "via", "zone"] }], label, { refill: true });
    if (!r) this.pending = null;
    this.v.dirty();
  }

  // the selection bar while editing: what is picked, and what can be done to it
  selBar(box) {
    const d = this.v.data, keys = [...this.items];
    const tr = keys.filter((k) => k.startsWith("t:")).map((k) => d.tid.indexOf(k.slice(2))).filter((i) => i >= 0);
    const vi = keys.filter((k) => k.startsWith("v:")).map((k) => d.vid.indexOf(k.slice(2))).filter((i) => i >= 0);
    const zs = keys.filter((k) => k.startsWith("z:")).map((k) => this.zoneById(k.slice(2))).filter(Boolean);
    const del = h("button.tbtn", { "data-tip": "Delete (⌫)", onclick: () => this.deleteSel(), disabled: this.busy || undefined }, icon("trash-2", 14));
    const close = h("button.tbtn", { "data-tip": "Clear the selection", onclick: () => { this.items.clear(); this.v.renderSelBar(); this.v.dirty(); } }, icon("x", 14));
    if (tr.length === 1 && !vi.length && !zs.length) {
      const t = d.tracks[tr[0]];
      const ws = h("select.esel", { disabled: this.busy || undefined, onchange: (e) => this.commit([{ op: "track_set", uuid: d.tid[tr[0]], w: parseFloat(e.target.value) }], `Width of a ${short(t[6])} track`, { refill: true }) },
        [...new Set([t[4], ...WIDTHS])].sort((a, b) => a - b).map((w) => h("option", { value: String(w), selected: Math.abs(w - t[4]) < 1e-6 || undefined }, `${w} mm`)));
      box.append(icon("route", 14), h("span.sl", `${short(t[6])} track · ${t[5]}`), ws, del, close);
    } else if (vi.length === 1 && !tr.length && !zs.length) {
      const v = d.vias[vi[0]];
      box.append(icon("circle-dot", 14), h("span.sl", `${short(v[4])} via · ${v[2]}/${v[3]} mm`), del, close);
    } else if (zs.length === 1 && !tr.length && !vi.length) {
      const z = zs[0];
      if (z.rule) {
        const ko = Object.entries(z.ko || {}).filter(([, v]) => v).map(([k]) => ({ tracks: "tracks", vias: "vias", copperpour: "pours", footprints: "parts", pads: "pads" })[k] || k);
        box.append(icon("ban", 14), h("span.sl", `Keep-out · ${(z.l || []).join(", ")}${ko.length ? " · no " + ko.join(", ") : ""}`), del, close);
      } else {
        const pri = (dv) => this.commit([{ op: "zone_set", uuid: z.id, priority: Math.max(0, (z.pri || 0) + dv) }], `Priority of the ${short(z.net)} pour`, { fill: true });
        const nets = (d.nets || []).filter((n) => n && !n.startsWith("unconnected-"));
        const ns = h("select.esel", { disabled: this.busy || undefined, onchange: (e) => this.commit([{ op: "zone_set", uuid: z.id, net: e.target.value }], `Pour on ${short(e.target.value)}`, { fill: true }) },
          nets.map((n) => h("option", { value: n, selected: n === z.net || undefined }, short(n))));
        box.append(icon("paint-bucket", 14), h("span.sl", `Pour · ${(z.l || []).join(", ")}`), ns,
          h("span.epri", h("button.tbtn", { "data-tip": "Lower priority", onclick: () => pri(-1), disabled: this.busy || !z.pri || undefined }, "−"), h("span", `priority ${z.pri || 0}`),
            h("button.tbtn", { "data-tip": "Higher priority (fills first)", onclick: () => pri(1), disabled: this.busy || undefined }, "+")),
          h("button.tbtn", { "data-tip": "Fill the pours again", onclick: () => this.commit([{ op: "fill" }], "Refill the pours"), disabled: this.busy || undefined }, icon("refresh-cw", 14)), del, close);
      }
    } else {
      const parts = [tr.length ? `${tr.length} track${tr.length === 1 ? "" : "s"}` : "", vi.length ? `${vi.length} via${vi.length === 1 ? "" : "s"}` : "", zs.length ? `${zs.length} pour${zs.length === 1 ? "" : "s"}` : ""].filter(Boolean);
      box.append(icon("layers", 14), h("span.sl", parts.join(", ")), del, close);
    }
  }

  // buttons for picked parts while editing
  partActions(refs) {
    if (!this.on || !refs.length) return [];
    const locked = refs.every((r) => this.v.byRef[r] && this.v.byRef[r].locked);
    const dis = this.busy || undefined;
    return [h("div.tsep"),
      h("button.tbtn", { "data-tip": "Turn 90°", "data-kbd": "r", onclick: () => this.rotateSel(), disabled: dis }, icon("rotate-ccw", 14)),
      h("button.tbtn", { "data-tip": "Flip to the other side", onclick: () => this.flipSel(), disabled: dis }, icon("flip-horizontal-2", 14)),
      h("button.tbtn" + (locked ? ".on" : ""), { "data-tip": locked ? "Unlock" : "Lock where it is", "data-kbd": "l", onclick: () => this.lockSel(), disabled: dis }, icon(locked ? "lock" : "lock-open", 14))];
  }

  // ------------------------------------------------------------------ saving
  async commit(ops, label, opts = {}) {
    if (this.busy) { this.flash("Claude is working on the design: edit when it is done"); return null; }
    this.saving++; this.status = "Saving"; this.renderBar();
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/board/edit`, { body: { ops, label, fill: !!opts.fill } });
      this.hist = r.history || this.hist;
      this.status = r.via === "live" || r.via === "file+reload" ? "Saved in KiCad" : "Saved";
      if (opts.refill) this.refillSoon();
      return r;
    } catch (e) {
      this.status = "";
      toast(e.status === 409 ? e.message : `The edit did not apply: ${e.message}`, "error");
      return null;
    } finally {
      this.saving--;
      this.renderBar();
      clearTimeout(this.statusT);
      this.statusT = setTimeout(() => { if (!this.saving) { this.status = ""; this.renderBar(); } }, 2200);
    }
  }

  // pours fill again round the new copper a moment after the last change, as part of that change (one undo step)
  refillSoon() {
    if (!(this.v.data.zones || []).some((z) => !z.rule)) return;
    clearTimeout(this.refillT);
    this.refillT = setTimeout(async () => {
      if (this.busy || this.drag || this.route) { this.refillSoon(); return; }
      this.saving++; this.status = "Filling the pours"; this.renderBar();
      try {
        const r = await api(`/api/projects/${enc(this.pid)}/board/edit`, { body: { ops: [{ op: "fill" }], label: "Refill the pours", merge: true } });
        this.hist = r.history || this.hist;
        this.status = "Saved";
      } catch { this.status = ""; }
      finally { this.saving--; this.renderBar(); }
    }, 700);
  }

  async undo() { await this.step(true); }
  async redo() { await this.step(false); }
  async step(back) {
    if (this.busy) { this.flash("Claude is working on the design: undo when it is done"); return; }
    if (this.route) { this.routeBack(); return; }
    if (this.shape) { this.shape.pts.pop(); if (!this.shape.pts.length) this.shape = null; this.v.dirty(); return; }
    if (!(back ? this.hist.undo : this.hist.redo)) { this.flash(back ? "Nothing to undo" : "Nothing to redo"); return; }
    clearTimeout(this.refillT);
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/board/${back ? "undo" : "redo"}`, { body: {} });
      this.hist = r.history || this.hist;
      this.items.clear();
      this.flash(`${back ? "Undid" : "Redid"}: ${r.label}`);
    } catch (e) { toast(e.message, "error"); }
    this.renderBar();
    this.v.renderSelBar();
  }

  flash(text) {
    const f = this.v.flash;
    f.textContent = text;
    f.style.display = "block";
    clearTimeout(this.v.flashT);
    this.v.flashT = setTimeout(() => (f.style.display = "none"), 3200);
  }

  // the board came in again: the picked copper by its id, the history
  loaded() {
    const d = this.v.data;
    this.pending = null;
    this.edgeObs = null;
    if (!d) return;
    const ids = new Set([...d.tid.map((x) => "t:" + x), ...d.vid.map((x) => "v:" + x), ...d.zones.map((z) => "z:" + z.id)]);
    for (const k of [...this.items]) if (!ids.has(k)) this.items.delete(k);
    if (this.on) { this.loadHistory(); this.v.renderSelBar(); }
  }

  hidden(key) {
    if (this.pending && this.pending.hide && this.pending.hide.has(key)) return true;
    return !!(this.drag && this.drag.hide && this.drag.hide.has(key));
  }

  // ------------------------------------------------------------------ drawing (board mm; px is one screen pixel)
  draw(c, px) {
    if (!this.on && !this.pending) return;
    const d = this.v.data, sel = this.v.colors.sel;
    // copper hidden by an edit in progress (its new place is drawn below): painted over with the board
    const hide = new Set([...(this.pending && this.pending.hide ? this.pending.hide : []), ...(this.drag && this.drag.hide ? this.drag.hide : [])]);
    if (hide.size) {
      c.strokeStyle = this.v.panel === "copper" ? "#141a17" : "#16241b"; c.fillStyle = c.strokeStyle; c.lineCap = "round";
      d.tracks.forEach((t, i) => { if (hide.has("t:" + d.tid[i])) { c.lineWidth = t[4] + 2 * px; c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); } });
      d.vias.forEach((v, i) => { if (hide.has("v:" + d.vid[i])) { c.beginPath(); c.arc(v[0], v[1], v[2] / 2 + px, 0, Math.PI * 2); c.fill(); } });
    }
    const seg = (a, b, w, col) => { c.strokeStyle = col; c.lineWidth = w; c.lineCap = "round"; c.beginPath(); c.moveTo(a[0], a[1]); c.lineTo(b[0], b[1]); c.stroke(); };
    const via = (x, y, dd, drill, col) => { c.fillStyle = col; c.beginPath(); c.arc(x, y, dd / 2, 0, Math.PI * 2); c.fill(); c.fillStyle = "#0b0d11"; c.beginPath(); c.arc(x, y, (drill || dd / 2) / 2, 0, Math.PI * 2); c.fill(); };
    const lc = (l, a = 0.95) => this.v.layerColor(l, a);
    const lit = (l, a = 1) => { const q = this.v.colors[l] || [160, 160, 160]; return rgbaOf(q.map((v) => Math.round(v + (255 - v) * 0.4)), a); };   // copper not placed yet
    // the edit on its way to the board
    if (this.pending) {
      for (const t of this.pending.tracks || []) seg(t.a, t.b, t.w, lc(t.layer));
      for (const v of this.pending.vias || []) via(v.x, v.y, v.d, v.d / 2, "rgba(196,200,210,.95)");
      if (this.pending.zone) { c.strokeStyle = rgbaOf(sel, 0.9); c.lineWidth = 1.5 * px; c.beginPath(); polyPath(c, this.pending.zone.pts); c.stroke(); }
    }
    if (!this.on) return;
    // the picked copper
    for (const k of this.items) {
      if (k.startsWith("t:")) { const i = d.tid.indexOf(k.slice(2)); if (i >= 0) { const t = d.tracks[i]; seg([t[0], t[1]], [t[2], t[3]], Math.max(t[4], 2 * px) + 2 * px, rgbaOf(sel, 0.95)); seg([t[0], t[1]], [t[2], t[3]], t[4], lc(t[5])); } }
      else if (k.startsWith("v:")) { const i = d.vid.indexOf(k.slice(2)); if (i >= 0) { const v = d.vias[i]; c.strokeStyle = rgbaOf(sel, 1); c.lineWidth = 2 * px; c.beginPath(); c.arc(v[0], v[1], v[2] / 2 + 1.5 * px, 0, Math.PI * 2); c.stroke(); } }
      else if (k.startsWith("z:")) { const z = this.zoneById(k.slice(2)); if (z) { c.strokeStyle = rgbaOf(z.rule ? [255, 110, 110] : sel, 0.95); c.lineWidth = 2 * px; c.setLineDash([]); for (const o of z.o) { c.beginPath(); polyPath(c, o); c.stroke(); } } }
    }
    // a drag in progress
    const g = this.drag;
    if (g && (g.kind === "seg" || g.kind === "via") && g.preview) {
      for (const p of g.preview) seg(p.a, p.b, p.w, g.hit ? "rgba(240,101,96,.95)" : lit(p.layer));
      if (g.viaPreview) via(g.viaPreview.x, g.viaPreview.y, g.viaPreview.d, g.viaPreview.d / 2, g.hit ? "rgba(240,101,96,.95)" : "rgba(196,200,210,.95)");
    }
    if (g && (g.kind === "vertex" || g.kind === "zone") && g.pts) {
      c.strokeStyle = rgbaOf(sel, 0.95); c.lineWidth = 1.5 * px; c.setLineDash([5 * px, 3 * px]);
      c.beginPath(); polyPath(c, g.pts); c.stroke(); c.setLineDash([]);
    }
    if (g && g.kind === "fp" && (g.moved || g.rot)) {
      for (const r of g.refs) {
        const f = this.v.byRef[r];
        if (!f) continue;
        const p = this.v.pose(f);
        const loops = f.cy && f.cy.length ? f.cy : [[[f.bbox[0], f.bbox[1]], [f.bbox[2], f.bbox[1]], [f.bbox[2], f.bbox[3]], [f.bbox[0], f.bbox[3]]]];
        c.strokeStyle = g.bad && g.bad.some((b) => b.startsWith(r + " ")) ? "rgba(240,101,96,.95)" : rgbaOf(sel, 0.9);
        c.lineWidth = 1.5 * px;
        for (const l of loops) { c.beginPath(); polyPath(c, p ? l.map(([x, y]) => fwd(p, f, x, y)) : l); c.stroke(); }
      }
    }
    // the route being drawn
    const r = this.route;
    if (r) {
      for (const s of r.segs) seg(s.a, s.b, r.w, lit(s.layer));
      for (const v of r.vias) via(v.x, v.y, r.vd, r.vdrill, "rgba(225,228,236,1)");
      if (r.preview && r.preview.length > 1) {
        const col = r.blocked ? "rgba(240,101,96,.85)" : lit(r.layer, 0.8);
        for (let i = 0; i + 1 < r.preview.length; i++) seg(r.preview[i], r.preview[i + 1], r.w, col);
        // the clearance it keeps
        c.strokeStyle = r.blocked ? "rgba(240,101,96,.5)" : "rgba(255,255,255,.22)"; c.lineWidth = px; c.setLineDash([3 * px, 3 * px]);
        for (let i = 0; i + 1 < r.preview.length; i++) capsule(c, r.preview[i], r.preview[i + 1], r.w / 2 + r.cl);
        c.setLineDash([]);
      }
      c.fillStyle = "#fff"; c.beginPath(); c.arc(r.at[0], r.at[1], Math.max(r.w / 2, 3 * px), 0, Math.PI * 2); c.fill();
    }
    // a pour or keep-out being drawn
    const s = this.shape;
    if (s) {
      const pts = s.closed ? s.pts : [...s.pts, s.cur || s.pts[s.pts.length - 1]];
      const keep = s.kind === "keepout";
      c.fillStyle = keep ? "rgba(255,110,110,.12)" : lc(this.curLayer(), 0.18);
      c.strokeStyle = keep ? "rgba(255,110,110,.9)" : lc(this.curLayer(), 0.9);
      c.lineWidth = 1.5 * px; c.setLineDash(keep ? [5 * px, 3 * px] : []);
      c.beginPath(); polyPath(c, pts, !s.closed && pts.length < 3); if (pts.length >= 3) c.fill(); c.stroke(); c.setLineDash([]);
    }
  }

  // handles and markers in screen pixels
  drawScreen(c) {
    if (!this.on) return;
    const z = this.selZone();
    if (z && !(this.drag && this.drag.kind === "zone")) {
      const o = this.drag && this.drag.kind === "vertex" ? this.drag.pts : z.o[0];
      for (let k = 0; k < o.length; k++) {
        const [sx, sy] = this.v.toScreen(o[k][0], o[k][1]);
        c.fillStyle = "#fff"; c.strokeStyle = "rgba(0,0,0,.7)"; c.lineWidth = 1;
        c.fillRect(sx - 4, sy - 4, 8, 8); c.strokeRect(sx - 4, sy - 4, 8, 8);
        const b = o[(k + 1) % o.length], [mx, my] = this.v.toScreen((o[k][0] + b[0]) / 2, (o[k][1] + b[1]) / 2);
        c.fillStyle = "rgba(255,255,255,.55)"; c.beginPath(); c.arc(mx, my, 3.5, 0, Math.PI * 2); c.fill();
      }
    }
    const s = this.shape;
    if (s) for (const [i, p] of s.pts.entries()) {
      const [sx, sy] = this.v.toScreen(p[0], p[1]);
      c.fillStyle = i === 0 ? "#f4a574" : "#fff"; c.beginPath(); c.arc(sx, sy, i === 0 ? 5 : 3.5, 0, Math.PI * 2); c.fill();
    }
    if (this.route && this.route.why && this.cursor) {
      const [sx, sy] = this.v.toScreen(...this.cursor);
      c.font = "12px ui-sans-serif, -apple-system, sans-serif"; c.textAlign = "left"; c.textBaseline = "middle";
      const t = this.route.why, w = Math.min(380, c.measureText(t).width + 16);
      c.fillStyle = "rgba(20,21,25,.94)"; c.fillRect(sx + 16, sy + 14, w, 22);
      c.fillStyle = this.route.blocked ? "#ff9a95" : "#ffe19a"; c.fillText(t, sx + 24, sy + 25);
    }
  }
}

// ------------------------------------------------------------------ helpers
function round(v) { return Math.round(v * 10000) / 10000; }
function rnd(p) { return [round(p[0]), round(p[1])]; }
function rgbaOf(c, a) { return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }
function area(pts) { let a = 0; for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) a += (pts[j][0] + pts[i][0]) * (pts[j][1] - pts[i][1]); return a / 2; }
function polyPath(c, pts, open) { if (!pts.length) return; c.moveTo(pts[0][0], pts[0][1]); for (let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]); if (!open) c.closePath(); }
function inv(p, f, x, y) {
  const a = (p.rot - f.a) * Math.PI / 180, cs = Math.cos(a), sn = Math.sin(a);
  const dx = x - p.x, dy = y - p.y;
  return [f.x + dx * cs - dy * sn, f.y + dx * sn + dy * cs];
}
// the outline a track keeps clear: a capsule round a segment
function capsule(c, a, b, r) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy);
  if (L < 1e-9) { c.beginPath(); c.arc(a[0], a[1], r, 0, Math.PI * 2); c.stroke(); return; }
  const ang = Math.atan2(dy, dx);
  c.beginPath();
  c.arc(a[0], a[1], r, ang + Math.PI / 2, ang + 3 * Math.PI / 2);
  c.arc(b[0], b[1], r, ang - Math.PI / 2, ang + Math.PI / 2);
  c.closePath(); c.stroke();
}
export function describeHit(s) {
  if (!s) return "";
  if (s.what === "edge") return "the board's edge";
  if (s.what === "keepout") return `a keep-out${s.name ? ` (${s.name})` : ""}`;
  if (s.what === "hole") return `a hole${s.ref ? ` in ${s.ref}` : ""}`;
  if (s.what === "pad") return `${s.ref} pad ${s.pad}${s.net ? ` (${short(s.net)})` : ""}`;
  if (s.what === "via") return `a ${short(s.net)} via`;
  return `a ${short(s.net)} track`;
}
