// The workspace tour: a spotlight on each part of the workspace, with a line on what it is for. It runs
// the first time someone opens a project, and again from Help. Steps whose part is not on screen
// (a hidden chat, a narrow window) are passed over.
import { h, clear } from "./util.js";
import { icon } from "./icons.js";

const STEPS = [
  { sel: ".chatpane", title: "Chat with Claude", text: "Ask for a review, a part change, placement or routing. You can send notes while it works.", side: "right" },
  { sel: ".tabs", title: "Views", text: "Board, schematic, 3D, BOM, checks and ordering. Pin the ones you use.", side: "bottom" },
  { sel: ".mainpane", title: "Live updates", text: "Changes appear here and in KiCad as Claude works. Select parts or nets to ask about them.", side: "left" },
  { sel: ".tabs-right [data-tip^='Review']", title: "Review flags", text: "Press C to mark something on any view, then send your flags to Claude.", side: "bottom" },
  { sel: ".tabs-right [data-tip^='Tools']", title: "Tools", text: "Calculators, timelapse, Mission Control and version comparison.", side: "bottom" },
  { sel: ".topbar .helpbtn", title: "Help", text: "Shortcuts, release notes and this tour. ⌘K runs any command.", side: "bottom" },
];

export class Tour {
  constructor(done) { this.done = done; this.at = 0; this.steps = STEPS.filter((s) => visible(s.sel)); }

  start() {
    if (!this.steps.length) return;
    this.el = h("div.tour");
    this.hole = h("div.tour-hole");
    this.card = h("div.tour-card");
    this.el.append(this.hole, this.card);
    document.body.appendChild(this.el);
    this.onKey = (e) => {
      if (e.key === "Escape") this.end();
      else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); this.go(1); }
      else if (e.key === "ArrowLeft") this.go(-1);
    };
    addEventListener("keydown", this.onKey, true);
    this.onResize = () => this.place();
    addEventListener("resize", this.onResize);
    requestAnimationFrame(() => this.el.classList.add("on"));
    this.place();
  }

  go(d) {
    const n = this.at + d;
    if (n >= this.steps.length) { this.end(); return; }
    if (n < 0) return;
    this.at = n;
    this.place();
  }

  place() {
    const s = this.steps[this.at], t = document.querySelector(s.sel);
    if (!t) { this.go(1); return; }
    const r = t.getBoundingClientRect(), pad = 6;
    Object.assign(this.hole.style, { left: r.left - pad + "px", top: r.top - pad + "px", width: r.width + 2 * pad + "px", height: r.height + 2 * pad + "px" });
    clear(this.card).append(h("div.tour-n", `${this.at + 1} of ${this.steps.length}`), h("b", s.title), h("p", s.text),
      h("div.tour-nav", h("button.linkbtn", { onclick: () => this.end() }, "Skip the tour"), h("div.grow"),
        this.at ? h("button.btn.sm.ghost", { onclick: () => this.go(-1) }, icon("chevron-left", 13)) : null,
        h("button.btn.sm.primary", { onclick: () => this.go(1) }, this.at === this.steps.length - 1 ? "Done" : "Next", icon(this.at === this.steps.length - 1 ? "check" : "arrow-right", 13))));
    const cw = 320, ch = this.card.offsetHeight || 170, gap = 16, W = innerWidth, H = innerHeight;
    let x, y;
    if (s.side === "right") { x = r.right + gap; y = r.top + 40; }
    else if (s.side === "left") { x = r.left + r.width / 2 - cw / 2; y = r.top + r.height / 2 - ch / 2; }
    else { x = r.left + Math.min(r.width, 400) / 2 - cw / 2; y = r.bottom + gap; }
    x = Math.max(12, Math.min(W - cw - 12, x)); y = Math.max(12, Math.min(H - ch - 12, y));
    Object.assign(this.card.style, { left: x + "px", top: y + "px", width: cw + "px" });
    this.card.classList.remove("in"); void this.card.offsetWidth; this.card.classList.add("in");
  }

  end() {
    removeEventListener("keydown", this.onKey, true);
    removeEventListener("resize", this.onResize);
    if (this.el) { this.el.classList.remove("on"); const el = this.el; setTimeout(() => el.remove(), 200); }
    this.el = null;
    if (this.done) this.done();
  }
}

function visible(sel) {
  const t = document.querySelector(sel);
  if (!t) return false;
  const r = t.getBoundingClientRect();
  return r.width > 20 && r.height > 20;
}
