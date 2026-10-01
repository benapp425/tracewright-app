// Review flags: comments you leave on the board, the schematic or the 3D view ("move C3 closer to U1", "is this
// wide enough?"), or findings you pick from the checks -- each a request (change it) or a question (ask first), some
// with a drawing: a pen line, an arrow, a sketch of where a track should run. They collect in the Review panel and
// go to Claude together, each with a snapshot of the spot. Each flag is a thread: Claude replies to every one, done
// (green: it made the change), declined (red: it disagrees and changed nothing, with why) or answered (blue); you
// can reply, or say do it anyway, and the reply goes with the next send.
import { h, clear, api, toast, btn, popover, confirmDialog, fmtTime, Emitter, kbd, lightbox } from "./util.js";
import { icon } from "./icons.js";

const enc = encodeURIComponent;
const SVGNS = "http://www.w3.org/2000/svg";
export const VIEW_ICON = { board: "circuit-board", schematic: "waypoints", "3d": "box", check: "list-checks" };
const VIEW_NAME = { board: "Board", schematic: "Schematic", "3d": "3D", check: "Check" };
const STATUS_NAME = { open: "Not sent", sent: "With Claude", done: "Done", declined: "Claude disagrees", answered: "Answered" };
const OUTCOME = new Set(["done", "declined", "answered"]);
const PIN_ICON = { done: "check", declined: "x", answered: "message-circle" };
const MARK_TOOLS = [["pin", "flag", "Comment: click a spot or drag an area"], ["pen", "pen-line", "Draw freehand"],
  ["arrow", "move-up-right", "Draw an arrow"], ["route", "route", "Sketch where a track should run: click the corners, double-click to finish"]];

// ------------------------------------------------------------------ the project's flags
export class Review extends Emitter {
  constructor(ws) {
    super();
    this.ws = ws; this.pid = ws.pid; this.flags = []; this.sel = new Set(); this.loaded = false;
    ws.ev.on("review.changed", (e) => this.set(e.flags, true));
    this.load();
  }

  async load() {
    try { const r = await api(`/api/projects/${enc(this.pid)}/review`); this.set(r.flags); } catch (e) { /* offline */ }
  }

  set(flags, live) {
    const before = new Map(this.flags.map((f) => [f.id, f]));
    this.flags = flags || [];
    this.loaded = true;
    for (const id of [...this.sel]) if (!this.flags.find((f) => f.id === id && f.status === "open")) this.sel.delete(id);
    if (live) for (const f of this.flags) {                         // Claude's replies, as they come
      const b = before.get(f.id);
      if (b && b.status !== f.status && OUTCOME.has(f.status) && f.resolved_by === "claude") {
        const lead = f.status === "done" ? "Done" : f.status === "declined" ? "Claude disagrees on" : "Answered";
        toast(`${lead} #${f.n}: ${f.resolution || f.text}`.slice(0, 170), f.status === "done" ? "ok" : f.status === "declined" ? "warn" : "info", 7000,
          { label: "Show", run: () => this.ws.showFlag(f) });
      }
    }
    this.emit("changed", this.flags);
  }

  get(id) { return this.flags.find((f) => f.id === id); }
  active() { return this.flags.filter((f) => f.status === "open" || f.status === "sent"); }
  open() { return this.flags.filter((f) => f.status === "open"); }
  replied() { return this.flags.filter((f) => OUTCOME.has(f.status)); }

  // the flags a view shows: its own, plus those with a place in it (a 3D flag on the board, a finding's spot)
  forView(view, sheet) {
    return this.flags.filter((f) => {
      const w = f.where || {};
      if (view === "schematic") return (f.view === "schematic" || f.view === "check") && w.sheet && w.sheet === sheet && w.x !== undefined;
      if (w.x === undefined || w.sheet) return false;
      return f.view === view || f.view === "check" || (view === "board" && f.view === "3d") || (view === "3d" && f.view === "board");
    });
  }

  async add(view, text, where, snapshot, ask, marks) {
    const f = await api(`/api/projects/${enc(this.pid)}/review`, { body: { view, text, where, snapshot, ask: ask || "request", marks: marks || [] } });
    if (!this.get(f.id)) { this.flags = [...this.flags, f]; this.emit("changed", this.flags); }
    return f;
  }

  async update(id, changes) { return api(`/api/projects/${enc(this.pid)}/review/${enc(id)}`, { method: "PATCH", body: changes }); }
  async reply(id, text, anyway) { return api(`/api/projects/${enc(this.pid)}/review/${enc(id)}/reply`, { body: { text, anyway: !!anyway } }); }

  async remove(id) {
    await api(`/api/projects/${enc(this.pid)}/review/${enc(id)}`, { method: "DELETE" });
    this.flags = this.flags.filter((f) => f.id !== id); this.sel.delete(id); this.emit("changed", this.flags);
  }

  async clearResolved() { await api(`/api/projects/${enc(this.pid)}/review/resolved`, { method: "DELETE" }); }

  // a check finding into the review list
  async fromFinding(check, f) {
    const w = f.where || {};
    const where = { check: check.id, x: w.x, y: w.y, sheet: w.sheet, refs: w.ref ? [w.ref] : [], nets: w.net ? [w.net] : [] };
    return this.add("check", `${check.title}: ${f.message}` + (f.hint ? `\n(${f.hint})` : ""), where);
  }

  async send(ids, note) {
    const list = ids && ids.length ? ids : this.open().map((f) => f.id);
    if (!list.length) { toast("Nothing to send. Press C to leave a comment.", "info"); return false; }
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/review/send`, { body: { ids: list, note: note || "", sid: this.ws.chat && this.ws.chat.sid } });
      this.sel.clear();
      toast(`Sent ${r.sent} flag${r.sent === 1 ? "" : "s"} to Claude`, "ok");
      return true;
    } catch (e) { toast(e.message, "error"); return false; }
  }
}

export function snapshotURL(pid, f) { return `/api/projects/${enc(pid)}/review/${enc(f.id)}/snapshot?t=${enc(f.updated || "")}`; }

export function whereLabel(f) {
  const w = f.where || {};
  const bits = [];
  if (w.check) bits.push(w.check);
  if (w.sheet) bits.push(w.sheet === "/" ? "root sheet" : w.sheet.replace(/^\/|\/$/g, ""));
  if (w.refs && w.refs.length) bits.push(w.refs.slice(0, 4).join(", ") + (w.refs.length > 4 ? "…" : ""));
  else if (w.nets && w.nets.length) bits.push(w.nets.map((n) => n.split("/").pop()).slice(0, 3).join(", "));
  else if (w.x !== undefined) bits.push(`${(+w.x).toFixed(1)}, ${(+w.y).toFixed(1)}`);
  return bits.join(" · ");
}

function lastWord(f) {
  const t = f.thread || [];
  return t.length ? t[t.length - 1] : null;
}

// ------------------------------------------------------------------ pins and drawings over a view
// opts: {view, project(x, y) -> [sx, sy] | null, sheet() -> the sheet shown, onPin(flag, el), showDone()}
export class FlagLayer {
  constructor(container, review, opts) {
    this.review = review; this.o = opts;
    this.el = h("div.flaglayer");
    this.svg = document.createElementNS(SVGNS, "svg");
    this.svg.setAttribute("class", "flagmarks");
    this.el.appendChild(this.svg);
    container.appendChild(this.el);
    this.items = [];
    this.draft = null;
    this.sketch = null;                                   // a drawing in progress: {t, p}
    this.off = review.on("changed", () => this.render());
    this.render();
  }

  destroy() { this.off(); this.el.remove(); }

  render() {
    for (const n of [...this.el.childNodes]) if (n !== this.svg) n.remove();
    this.items = [];
    const showDone = this.o.showDone ? this.o.showDone() : true;
    for (const f of this.review.forView(this.o.view, this.o.sheet && this.o.sheet())) {
      if (!showDone && f.status !== "open" && f.status !== "sent") continue;
      const region = f.where.region ? h("div.fregion." + f.status) : null;
      const ask = f.ask === "question" && !OUTCOME.has(f.status);
      const pin = h("div.fpin." + f.status + (ask ? ".ask" : ""), { "data-tip": f.text.length > 90 ? f.text.slice(0, 90) + "…" : f.text,
        onmousedown: (e) => e.stopPropagation(), onclick: (e) => { e.stopPropagation(); this.o.onPin && this.o.onPin(f, pin); } },
        PIN_ICON[f.status] ? icon(PIN_ICON[f.status], 13) : ask ? "?" : String(f.n));
      if (region) this.el.appendChild(region);
      this.el.appendChild(pin);
      this.items.push({ f, pin, region });
    }
    if (this.draft) {
      const d = this.draft;
      d.region = d.where.region ? h("div.fregion") : null;
      d.pin = h("div.fpin.draft", icon("plus", 13));
      if (d.region) this.el.appendChild(d.region);
      this.el.appendChild(d.pin);
      this.items.push({ f: d, pin: d.pin, region: d.region, draft: true });
    }
    this.update();
  }

  setDraft(where, marks) { this.draft = where ? { where, status: "draft", marks: marks || [] } : null; this.render(); return this.draft && this.draft.pin; }
  setSketch(m) { this.sketch = m; this.update(); }

  update() {
    const W = this.el.clientWidth, H = this.el.clientHeight;
    for (const it of this.items) {
      const w = it.f.where;
      const p = this.o.project(w.x, w.y, w);
      const vis = p && p[0] > -30 && p[1] > -30 && p[0] < W + 30 && p[1] < H + 30;
      it.pin.style.display = vis ? "" : "none";
      if (vis) { it.pin.style.left = p[0] + "px"; it.pin.style.top = p[1] + "px"; }
      if (it.region) {
        const a = this.o.project(w.region[0], w.region[1], w), b = this.o.project(w.region[2], w.region[3], w);
        if (!a || !b) { it.region.style.display = "none"; continue; }
        it.region.style.display = "";
        Object.assign(it.region.style, { left: Math.min(a[0], b[0]) + "px", top: Math.min(a[1], b[1]) + "px",
          width: Math.abs(b[0] - a[0]) + "px", height: Math.abs(b[1] - a[1]) + "px" });
      }
    }
    this.drawMarks();
  }

  // the drawings, in screen pixels: each flag's in its status colour, the one being drawn on top
  drawMarks() {
    const svg = this.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const all = this.items.map((it) => [it.f.marks || [], it.draft ? "draft" : it.f.status, it.f.where]);
    if (this.sketch) all.push([[this.sketch], "draft", {}]);
    for (const [marks, st, w] of all) for (const m of marks) {
      const pts = m.p.map(([x, y]) => this.o.project(x, y, w || {})).filter(Boolean);
      if (!pts.length) continue;
      const g = document.createElementNS(SVGNS, "g");
      g.setAttribute("class", `fmark ${m.t} ${st}`);
      const path = document.createElementNS(SVGNS, "polyline");
      path.setAttribute("points", pts.map((q) => q.join(",")).join(" "));
      g.appendChild(path);
      if (m.t === "arrow" && pts.length >= 2) {
        const a = pts[pts.length - 2], b = pts[pts.length - 1], ang = Math.atan2(b[1] - a[1], b[0] - a[0]), L = 12;
        const head = document.createElementNS(SVGNS, "polyline");
        head.setAttribute("points", [[b[0] - L * Math.cos(ang - 0.45), b[1] - L * Math.sin(ang - 0.45)], b,
          [b[0] - L * Math.cos(ang + 0.45), b[1] - L * Math.sin(ang + 0.45)]].map((q) => q.join(",")).join(" "));
        g.appendChild(head);
      }
      if (m.t === "route") for (const q of pts) {
        const c = document.createElementNS(SVGNS, "circle");
        c.setAttribute("cx", q[0]); c.setAttribute("cy", q[1]); c.setAttribute("r", 3);
        g.appendChild(c);
      }
      svg.appendChild(g);
    }
  }

  pinFor(id) { const it = this.items.find((x) => x.f.id === id); return it && it.pin; }
  mark(id) { for (const it of this.items) it.pin.classList.toggle("on", it.f.id === id); }
}

// ------------------------------------------------------------------ the flag tool in a view
// opts: {view, surface: the element clicked, toWorld(px, py) -> [x, y] | null, context(where) -> where,
//        snapshot(where) -> Promise<dataURL | null>, layer: FlagLayer, hud: element for the hint, onChange(active),
//        marks: the drawing tools that make sense there -- true for all, or a list of pin | pen | arrow | route}
export class FlagTool {
  constructor(ws, opts) {
    this.ws = ws; this.o = opts; this.active = false; this.mode = "pin"; this.route = null;
    this.modeBtns = {};
    const tools = MARK_TOOLS.filter(([k]) => opts.marks === true || (opts.marks || []).includes(k));
    const modes = tools.length > 1 ? h("div.fmodes", tools.map(([k, ic, tip]) => (this.modeBtns[k] = h("button.tbtn" + (k === "pin" ? ".on" : ""),
      { onclick: () => this.setMode(k), "data-tip": tip }, icon(ic, 14))))) : null;
    this.label = h("span", "Click or drag to mark an area");
    this.hint = h("div.vhint", modes || icon("flag", 14), this.label, h("span.kbd", "esc"),
      btn("x", null, { onclick: () => this.toggle(false), "data-tip": "Exit the flag tool" }, "sm ghost"));
    this.hint.style.display = "none";
    opts.hud.appendChild(this.hint);
    this.keys = (e) => {
      if (!this.active || !this.route || /INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
      if (e.key === "Enter") { e.preventDefault(); this.finishRoute(); }
      else if (e.key === "Backspace") {
        e.preventDefault();
        this.route.p.pop();
        if (!this.route.p.length) this.route = null;
        this.o.layer.setSketch(this.route);
      }
    };
    document.addEventListener("keydown", this.keys);
  }

  setMode(k) {
    this.mode = k; this.route = null; this.o.layer.setSketch(null);
    for (const [m, b] of Object.entries(this.modeBtns)) b.classList.toggle("on", m === k);
    this.label.textContent = { pin: "Click or drag to mark an area", pen: "Draw what you mean", arrow: "Drag an arrow",
      route: "Click the corners of the track's way · double-click or Enter to finish" }[k];
  }

  toggle(on = !this.active) {
    this.active = on;
    this.hint.style.display = on ? "" : "none";
    this.o.onChange && this.o.onChange(on);
    if (!on) { this.o.layer.setDraft(null); this.o.layer.setSketch(null); this.route = null; if (this.o.marks) this.setMode("pin"); }
  }

  // mousedown on the view's surface: true when the flag tool took it
  down(e) {
    if (!this.active || e.button !== 0) return false;
    e.preventDefault();
    const r = this.o.surface.getBoundingClientRect();
    const p0 = [e.clientX - r.left, e.clientY - r.top];
    const w0 = this.o.toWorld(p0[0], p0[1], e);
    if (!w0) return true;
    if (this.mode === "route") {                        // a click adds a corner; a double-click ends the sketch
      if (e.detail >= 2 && this.route) { this.finishRoute(); return true; }
      this.route = this.route || { t: "route", p: [] };
      const last = this.route.p[this.route.p.length - 1];
      if (!last || Math.hypot(last[0] - w0[0], last[1] - w0[1]) > 1e-6) this.route.p.push([w0[0], w0[1]]);
      this.o.layer.setSketch(this.route);
      return true;
    }
    if (this.mode === "pen" || this.mode === "arrow") {
      const m = { t: this.mode, p: [[w0[0], w0[1]]] };
      const mv = (ev) => {
        const w1 = this.o.toWorld(ev.clientX - r.left, ev.clientY - r.top, ev);
        if (!w1) return;
        if (m.t === "arrow") m.p = [m.p[0], [w1[0], w1[1]]];
        else { const q = m.p[m.p.length - 1]; if (Math.hypot(q[0] - w1[0], q[1] - w1[1]) > 0) m.p.push([w1[0], w1[1]]); }
        this.o.layer.setSketch(m);
      };
      const up = () => {
        removeEventListener("mousemove", mv); removeEventListener("mouseup", up);
        this.o.layer.setSketch(null);
        if (m.p.length < 2) return;
        const end = m.p[m.p.length - 1];
        this.create({ x: end[0], y: end[1], ...(w0[2] || {}) }, [simplify(m)]);
      };
      addEventListener("mousemove", mv); addEventListener("mouseup", up);
      return true;
    }
    let moved = false, box = null;
    const mv = (ev) => {
      const p = [ev.clientX - r.left, ev.clientY - r.top];
      if (!moved && Math.hypot(p[0] - p0[0], p[1] - p0[1]) < 5) return;
      moved = true;
      const w1 = this.o.toWorld(p[0], p[1], ev);
      if (!w1) return;
      box = [w0[0], w0[1], w1[0], w1[1]];
      this.o.layer.setDraft({ x: w0[0], y: w0[1], region: [Math.min(w0[0], w1[0]), Math.min(w0[1], w1[1]), Math.max(w0[0], w1[0]), Math.max(w0[1], w1[1])] });
    };
    const up = () => {
      removeEventListener("mousemove", mv); removeEventListener("mouseup", up);
      let where = { x: w0[0], y: w0[1], ...(w0[2] || {}) };
      if (moved && box) {
        const reg = [Math.min(box[0], box[2]), Math.min(box[1], box[3]), Math.max(box[0], box[2]), Math.max(box[1], box[3])];
        where = { x: reg[0], y: reg[1], region: reg, ...(w0[2] || {}) };
      }
      this.create(where);
    };
    addEventListener("mousemove", mv); addEventListener("mouseup", up);
    return true;
  }

  finishRoute() {
    const m = this.route;
    this.route = null;
    this.o.layer.setSketch(null);
    if (!m || m.p.length < 2) return;
    const end = m.p[m.p.length - 1];
    this.create({ x: end[0], y: end[1] }, [m], "Route this track along the line I drew.");
  }

  // a flag at a place given by code (a selection, the 3D pick), with what was drawn
  async create(where, marks, text) {
    where = this.o.context ? this.o.context(where) : where;
    const pin = this.o.layer.setDraft(where, marks);
    const shot = this.o.snapshot ? Promise.resolve(this.o.snapshot(where, marks)).catch(() => null) : Promise.resolve(null);
    flagEditor(pin, this.ws, { view: this.o.view, where, shot, marks, text, onDone: () => this.o.layer.setDraft(null) });
  }
}

// a pen line thinned to the points that matter
function simplify(m) {
  if (m.t !== "pen" || m.p.length < 3) return m;
  const out = [m.p[0]];
  for (let i = 1; i < m.p.length - 1; i++) {
    const a = out[out.length - 1], b = m.p[i];
    if (Math.hypot(b[0] - a[0], b[1] - a[1]) > 0.25) out.push(b);
  }
  out.push(m.p[m.p.length - 1]);
  return { ...m, p: out.slice(0, 400) };
}

// ------------------------------------------------------------------ the editor (a new flag, or a flag's thread)
export function flagEditor(anchor, ws, { flag, view, where, shot, marks, text: preset, onDone } = {}) {
  const review = ws.review;
  const isNew = !flag;
  let ask = flag ? flag.ask || "request" : "request";
  const text = h("textarea", { placeholder: "", rows: 3 });
  text.value = flag ? flag.text : preset || "";
  const setPlaceholder = () => { text.placeholder = ask === "question" ? "What do you want to know?" : "What should change here?"; };
  setPlaceholder();
  let w = { ...(flag ? flag.where : where) };
  let drawn = [...(flag ? flag.marks || [] : marks || [])];
  const ctx = h("div.fe-ctx");
  const drawCtx = () => {
    clear(ctx);
    const chip = (label, rm) => h("span.ctx", label, isNew ? h("button", { "data-tip": "Leave out", onclick: rm }, icon("x", 11)) : null);
    for (const r of w.refs || []) ctx.appendChild(chip(r, () => { w.refs = w.refs.filter((x) => x !== r); drawCtx(); }));
    for (const n of w.nets || []) ctx.appendChild(chip(n.split("/").pop(), () => { w.nets = w.nets.filter((x) => x !== n); drawCtx(); }));
    if (w.layer) ctx.appendChild(chip(w.layer, () => { delete w.layer; drawCtx(); }));
    if (w.sheet) ctx.appendChild(h("span.ctx", w.sheet === "/" ? "root sheet" : w.sheet.replace(/^\/|\/$/g, "")));
    if (w.check) ctx.appendChild(h("span.ctx", w.check));
    for (const m of drawn) ctx.appendChild(chip(({ route: "route sketch", pen: "drawing", arrow: "arrow", area: "area", box: "box", text: "note" })[m.t] || m.t,
      () => { drawn = drawn.filter((x) => x !== m); drawCtx(); }));
    if (!ctx.children.length && w.x !== undefined) ctx.appendChild(h("span.ctx", `${(+w.x).toFixed(1)}, ${(+w.y).toFixed(1)} mm`));
  };
  drawCtx();
  const status = flag ? flag.status : "draft";
  const askSeg = h("div.seg.fe-ask", [["request", "Change it"], ["question", "Ask first"]].map(([k, t]) =>
    h("button" + (ask === k ? ".on" : ""), { "data-k": k, onclick: () => { ask = k; for (const b of askSeg.children) b.classList.toggle("on", b.dataset.k === k); setPlaceholder(); },
      "data-tip": k === "request" ? "Claude makes the change and says what it did" : "Claude answers first: it changes things only if it agrees" }, t)));
  const save = async () => {
    const t = text.value.trim();
    if (!t) { text.focus(); toast(ask === "question" ? "Say what you want to know" : "Say what should change", "warn"); return; }
    saveB.disabled = true;
    try {
      if (isNew) {
        const snap = await shot;
        await review.add(view, t, w, snap || undefined, ask, drawn);
      } else if (t !== flag.text || ask !== (flag.ask || "request")) await review.update(flag.id, { text: t, ask });
      pop.close();
    } catch (e) { toast(e.message, "error"); saveB.disabled = false; }
  };
  const saveB = h("button.btn.primary.sm", { onclick: save }, isNew ? "Add flag" : "Save");
  // the thread: what Claude said, the replies, and a box to reply in
  let thread = null, replyBox = null;
  if (!isNew) {
    const msgs = (flag.thread || []).map((m) => h("div.fe-msg." + (m.who === "claude" ? "claude" : "you") + (m.outcome ? "." + m.outcome : ""),
      h("div.fe-who", m.who === "claude" ? "Claude" : "You", m.outcome ? h("span.fe-out." + m.outcome, STATUS_NAME[m.outcome]) : null,
        m.anyway ? h("span.fe-out.anyway", "Do it anyway") : null, h("span.grow"), h("span.tiny.faint", fmtTime(m.at))),
      h("div.fe-say", m.text)));
    thread = msgs.length ? h("div.fe-thread", msgs) : null;
    if (OUTCOME.has(status) || (flag.thread || []).length) {
      const input = h("textarea.fe-reply", { rows: 2, placeholder: status === "declined" ? "Reply, or say do it anyway" : "Reply to Claude" });
      const send = async (anyway) => {
        const t = input.value.trim();
        if (!t && !anyway) { input.focus(); return; }
        try {
          await review.reply(flag.id, t, anyway);
          pop.close();
          toast("Saved: it goes to Claude with the next send", "info", 4500);
        } catch (e) { toast(e.message, "error"); }
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(false); } });
      replyBox = h("div.fe-replybox", input, h("div.fe-rb",
        status === "declined" ? btn("check", "Do it anyway", { onclick: () => send(true), "data-tip": "Claude makes the change after all" }, "sm") : null,
        h("div.grow"), h("button.btn.sm", { onclick: () => send(false) }, "Reply")));
    }
  }
  const editable = isNew || status === "open";
  const foot = h("div.fe-foot",
    isNew ? h("span.small.faint.row", { style: { gap: "3px" } }, kbd("mod+enter")) : null,
    !isNew ? btn("trash-2", null, { "data-tip": "Delete the flag", onclick: async () => { await review.remove(flag.id); pop.close(); } }, "sm ghost danger") : null,
    !isNew && (status === "open" || status === "sent") ? btn("check", "Done", { "data-tip": "Mark it done yourself", onclick: async () => {
      await review.update(flag.id, { status: "done", resolution: "marked done by you" }); pop.close(); } }, "sm ghost") : null,
    !isNew && OUTCOME.has(status) ? btn("rotate-ccw", "Reopen", { onclick: async () => { await review.update(flag.id, { status: "open" }); pop.close(); } }, "sm ghost") : null,
    h("div.grow"),
    h("button.btn.sm", { onclick: () => pop.close() }, isNew ? "Cancel" : "Close"), editable ? saveB : null);
  const img = h("img", { style: { display: "none", width: "100%", height: "150px", objectFit: "cover", borderRadius: "7px", marginTop: "8px", border: "1px solid var(--line)", cursor: "zoom-in" }, onclick: () => lightbox(img.src) });
  if ((flag && flag.snapshot) || (!flag && shot)) {         // room for the picture from the start, so the popover sits right
    img.style.display = ""; img.classList.add("skeleton");
    img.onload = () => img.classList.remove("skeleton");
  }
  if (flag && flag.snapshot) img.src = snapshotURL(ws.pid, flag);
  else if (shot) shot.then((d) => { if (d) img.src = d; else img.style.display = "none"; });
  if (!editable) text.readOnly = true;
  const body = h("div",
    h("div.fe-head", h("span.fnum." + (flag ? flag.status : "draft"), flag ? (PIN_ICON[flag.status] ? icon(PIN_ICON[flag.status], 11) : String(flag.n)) : icon("plus", 11)),
      h("b", isNew ? "New flag" : `Flag ${flag.n}`),
      h("span.small.muted", `${VIEW_NAME[flag ? flag.view : view]}${flag ? " · " + STATUS_NAME[flag.status] : ""}`),
      h("div.grow"), flag ? h("span.tiny.faint", fmtTime(flag.created)) : null),
    editable ? askSeg : h("div.fe-asked", ask === "question" ? "You asked" : "You asked for"),
    text, ctx, img, thread, replyBox, foot);
  const pop = popover(anchor, body, { cls: "flag-editor", onClose: () => { onDone && onDone(); },
    keep: (e) => e.target.closest && e.target.closest(".fpin.draft") });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); pop.close(); }
  });
  setTimeout(() => { if (editable) { text.focus(); if (!isNew) text.setSelectionRange(text.value.length, text.value.length); } }, 20);
  return pop;
}

// ------------------------------------------------------------------ the Review panel
export class ReviewPanel {
  constructor(ws) {
    this.ws = ws; this.review = ws.review; this.filter = "active";
    this.el = h("div.drawer");
    this.review.on("changed", () => { if (this.el.isConnected) this.render(); });
    if (ws.approvals) ws.approvals.on("changed", () => { if (this.el.isConnected && !this.el.contains(document.activeElement)) this.render(); });
    this.note = h("textarea", { placeholder: "Add a note (optional)" });
  }

  render() {
    const r = this.review, el = clear(this.el);
    const counts = { active: r.active().length, replied: r.replied().length, all: r.flags.length };
    el.appendChild(h("div.drawer-head", icon("flag", 15, "accent-t"), h("h3", "Review"), counts.active ? h("span.count", String(counts.active)) : null, h("div.grow"),
      btn("x", null, { onclick: () => this.ws.toggleReview(false), "data-tip": "Close", "data-kbd": "mod+shift+r" }, "sm ghost")));
    el.appendChild(h("div.drawer-filter", h("div.seg", [["active", "To do"], ["replied", "Replied"], ["all", "All"]].map(([v, t]) =>
      h("button" + (this.filter === v ? ".on" : ""), { onclick: () => { this.filter = v; this.render(); } }, t, counts[v] ? h("span.faint", String(counts[v])) : null))),
      h("div.grow"), counts.replied && this.filter !== "active" ? btn("trash-2", null, { "data-tip": "Clear the replied flags", onclick: async () => {
        if (await confirmDialog({ title: "Clear the replied flags?", ok: "Clear" })) r.clearResolved();
      } }, "sm ghost") : null));
    const list = h("div.drawer-list");
    if (this.ws.approvals && this.ws.approvals.pending().length) list.appendChild(this.ws.approvals.list());
    const shown = r.flags.filter((f) => this.filter === "all" || (this.filter === "active" ? f.status === "open" || f.status === "sent" : OUTCOME.has(f.status)))
      .slice().sort((a, b) => (a.status === "sent") - (b.status === "sent") || a.n - b.n);
    if (!shown.length) {
      list.appendChild(h("div.empty.plain", { style: { padding: "34px 14px" } }, h("div.eicon.accent", icon("flag", 20)),
        h("h3", this.filter === "active" ? (r.flags.length ? "All replied" : "No flags yet") : "No flags"),
        h("p", { style: { fontSize: "12.5px" } }, "Press ", h("span.kbd", "C"), " on any view to leave a comment or ask a question.")));
    }
    for (const f of shown) list.appendChild(this.row(f));
    el.appendChild(list);
    const open = r.open();
    const sel = [...r.sel].filter((id) => open.find((f) => f.id === id));
    const n = sel.length || open.length;
    const busy = this.ws.chat && this.ws.chat.busy;
    el.appendChild(h("div.drawer-foot",
      open.length ? this.note : null,
      h("button.btn.primary.block", { disabled: !open.length || busy, onclick: async () => { if (await r.send(sel, this.note.value)) this.note.value = ""; } },
        icon("send", 14), h("span", open.length ? `Send ${sel.length ? sel.length + " selected" : n === 1 ? "1 flag" : n + " flags"} to Claude` : "Nothing to send")),
      busy ? h("div.tiny.faint", { style: { textAlign: "center" } }, "Available when Claude finishes") : null));
  }

  row(f) {
    const r = this.review;
    const selectable = f.status === "open";
    const chk = selectable ? h("input.fchk", { type: "checkbox", checked: r.sel.has(f.id), onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? r.sel.add(f.id) : r.sel.delete(f.id); this.render(); } }) : null;
    const loc = whereLabel(f);
    const last = lastWord(f);
    const replied = f.status === "open" && last && last.who === "you";
    return h("div.flagrow" + (OUTCOME.has(f.status) ? ".replied." + f.status : "") + (this.ws.focusFlag === f.id ? ".on" : ""),
      { onclick: () => this.ws.showFlag(f) },
      chk || h("span", { style: { width: "15px", flex: "none" } }),
      h("div.fnum." + f.status, PIN_ICON[f.status] ? icon(PIN_ICON[f.status], 12) : f.ask === "question" ? "?" : String(f.n)),
      h("div.fbody",
        h("div.ftext", f.text.length > 220 ? f.text.slice(0, 220) + "…" : f.text),
        h("div.fmeta", icon(VIEW_ICON[f.view] || "flag", 12), h("span", VIEW_NAME[f.view]), loc ? h("span", "· " + loc) : null,
          f.ask === "question" ? h("span.badge", { style: { height: "17px" } }, "question") : null,
          (f.marks || []).length ? h("span.badge", { style: { height: "17px" } }, (f.marks || []).some((m) => m.t === "route") ? "route sketch" : "drawing") : null,
          f.status === "sent" ? h("span.badge.info", { style: { height: "17px" } }, "with Claude") : null,
          replied ? h("span.badge.accent", { style: { height: "17px" } }, last.anyway ? "do it anyway" : "your reply") : null,
          f.source === "claude" ? h("span.badge.accent", { style: { height: "17px" } }, "from Claude") : null),
        last && last.who === "claude" ? h("div.fres." + (last.outcome || "done"),
          h("b", last.outcome === "declined" ? "Claude disagrees: " : last.outcome === "answered" ? "Claude: " : "Done: "), last.text) : null),
      h("div.fact",
        (f.status === "open" || f.status === "sent") ? btn("check", null, { "data-tip": "Mark done", onclick: (e) => { e.stopPropagation(); r.update(f.id, { status: "done", resolution: "marked done by you" }); } }, "sm ghost") : null,
        btn("trash-2", null, { "data-tip": "Delete", onclick: (e) => { e.stopPropagation(); r.remove(f.id); } }, "sm ghost")));
  }
}
