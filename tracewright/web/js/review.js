// Review flags: pins you drop on the board, the schematic or the 3D view ("move C3 closer to U1"),
// or findings you pick from the checks. They collect in the Review panel and go to Claude together,
// each with a snapshot of the spot; Claude marks each one fixed (or explains why not).
import { h, clear, api, toast, btn, popover, confirmDialog, fmtTime, Emitter, kbd, lightbox } from "./util.js";
import { icon } from "./icons.js";

const enc = encodeURIComponent;
export const VIEW_ICON = { board: "circuit-board", schematic: "waypoints", "3d": "box", check: "list-checks" };
const VIEW_NAME = { board: "Board", schematic: "Schematic", "3d": "3D", check: "Check" };
const STATUS_NAME = { open: "Open", sent: "With Claude", fixed: "Fixed", wontfix: "Won't fix" };

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
    if (live) for (const f of this.flags) {                         // Claude's answers, as they come
      const b = before.get(f.id);
      if (b && b.status !== f.status && (f.status === "fixed" || f.status === "wontfix") && f.resolved_by === "claude")
        toast(`${f.status === "fixed" ? "Fixed" : "Won't fix"} #${f.n}: ${f.resolution || f.text}`.slice(0, 160), f.status === "fixed" ? "ok" : "info", 6000,
          { label: "Show", run: () => this.ws.showFlag(f) });
    }
    this.emit("changed", this.flags);
  }

  get(id) { return this.flags.find((f) => f.id === id); }
  active() { return this.flags.filter((f) => f.status === "open" || f.status === "sent"); }
  open() { return this.flags.filter((f) => f.status === "open"); }

  // the flags a view shows: its own, plus those with a place in it (a 3D flag on the board, a finding's spot)
  forView(view, sheet) {
    return this.flags.filter((f) => {
      const w = f.where || {};
      if (view === "schematic") return (f.view === "schematic" || f.view === "check") && w.sheet && w.sheet === sheet && w.x !== undefined;
      if (w.x === undefined || w.sheet) return false;
      return f.view === view || f.view === "check" || (view === "board" && f.view === "3d") || (view === "3d" && f.view === "board");
    });
  }

  async add(view, text, where, snapshot) {
    const f = await api(`/api/projects/${enc(this.pid)}/review`, { body: { view, text, where, snapshot } });
    if (!this.get(f.id)) { this.flags = [...this.flags, f]; this.emit("changed", this.flags); }
    return f;
  }

  async update(id, changes) { return api(`/api/projects/${enc(this.pid)}/review/${enc(id)}`, { method: "PATCH", body: changes }); }

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
    if (!list.length) { toast("No open flags. Press C to add one.", "info"); return false; }
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

// ------------------------------------------------------------------ pins over a view
// opts: {view, project(x, y) -> [sx, sy] | null, sheet() -> the sheet shown, onPin(flag, el)}
export class FlagLayer {
  constructor(container, review, opts) {
    this.review = review; this.o = opts;
    this.el = h("div.flaglayer");
    container.appendChild(this.el);
    this.items = [];
    this.draft = null;
    this.off = review.on("changed", () => this.render());
    this.render();
  }

  destroy() { this.off(); this.el.remove(); }

  render() {
    clear(this.el);
    this.items = [];
    const showDone = this.o.showDone ? this.o.showDone() : true;
    for (const f of this.review.forView(this.o.view, this.o.sheet && this.o.sheet())) {
      if (!showDone && f.status !== "open" && f.status !== "sent") continue;
      const region = f.where.region ? h("div.fregion." + f.status) : null;
      const pin = h("div.fpin." + f.status, { "data-tip": f.text.length > 90 ? f.text.slice(0, 90) + "…" : f.text,
        onmousedown: (e) => e.stopPropagation(), onclick: (e) => { e.stopPropagation(); this.o.onPin && this.o.onPin(f, pin); } },
        f.status === "fixed" ? icon("check", 13) : String(f.n));
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

  setDraft(where) { this.draft = where ? { where, status: "draft" } : null; this.render(); return this.draft && this.draft.pin; }

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
  }

  pinFor(id) { const it = this.items.find((x) => x.f.id === id); return it && it.pin; }
  mark(id) { for (const it of this.items) it.pin.classList.toggle("on", it.f.id === id); }
}

// ------------------------------------------------------------------ the flag tool in a view
// opts: {view, surface: the element clicked, toWorld(px, py) -> [x, y] | null, context(where) -> where,
//        snapshot(where) -> Promise<dataURL | null>, layer: FlagLayer, hud: element for the hint, onChange(active)}
export class FlagTool {
  constructor(ws, opts) {
    this.ws = ws; this.o = opts; this.active = false;
    this.hint = h("div.vhint", icon("flag", 14), h("span", "Click or drag to mark an area"), h("span.kbd", "esc"),
      btn("x", null, { onclick: () => this.toggle(false), "data-tip": "Exit flag tool" }, "sm ghost"));
    this.hint.style.display = "none";
    opts.hud.appendChild(this.hint);
  }

  toggle(on = !this.active) {
    this.active = on;
    this.hint.style.display = on ? "" : "none";
    this.o.onChange && this.o.onChange(on);
    if (!on) this.o.layer.setDraft(null);
  }

  // mousedown on the view's surface: true when the flag tool took it
  down(e) {
    if (!this.active || e.button !== 0) return false;
    e.preventDefault();
    const r = this.o.surface.getBoundingClientRect();
    const p0 = [e.clientX - r.left, e.clientY - r.top];
    const w0 = this.o.toWorld(p0[0], p0[1], e);
    if (!w0) return true;
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

  // a flag at a place given by code (a selection, the 3D pick)
  async create(where) {
    where = this.o.context ? this.o.context(where) : where;
    const pin = this.o.layer.setDraft(where);
    const shot = this.o.snapshot ? this.o.snapshot(where).catch(() => null) : Promise.resolve(null);
    flagEditor(pin, this.ws, { view: this.o.view, where, shot, onDone: () => this.o.layer.setDraft(null) });
  }
}

// ------------------------------------------------------------------ the editor (new or existing flag)
export function flagEditor(anchor, ws, { flag, view, where, shot, onDone } = {}) {
  const review = ws.review;
  const isNew = !flag;
  const text = h("textarea", { placeholder: isNew ? "What should change here?" : "", rows: 3 });
  text.value = flag ? flag.text : "";
  let w = { ...(flag ? flag.where : where) };
  const ctx = h("div.fe-ctx");
  const drawCtx = () => {
    clear(ctx);
    const chip = (label, rm) => h("span.ctx", label, isNew ? h("button", { "data-tip": "Leave out", onclick: rm }, icon("x", 11)) : null);
    for (const r of w.refs || []) ctx.appendChild(chip(r, () => { w.refs = w.refs.filter((x) => x !== r); drawCtx(); }));
    for (const n of w.nets || []) ctx.appendChild(chip(n.split("/").pop(), () => { w.nets = w.nets.filter((x) => x !== n); drawCtx(); }));
    if (w.layer) ctx.appendChild(chip(w.layer, () => { delete w.layer; drawCtx(); }));
    if (w.sheet) ctx.appendChild(h("span.ctx", w.sheet === "/" ? "root sheet" : w.sheet.replace(/^\/|\/$/g, "")));
    if (w.check) ctx.appendChild(h("span.ctx", w.check));
    if (!ctx.children.length && w.x !== undefined) ctx.appendChild(h("span.ctx", `${(+w.x).toFixed(1)}, ${(+w.y).toFixed(1)} mm`));
  };
  drawCtx();
  const status = flag ? flag.status : "draft";
  const save = async () => {
    const t = text.value.trim();
    if (!t) { text.focus(); toast("Say what should change", "warn"); return; }
    saveB.disabled = true;
    try {
      if (isNew) {
        const snap = await shot;
        await review.add(view, t, w, snap || undefined);
      } else if (t !== flag.text) await review.update(flag.id, { text: t });
      pop.close();
    } catch (e) { toast(e.message, "error"); saveB.disabled = false; }
  };
  const saveB = h("button.btn.primary.sm", { onclick: save }, isNew ? "Add flag" : "Save");
  const foot = h("div.fe-foot",
    isNew ? h("span.small.faint.row", { style: { gap: "3px" } }, kbd("mod+enter")) : null,
    !isNew ? btn("trash-2", null, { "data-tip": "Delete the flag", onclick: async () => { await review.remove(flag.id); pop.close(); } }, "sm ghost danger") : null,
    !isNew && (status === "open" || status === "sent") ? btn("check", "Done", { "data-tip": "Mark it fixed yourself", onclick: async () => {
      await review.update(flag.id, { status: "fixed", resolution: "marked fixed by you" }); pop.close(); } }, "sm ghost") : null,
    !isNew && (status === "fixed" || status === "wontfix") ? btn("rotate-ccw", "Reopen", { onclick: async () => { await review.update(flag.id, { status: "open" }); pop.close(); } }, "sm ghost") : null,
    h("div.grow"),
    h("button.btn.sm", { onclick: () => pop.close() }, isNew ? "Cancel" : "Close"), saveB);
  const img = h("img", { style: { display: "none", width: "100%", height: "150px", objectFit: "cover", borderRadius: "7px", marginTop: "8px", border: "1px solid var(--line)", cursor: "zoom-in" }, onclick: () => lightbox(img.src) });
  if ((flag && flag.snapshot) || (!flag && shot)) {         // room for the picture from the start, so the popover sits right
    img.style.display = ""; img.classList.add("skeleton");
    img.onload = () => img.classList.remove("skeleton");
  }
  if (flag && flag.snapshot) img.src = snapshotURL(ws.pid, flag);
  else if (shot) shot.then((d) => { if (d) img.src = d; else img.style.display = "none"; });
  const body = h("div",
    h("div.fe-head", h("span.fnum", flag ? String(flag.n) : icon("plus", 11)), h("b", isNew ? "New flag" : `Flag ${flag.n}`),
      h("span.small.muted", `${VIEW_NAME[flag ? flag.view : view]}${flag ? " · " + STATUS_NAME[flag.status] : ""}`),
      h("div.grow"), flag ? h("span.tiny.faint", fmtTime(flag.created)) : null),
    text, ctx, img,
    flag && flag.resolution ? h("div.fe-res", h("b", flag.status === "fixed" ? "Fixed: " : flag.status === "wontfix" ? "Won't fix: " : ""), flag.resolution) : null,
    foot);
  const pop = popover(anchor, body, { cls: "flag-editor", onClose: () => { onDone && onDone(); } ,
    keep: (e) => e.target.closest && e.target.closest(".fpin.draft") });
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); pop.close(); }
  });
  setTimeout(() => { text.focus(); if (!isNew) text.setSelectionRange(text.value.length, text.value.length); }, 20);
  return pop;
}

// ------------------------------------------------------------------ the Review panel
export class ReviewPanel {
  constructor(ws) {
    this.ws = ws; this.review = ws.review; this.filter = "active";
    this.el = h("div.drawer");
    this.review.on("changed", () => { if (this.el.isConnected) this.render(); });
    this.note = h("textarea", { placeholder: "Add a note (optional)" });
  }

  render() {
    const r = this.review, el = clear(this.el);
    const counts = { active: r.active().length, fixed: r.flags.filter((f) => f.status === "fixed" || f.status === "wontfix").length, all: r.flags.length };
    el.appendChild(h("div.drawer-head", icon("flag", 15, "accent-t"), h("h3", "Review"), counts.active ? h("span.count", String(counts.active)) : null, h("div.grow"),
      btn("x", null, { onclick: () => this.ws.toggleReview(false), "data-tip": "Close", "data-kbd": "mod+shift+r" }, "sm ghost")));
    el.appendChild(h("div.drawer-filter", h("div.seg", [["active", "To do"], ["fixed", "Resolved"], ["all", "All"]].map(([v, t]) =>
      h("button" + (this.filter === v ? ".on" : ""), { onclick: () => { this.filter = v; this.render(); } }, t, counts[v] ? h("span.faint", String(counts[v])) : null))),
      h("div.grow"), counts.fixed && this.filter !== "active" ? btn("trash-2", null, { "data-tip": "Clear resolved", onclick: async () => {
        if (await confirmDialog({ title: "Clear resolved flags?", ok: "Clear" })) r.clearResolved();
      } }, "sm ghost") : null));
    const list = h("div.drawer-list");
    const shown = r.flags.filter((f) => this.filter === "all" || (this.filter === "active" ? f.status === "open" || f.status === "sent" : f.status === "fixed" || f.status === "wontfix"))
      .slice().sort((a, b) => (a.status === "sent") - (b.status === "sent") || a.n - b.n);
    if (!shown.length) {
      list.appendChild(h("div.empty.plain", { style: { padding: "34px 14px" } }, h("div.eicon.accent", icon("flag", 20)),
        h("h3", this.filter === "active" ? (r.flags.length ? "All done" : "No flags yet") : "No flags"),
        h("p", { style: { fontSize: "12.5px" } }, "Press ", h("span.kbd", "C"), " on any view to mark what should change.")));
    }
    for (const f of shown) list.appendChild(this.row(f));
    el.appendChild(list);
    const open = r.open();
    const sel = [...r.sel].filter((id) => open.find((f) => f.id === id));
    const n = sel.length || open.length;
    const sending = r.flags.some((f) => f.status === "sent");
    const busy = this.ws.chat && this.ws.chat.busy;
    el.appendChild(h("div.drawer-foot",
      open.length ? this.note : null,
      h("button.btn.primary.block", { disabled: !open.length || busy, onclick: async () => { if (await r.send(sel, this.note.value)) this.note.value = ""; } },
        icon("send", 14), h("span", open.length ? `Send ${sel.length ? sel.length + " selected" : n === 1 ? "1 flag" : n + " flags"} to Claude` : "No open flags")),
      busy ? h("div.tiny.faint", { style: { textAlign: "center" } }, "Available when Claude finishes") : null));
  }

  row(f) {
    const r = this.review;
    const w = f.where || {};
    const selectable = f.status === "open";
    const chk = selectable ? h("input.fchk", { type: "checkbox", checked: r.sel.has(f.id), onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? r.sel.add(f.id) : r.sel.delete(f.id); this.render(); } }) : null;
    const loc = whereLabel(f);
    return h("div.flagrow" + (f.status === "fixed" || f.status === "wontfix" ? ".done" : "") + (this.ws.focusFlag === f.id ? ".on" : ""),
      { onclick: () => this.ws.showFlag(f) },
      chk || h("span", { style: { width: "15px", flex: "none" } }),
      h("div.fnum." + f.status, f.status === "fixed" ? icon("check", 12) : String(f.n)),
      h("div.fbody",
        h("div.ftext", f.text.length > 220 ? f.text.slice(0, 220) + "…" : f.text),
        h("div.fmeta", icon(VIEW_ICON[f.view] || "flag", 12), h("span", VIEW_NAME[f.view]), loc ? h("span", "· " + loc) : null,
          f.status === "sent" ? h("span.badge.info", { style: { height: "17px" } }, "with Claude") : null,
          f.source === "claude" ? h("span.badge.accent", { style: { height: "17px" } }, "from Claude") : null),
        f.resolution ? h("div.fres" + (f.status === "wontfix" ? ".wontfix" : ""), f.resolution) : null),
      h("div.fact",
        (f.status === "open" || f.status === "sent") ? btn("check", null, { "data-tip": "Mark fixed", onclick: (e) => { e.stopPropagation(); r.update(f.id, { status: "fixed", resolution: "marked fixed by you" }); } }, "sm ghost") : null,
        btn("trash-2", null, { "data-tip": "Delete", onclick: (e) => { e.stopPropagation(); r.remove(f.id); } }, "sm ghost")));
  }
}
