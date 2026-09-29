// The Mac app's bridge (window.webkit.messageHandlers.tw): menus, Notification Center, the Dock,
// native Open / Save panels, files dropped on the window, dragging the window by its header. In a
// browser every call falls back to the web equivalent, or does nothing.
import { Emitter, debounce } from "./util.js";

const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.tw;
export const isNative = !!handler;
export const events = new Emitter();         // command, files, open, focus, fullscreen, downloaded, toast

function post(type, data = {}) {
  if (!handler) return Promise.resolve(null);
  try { return Promise.resolve(handler.postMessage({ type, ...data })); } catch (e) { return Promise.resolve(null); }
}

window.twNative = { receive(msg) { if (msg && msg.type) events.emit(msg.type, msg); } };

export const native = {
  ready: () => post("ready"),
  context: (project, name) => post("context", { project: project || "", name: name || "" }),
  // a notification when the window is in the background
  notify(title, body, pid) {
    if (isNative) return post("notify", { title, body: String(body || "").slice(0, 180), pid });
    try {
      if (!document.hidden || !window.Notification || Notification.permission !== "granted") return;
      const n = new Notification(title, { body: String(body || "").slice(0, 140), tag: "tracewright" });
      n.onclick = () => { window.focus(); n.close(); };
    } catch (e) { /* unavailable */ }
  },
  askNotify() { if (!isNative && window.Notification && Notification.permission === "default") Notification.requestPermission().catch(() => {}); },
  badge: (text) => post("badge", { text: text ? String(text) : "" }),
  attention: () => post("attention"),
  openURL(url) { if (isNative) post("open", { url }); else window.open(url, "_blank", "noopener"); },
  activate: () => post("activate"),                     // bring the window to the front (after a browser sign-in)
  openPath: (path) => post("openPath", { path }),
  reveal: (path) => post("reveal", { path }),
  // a native Open panel: kind folder | file | any; resolves to a path (or paths), or null
  pick: (opts) => post("pick", opts),
  // dictation (the Mac app's speech recognizer): action start | stop | cancel; words arrive as "dictation" events
  dictate: (action, opts = {}) => post("dictate", { action, ...opts }),
  save(url) { if (isNative) post("save", { url }); else { const a = document.createElement("a"); a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); } },
  theme: (mode, bg) => post("theme", { mode, bg }),
  // the app lock (Touch ID, or the Mac's password): {enabled, minutes, available, biometrics, locked}
  lockState: () => post("lockState"),
  lockSet: (opts) => post("lockSet", opts),
  lockNow: () => post("lockNow"),
  // a forgotten password, on this Mac: Touch ID (or the Mac's password), then `tracewright account reset`;
  // resolves to {ok, message} (cancelled: true when the Mac's owner did not confirm)
  resetPassword: (email, password) => post("resetPassword", { email, password }),
  quit: () => post("quit"),
};

// The header's empty space drags the window (the Mac app's title bar is the page's own header):
// every [data-drag] element is a region, its controls are holes in it.
const CONTROLS = "button, a, input, select, textarea, [role=button], [data-nodrag], .tab, .stagechip, .pill, .cbtn, .seg, .modepill, .ctitle";
function regions() {
  const out = [], holes = [];
  for (const el of document.querySelectorAll("[data-drag]")) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    out.push([r.left, r.top, r.width, r.height]);
    for (const c of el.querySelectorAll(CONTROLS)) {
      const q = c.getBoundingClientRect();
      if (q.width && q.height) holes.push([q.left - 2, q.top - 2, q.width + 4, q.height + 4]);
    }
  }
  post("drag", { regions: out, holes });
}

export function initNative() {
  if (!isNative) return;
  document.documentElement.classList.add("native");
  const update = debounce(regions, 80);
  new MutationObserver(update).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class", "style"] });
  addEventListener("resize", update);
  events.on("fullscreen", (m) => { document.documentElement.classList.toggle("fullscreen", !!m.on); update(); });
  update();
  native.ready();
}
