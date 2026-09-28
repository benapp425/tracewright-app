// Small DOM + API helpers shared by every view: elements, the API, toasts, dialogs, menus, popovers,
// tooltips and the project event stream.
import { icon } from "./icons.js";

// The DOM's append / prepend / replaceChildren write null, undefined and false out as text ("null");
// the views pass `cond ? el : null` everywhere, as h() allows, so these leave such values out too.
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const k of ["append", "prepend", "replaceChildren"]) {
    const orig = proto[k];
    if (!orig || orig.__twSkipsEmpty) continue;
    const wrapped = function (...kids) { return orig.apply(this, kids.filter((x) => x !== null && x !== undefined && x !== false)); };
    wrapped.__twSkipsEmpty = true;
    proto[k] = wrapped;
  }
}

export function h(tag, attrs, ...kids) {
  const [name, ...cls] = tag.split(".");
  const el = document.createElement(name || "div");
  if (cls.length) el.className = cls.join(" ");
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) { kids.unshift(attrs); attrs = null; }
  let value;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "value") { value = v; continue; }              // a property, set once the children (a select's options) exist
    if (k === "class") el.className += (el.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  if (value !== undefined) {
    if ("value" in el) el.value = value; else el.setAttribute("value", value);
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export { icon };

// A button with an icon: btn("download", "Download", {onclick}, "sm ghost")
export function btn(ic, label, attrs = {}, mods = "") {
  const cls = "button.btn" + (mods ? "." + mods.split(" ").filter(Boolean).join(".") : "") + (label ? "" : ".icon");
  return h(cls, attrs, ic ? icon(ic, mods.includes("sm") ? 14 : 15) : null, label ? h("span", label) : null);
}

export async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  let data = null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("json")) data = await r.json(); else data = await r.text();
  if (r.status === 401 && !path.startsWith("/api/login")) signedOut(data);
  if (!r.ok) {
    const msg = (data && data.error) || (typeof data === "string" ? data : r.statusText);
    const e = new Error(msg); e.status = r.status; e.data = data; throw e;
  }
  return data;
}

// Upload files (multipart) with extra fields; returns the JSON reply.
export async function upload(path, files, fields = {}) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  for (const f of files) fd.append("file", f, f.name);
  const r = await fetch(path, { method: "POST", body: fd });
  const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : await r.text();
  if (r.status === 401) signedOut(data);
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

// A 401: this window's key is gone (the app restarted: reload through it) or, on a server, sign in.
function signedOut(data) {
  if (data && data.signin) {                           // accounts: the app shows its sign-in screen
    window.dispatchEvent(new CustomEvent("tw:signedout"));
    throw new Error("sign in first");
  }
  location.href = data && data.locked ? "/" : "/login";
  throw new Error("sign in first");
}

// ------------------------------------------------------------------ toasts
const TOAST_ICON = { info: "info", ok: "circle-check", warn: "triangle-alert", error: "circle-alert" };

// Toasts sit at the top of the work area, under the tabs: never over the buttons along the bottom
// (the review flags' Send, the selection bars, the chat box).
export function toast(text, level = "info", ms = 4200, action = null) {
  const box = document.getElementById("toasts");
  const area = document.querySelector(".mainpane") || document.querySelector(".page");
  if (area) {
    const r = area.getBoundingClientRect();
    box.style.left = Math.round(r.left + r.width / 2) + "px";
    box.style.top = Math.round(Math.max(r.top, 0) + (area.classList.contains("mainpane") ? 50 : 14)) + "px";
  } else { box.style.left = "50%"; box.style.top = "64px"; }
  const t = h("div.toast." + level, icon(TOAST_ICON[level] || "info", 16), h("div.t-body", text),
    action ? h("button.btn.sm.t-act", { onclick: () => { action.run(); gone(); } }, action.label) : null,
    h("button.t-x", { onclick: () => gone(), "aria-label": "Dismiss" }, icon("x", 12)));
  const gone = () => { t.classList.add("out"); setTimeout(() => t.remove(), 220); };
  box.appendChild(t);
  if (box.children.length > 5) box.firstChild.remove();
  let timer = setTimeout(gone, level === "error" ? Math.max(ms, 7000) : ms);
  t.addEventListener("mouseenter", () => clearTimeout(timer));
  t.addEventListener("mouseleave", () => { timer = setTimeout(gone, 2000); });
  return t;
}

// ------------------------------------------------------------------ time and sizes
export function fmtTime(s) {
  if (!s) return "";
  const d = typeof s === "number" ? new Date(s * 1000) : new Date(s);
  const now = new Date(), dt = (now - d) / 1000;
  if (dt < 60) return "just now";
  if (dt < 3600) return Math.floor(dt / 60) + " min ago";
  if (dt < 86400) return Math.floor(dt / 3600) + " h ago";
  if (dt < 86400 * 7) return Math.floor(dt / 86400) + " d ago";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
}

export function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

export function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const isMac = /Mac/.test(navigator.platform) || /Macintosh/.test(navigator.userAgent);

// "mod+shift+k" -> ["⌘", "⇧", "K"]
export function keys(combo) {
  const map = { mod: isMac ? "⌘" : "Ctrl", shift: "⇧", alt: isMac ? "⌥" : "Alt", ctrl: isMac ? "⌃" : "Ctrl", enter: "↩", esc: "Esc",
                up: "↑", down: "↓", left: "←", right: "→", backspace: "⌫", space: "Space" };
  return combo.split("+").map((k) => map[k] || k.toUpperCase());
}
export function kbd(combo) { return keys(combo).map((k) => h("span.kbd", k)); }

// ------------------------------------------------------------------ dialogs
// A modal. content: nodes, or {title, sub, icon, body, actions: [nodes], cls, danger}.
export function modal(content, { onClose, cls } = {}) {
  const bg = h("div.modal-bg");
  let box;
  if (content && !Array.isArray(content) && !(content instanceof Node) && (content.title || content.body)) {
    const c = content;
    box = h("div.modal" + (c.cls ? "." + c.cls : ""),
      h("div.modal-head", c.icon ? h("div.mhi" + (c.danger ? ".danger" : ""), icon(c.icon, 18)) : null,
        h("div.grow", h("h3", c.title), c.sub ? h("div.sub", c.sub) : null),
        h("button.btn.ghost.icon.sm", { "data-tip": "Close", onclick: () => close() }, icon("x", 15))),
      h("div.modal-body", c.body || []),
      c.actions && c.actions.length ? h("div.modal-foot", c.actions) : null);
  } else {
    box = h("div.modal" + (cls ? "." + cls : ""), h("div.modal-body", { style: { paddingTop: "18px" } }, content));
  }
  bg.appendChild(box);
  const prevFocus = document.activeElement;
  const close = () => {
    bg.remove(); document.removeEventListener("keydown", esc, true); onClose && onClose();
    if (prevFocus && prevFocus.focus) try { prevFocus.focus(); } catch {}
  };
  const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); } };
  bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
  document.addEventListener("keydown", esc, true);
  document.body.appendChild(bg);
  setTimeout(() => { const f = box.querySelector("[autofocus], textarea, input:not([type=checkbox]):not([type=radio])"); if (f) f.focus(); }, 30);
  return { close, box };
}

// await confirmDialog({title, text, ok: "Delete", danger: true}) -> true / false
export function confirmDialog({ title, text, ok = "OK", cancel = "Cancel", danger = false, icon: ic } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } m.close(); };
    const okb = h("button.btn.primary" + (danger ? ".danger" : ""), { onclick: () => finish(true) }, ok);
    const m = modal({ title, icon: ic || (danger ? "triangle-alert" : null), danger, cls: "narrow", body: text ? [h("p", text)] : [],
      actions: [h("button.btn", { onclick: () => finish(false) }, cancel), okb] }, { onClose: () => { if (!done) { done = true; resolve(false); } } });
    m.box.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(true); } });
    setTimeout(() => okb.focus(), 40);
  });
}

// await promptDialog({title, label, value, placeholder, ok}) -> string or null
export function promptDialog({ title, text, label, value = "", placeholder = "", ok = "OK", multiline = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = multiline ? h("textarea", { placeholder, style: { minHeight: "90px" } }) : h("input", { value, placeholder });
    if (multiline) input.value = value;
    const finish = (v) => { if (!done) { done = true; resolve(v); } m.close(); };
    const m = modal({ title, cls: "narrow", body: [text ? h("p", text) : null, h("div.field", label ? h("label", label) : null, input)],
      actions: [h("button.btn", { onclick: () => finish(null) }, "Cancel"), h("button.btn.primary", { onclick: () => finish(input.value) }, ok)] },
      { onClose: () => { if (!done) { done = true; resolve(null); } } });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing && (!multiline || e.metaKey || e.ctrlKey)) { e.preventDefault(); finish(input.value); }
    });
    setTimeout(() => { input.focus(); input.select && input.select(); }, 40);
  });
}

// ------------------------------------------------------------------ menus and popovers
function place(el, anchor, { align = "start", gap = 6, above = false } = {}) {
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : anchor;
  document.body.appendChild(el);
  const w = el.offsetWidth, hh = el.offsetHeight;
  let x = align === "end" ? r.right - w : r.left;
  let y = above ? r.top - hh - gap : r.bottom + gap;
  if (y + hh > innerHeight - 8) y = Math.max(8, r.top - hh - gap);
  x = Math.max(8, Math.min(x, innerWidth - w - 8));
  el.style.left = x + "px"; el.style.top = Math.max(8, y) + "px";
}

let openMenu = null;
// A dropdown under `anchor` (an element or a {left, top, right, bottom} rect). Items: {label, icon, run,
// kbd, danger, checked, disabled} | "-" | {head}. Clicking the same anchor again closes it.
export function menu(anchor, items, opts = {}) {
  if (openMenu) {
    const same = openMenu._anchor === anchor;
    openMenu._close();
    if (same) return null;
  }
  const m = h("div.menu");
  const rows = [];
  for (const it of items) {
    if (!it) continue;
    if (it === "-") { m.appendChild(h("div.menu-sep")); continue; }
    if (it.head) { m.appendChild(h("div.menu-head", it.head)); continue; }
    if (it.custom) { m.appendChild(it.custom); continue; }
    const row = h("div.mi" + (it.danger ? ".danger" : "") + (it.disabled ? ".disabled" : ""), { onclick: () => { close(); it.run && it.run(); } },
      it.checked !== undefined ? h("span.mi-check", it.checked ? icon("check", 14) : null) : it.icon ? icon(it.icon, 15) : null,
      h("span.grow.ellipsis", it.label), it.kbd ? h("span.mi-kbd", keys(it.kbd).join("")) : it.hint ? h("span.mi-kbd", it.hint) : null);
    rows.push(row);
    m.appendChild(row);
  }
  let hi = -1;
  const setHi = (i) => { rows.forEach((r, j) => r.classList.toggle("hi", j === i)); hi = i; };
  const off = (e) => { if (!m.contains(e.target) && !(anchor.contains && anchor.contains(e.target))) close(); };
  const key = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setHi(Math.min(rows.length - 1, hi + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHi(Math.max(0, hi - 1)); }
    else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); rows[hi].click(); }
  };
  const close = () => {
    m.remove(); openMenu = null;
    document.removeEventListener("mousedown", off, true);
    document.removeEventListener("keydown", key, true);
    window.removeEventListener("blur", close);
    window.removeEventListener("resize", close);
  };
  m._anchor = anchor; m._close = close;
  place(m, anchor, opts);
  openMenu = m;
  setTimeout(() => {
    document.addEventListener("mousedown", off, true);
    document.addEventListener("keydown", key, true);
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
  }, 0);
  return m;
}

// A popover with arbitrary content next to `anchor`; closes on a click outside or Escape.
export function popover(anchor, content, { cls = "", align = "start", onClose, keep } = {}) {
  document.querySelectorAll(".popover").forEach((p) => p._close && p._close());
  const pop = h("div.popover" + (cls ? "." + cls : ""), content);
  const off = (e) => { if (!pop.contains(e.target) && !(anchor.contains && anchor.contains(e.target)) && !(keep && keep(e))) close(); };
  const key = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const close = () => {
    if (!pop.isConnected) return;
    pop.remove();
    document.removeEventListener("mousedown", off, true);
    document.removeEventListener("keydown", key, true);
    onClose && onClose();
  };
  pop._close = close;
  place(pop, anchor, { align });
  setTimeout(() => { document.addEventListener("mousedown", off, true); document.addEventListener("keydown", key, true); }, 0);
  return { el: pop, close };
}

export function lightbox(src) {
  const lb = h("div.lightbox", { onclick: () => lb.remove() }, h("img", { src }));
  const esc = (e) => { if (e.key === "Escape") { lb.remove(); document.removeEventListener("keydown", esc, true); } };
  document.addEventListener("keydown", esc, true);
  document.body.appendChild(lb);
}

// ------------------------------------------------------------------ tooltips: data-tip="text", data-kbd="mod+k"
let tipEl = null, tipT = null, tipFor = null;
export function initTooltips() {
  tipEl = h("div.tooltip");
  document.body.appendChild(tipEl);
  const hide = () => { clearTimeout(tipT); tipEl.classList.remove("on"); tipFor = null; };
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest && e.target.closest("[data-tip]");
    if (t === tipFor) return;
    hide();
    if (!t || !t.dataset.tip) return;
    tipFor = t;
    tipT = setTimeout(() => {
      if (!t.isConnected) return;
      clear(tipEl).append(h("span", t.dataset.tip), t.dataset.kbd ? h("span.row", { style: { gap: "3px" } }, kbd(t.dataset.kbd)) : null);
      const r = t.getBoundingClientRect();
      tipEl.style.left = "0px"; tipEl.style.top = "0px";
      tipEl.classList.add("on");
      const w = tipEl.offsetWidth, hh = tipEl.offsetHeight;
      let y = r.bottom + 7;
      if (y + hh > innerHeight - 6) y = r.top - hh - 7;
      tipEl.style.left = Math.max(6, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 6)) + "px";
      tipEl.style.top = y + "px";
    }, 450);
  });
  document.addEventListener("mousedown", hide, true);
  window.addEventListener("blur", hide);
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied", "ok", 1500); }
  catch { toast("Could not copy", "error"); }
}

// ------------------------------------------------------------------ events
export class Emitter {
  constructor() { this.l = {}; }
  on(t, f) { (this.l[t] = this.l[t] || []).push(f); return () => this.off(t, f); }
  off(t, f) { this.l[t] = (this.l[t] || []).filter((x) => x !== f); }
  emit(t, d) { for (const f of this.l[t] || []) try { f(d); } catch (e) { console.error(e); } for (const f of this.l["*"] || []) try { f(t, d); } catch (e) { console.error(e); } }
}

// A reconnecting websocket for one project's events.
export class Events extends Emitter {
  constructor(pid) {
    super();
    this.pid = pid; this.seq = 0; this.closed = false; this.tries = 0; this.open();
  }
  open() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/api/projects/${encodeURIComponent(this.pid)}/ws?since=${this.seq}`);
    this.ws.onmessage = (m) => {
      let ev; try { ev = JSON.parse(m.data); } catch { return; }
      if (ev.seq) this.seq = Math.max(this.seq, ev.seq);
      this.emit(ev.type, ev);
    };
    this.ws.onclose = () => {
      if (this.closed) return;
      this.emit("_disconnected");
      setTimeout(() => this.open(), Math.min(8000, 800 * 2 ** Math.min(this.tries++, 4)));
    };
    this.ws.onopen = () => { this.tries = 0; this.emit("_connected"); };
  }
  send(obj) { try { this.ws.send(JSON.stringify(obj)); } catch {} }
  close() { this.closed = true; try { this.ws.close(); } catch {} }
}

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
