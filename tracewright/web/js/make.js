// Parts > Make: getting the board built. The BOM's health (can every line be bought for the boards being ordered), the
// test points a person or fixture needs, the fab and assembly drawings, a V-scored panel, whether the board fits its
// enclosure (and an OpenSCAD box to start from), and the block library: circuits built once, used again.
import { h, clear, api, toast, confirmDialog } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";

const enc = encodeURIComponent;
const SEV = { error: "circle-x", warning: "triangle-alert", info: "info" };
const cap = (l) => l ? l[0].toUpperCase() + l.slice(1) : l;

export class MakeView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.box = h("div.panel.makeview");
    el.appendChild(this.box);
    this.out = {};
    ws.ev.on("board.changed", () => { this.stale = true; });
    ws.ev.on("schematic.changed", () => { this.stale = true; });
    this.load();
  }

  shown() { if (this.stale) { this.stale = false; this.load(); } }
  destroy() {}

  async load() {
    try { this.d = await api(`/api/projects/${enc(this.pid)}/make`); } catch (e) { this.d = { error: e.message }; }
    this.render();
  }

  url(path, download) { return `/api/projects/${enc(this.pid)}/file?path=${enc(path)}&raw=1${download ? "&download=1" : ""}`; }

  fileRow(name, f) {
    return h("div.mk-file", icon(name.endsWith(".pdf") ? "file-text" : name.endsWith(".zip") ? "package" : name.endsWith(".svg") ? "image" : "file-code", 13),
      h("a", { href: this.url(f.path), target: "_blank" }, name), h("span.mk-at", f.at ? f.at.replace("T", " ").slice(0, 16) : ""),
      isNative ? h("button.tbtn", { "data-tip": "Open it", onclick: () => native.openPath(this.ws.p.root + "/" + f.path) }, icon("external-link", 12)) : null,
      h("button.tbtn", { "data-tip": "Download", onclick: () => isNative ? native.save(this.url(f.path, true)) : window.open(this.url(f.path, true)) }, icon("download", 12)));
  }

  async run(kind, body, b) {
    if (b) { b.disabled = true; b.classList.add("busy"); }
    try { this.out[kind] = await api(`/api/projects/${enc(this.pid)}/make/${kind}`, { body }); await this.load(); }
    catch (e) { toast(e.message, "error", 6000); }
    if (b) { b.disabled = false; b.classList.remove("busy"); }
  }

  section(id, ic, title, sub) {
    const s = h("section.mk-sec", { "data-sec": id }, h("div.mk-h", icon(ic, 15), h("h3", title), sub ? h("span.mk-sub", sub) : null));
    this.box.appendChild(s);
    return s;
  }

  render() {
    const D = this.d || {}, box = clear(this.box);
    if (D.error) { box.appendChild(h("div.empty", h("h3", "Could not read the project"), h("p", D.error))); return; }
    this.bomSec(D);
    if (D.board) { this.tpSec(D); this.drawSec(D); this.panelSec(D); this.encSec(D); }
    else box.appendChild(h("div.pl-empty.mk-wait", "Test points, drawings, panels and the enclosure come once there is a board."));
    this.blockSec(D);
  }

  // ---------------------------------------------------------------- the BOM's health
  bomSec(D) {
    const B = D.bom;
    const s = this.section("bom", "list-checks", "BOM health", B && B.score != null ? `for ${B.boards} board${B.boards === 1 ? "" : "s"}` : "");
    if (!B) { s.appendChild(h("div.pl-empty", "No schematic yet.")); return; }
    s.appendChild(h("div.mk-score", h("b." + (B.score >= 90 ? "ok" : B.score >= 70 ? "warn" : "bad"), B.score == null ? "–" : String(B.score)),
      h("div.mk-lines", B.lines.map((l) => h("div", cap(l))))));
    const bad = (B.rows || []).filter((r) => r.flags.some((f) => f.sev !== "info"));
    if (bad.length) s.appendChild(h("table.sm-table", h("tr", h("th", "Parts"), h("th", "Value"), h("th", "What")),
      bad.slice(0, 20).map((r) => h("tr", h("td", r.refs.slice(0, 5).join(", ") + (r.refs.length > 5 ? " …" : "")), h("td", r.value),
        h("td", r.flags.filter((f) => f.sev !== "info").map((f) => h("div.mk-flag." + f.sev, icon(SEV[f.sev], 11), f.text)))))));
  }

  // ---------------------------------------------------------------- test points
  tpSec(D) {
    const T = D.testpoints;
    const s = this.section("tp", "crosshair", "Test points", "what a person or a fixture needs to touch");
    if (!T) { s.appendChild(h("div.pl-empty", "Needs the schematic and the board.")); return; }
    s.appendChild(h("div.sm-chips", [["B", "Bottom"], ["F", "Top"]].map(([k, t]) => h("button" + (T.side === k ? ".on" : ""),
      { onclick: (e) => this.run("testpoints", { side: k }, e.currentTarget) }, t))));
    s.appendChild(h("table.sm-table", h("tr", h("th", "Net"), h("th", "Where"), h("th", "Why")),
      T.items.map((i) => h("tr", h("td", i.net), h("td", i.at.kind === "add" ? `add one at (${i.at.x}, ${i.at.y}), ${i.at.side === "B" ? "bottom" : "top"}${i.at.via ? ", with a via" : ""}`
        : i.at.kind === "none" ? "no room beside it: route a stub out" : `${i.at.kind} ${i.at.ref} pin ${i.at.pin}`), h("td.sm-how", i.why)))));
    for (const c of T.close || []) s.appendChild(h("div.mk-flag.warning", icon("triangle-alert", 11), `${c[0]} and ${c[1]}: ${c[2]} mm apart (a fixture wants ${T.pitch} mm)`));
    if ((T.add || []).length) s.appendChild(h("div.pl-add", h("button.btn.sm", { onclick: () => this.ws.ask(
      "Add the test points the plan in Parts > Make lists (" + T.add.map((i) => `${i.net} at (${i.at.x}, ${i.at.y}) ${i.at.side === "B" ? "bottom" : "top"}`).join("; ") +
      "): a TestPoint symbol on each net in the schematic, then place each footprint there" + (T.add.some((i) => i.at.via) ? ", with its via" : "") + ".") }, icon("plus", 12), "Add them to the design")));
  }

  // ---------------------------------------------------------------- drawings
  drawSec(D) {
    const s = this.section("draw", "file-text", "Drawings", "for the fab and the assembler");
    s.appendChild(h("div.pl-add",
      h("button.btn.sm.primary", { onclick: (e) => this.run("drawings", { which: "fab" }, e.currentTarget) }, icon("file-text", 12), "Fab drawing"),
      h("button.btn.sm", { onclick: (e) => this.run("drawings", { which: "assembly" }, e.currentTarget) }, icon("layers", 12), "Assembly drawings")));
    const files = Object.entries(D.files || {}).filter(([n]) => n.includes("fab-drawing") || n.includes("assembly-"));
    for (const [n, f] of files) s.appendChild(this.fileRow(n, f));
    const o = this.out.drawings;
    if (o && o.notes) s.appendChild(h("details.mk-notes", h("summary", "The fab notes"), h("ol", o.notes.map((l) => h("li", l)))));
  }

  // ---------------------------------------------------------------- panel
  panelSec(D) {
    const s = this.section("panel", "layout-grid", "Panel", "V-scored copies with rails, tooling holes and fiducials");
    const v = this.pv || (this.pv = { nx: 2, ny: 2, rail: 5, rails: "tb" });
    const num = (k, w = 50) => { const i = h("input", { value: v[k], style: { width: w + "px" } }); i.addEventListener("input", () => { v[k] = +i.value || v[k]; }); return i; };
    const rails = h("select", [["tb", "Top and bottom"], ["lr", "Left and right"], ["all", "All round"], ["none", "None"]].map(([k, t]) => h("option", { value: k, selected: v.rails === k }, t)));
    rails.addEventListener("change", () => { v.rails = rails.value; });
    s.appendChild(h("div.sm-ctl", h("label.sm-f", h("span", "Across"), num("nx")), h("label.sm-f", h("span", "Down"), num("ny")),
      h("label.sm-f", h("span", "Rails (mm)"), num("rail")), h("label.sm-f", h("span", "Rails on"), rails),
      h("button.btn.sm.primary", { onclick: (e) => this.run("panel", { nx: v.nx, ny: v.ny, rail: v.rail, rails: v.rails }, e.currentTarget) }, icon("layout-grid", 12), "Make the panel")));
    const o = this.out.panel;
    if (o) s.appendChild(h("div.sm-lines", o.lines.map((l) => h("div", cap(l)))));
    const files = Object.entries(D.files || {}).filter(([n, f]) => f.path.startsWith("build/panel/"));
    const svg = files.find(([n]) => n.endsWith(".svg"));
    if (svg) s.appendChild(h("img.mk-panel", { src: this.url(svg[1].path) + "&t=" + enc(svg[1].at || ""), alt: "the panel" }));
    for (const [n, f] of files) if (!n.endsWith(".svg")) s.appendChild(this.fileRow(n, f));
  }

  // ---------------------------------------------------------------- enclosure
  encSec(D) {
    const s = this.section("enc", "box", "Enclosure", "the board and its parts against the box's inside");
    const e = this.ev || (this.ev = { ...(D.enclosure || { w: "", l: "", h: "", standoff: 5 }), step: false });
    const num = (k, ph) => { const i = h("input", { value: e[k] ?? "", placeholder: ph, style: { width: "64px" } }); i.addEventListener("input", () => { e[k] = i.value; }); return i; };
    const step = h("input", { type: "checkbox", checked: e.step }); step.addEventListener("change", () => { e.step = step.checked; });
    s.appendChild(h("div.sm-ctl", h("label.sm-f", h("span", "Width (mm)"), num("w", "60")), h("label.sm-f", h("span", "Length"), num("l", "45")),
      h("label.sm-f", h("span", "Height"), num("h", "20")), h("label.sm-f", h("span", "Standoffs"), num("standoff", "5")),
      h("label.dn-opt", step, "and the board's STEP"),
      h("button.btn.sm.primary", { onclick: (b) => this.run("enclosure", { w: +e.w, l: +e.l, h: +e.h, standoff: +e.standoff || 5, step: e.step }, b.currentTarget) }, icon("box", 12), "Check the fit")));
    const o = this.out.enclosure;
    if (o) s.appendChild(h("div.sm-lines" + (o.ok ? "" : ".bad"), o.lines.map((l) => h("div", cap(l)))));
    for (const [n, f] of Object.entries(D.files || {}).filter(([n, f]) => f.path.startsWith("build/enclosure/") || n.endsWith(".step"))) s.appendChild(this.fileRow(n, f));
  }

  // ---------------------------------------------------------------- blocks
  blockSec(D) {
    const s = this.section("blocks", "package", "Blocks", "circuits built once, used again");
    const sel = this.ws.views && this.ws.views.schematic && this.ws.views.schematic.sel ? [...this.ws.views.schematic.sel] : [];
    if (D.schematic) {
      const name = h("input", { placeholder: "3.3 V buck", style: { width: "150px" } });
      const refs = h("input", { value: sel.join(", "), placeholder: "U3, L1, C5, C6, R7", style: { width: "220px" } });
      const desc = h("input", { placeholder: "what it is for (optional)", style: { width: "220px" } });
      s.appendChild(h("div.sm-ctl", h("label.sm-f", h("span", "Name"), name), h("label.sm-f", h("span", "Parts (pick them in the schematic)"), refs),
        h("label.sm-f", h("span", "Note"), desc), h("button.btn.sm", { onclick: async (e) => {
          const b = e.currentTarget; b.disabled = true;
          try {
            const r = await api(`/api/projects/${enc(this.pid)}/blocks`, { body: { name: name.value, description: desc.value, refs: refs.value.split(/[\s,]+/).filter(Boolean) } });
            toast(`Saved: ${r.parts} parts, ports ${r.ports.join(", ")}`, "ok", 4000); this.load();
          } catch (err) { toast(err.message, "error", 5000); }
          b.disabled = false;
        } }, icon("save", 12), "Save as a block")));
    }
    const items = D.blocks || [];
    if (!items.length) { s.appendChild(h("div.pl-empty", "No blocks yet: save a circuit that works, and use it in the next design.")); return; }
    for (const b of items) s.appendChild(h("div.mk-block", { "data-block": b.id },
      h("div.grow", h("b", b.name), h("div.mk-sub", `${b.parts} parts · ports ${b.ports.join(", ")}${b.from ? ` · from ${b.from.project}` : ""}`),
        b.description ? h("div.mk-desc", b.description) : null),
      D.schematic ? h("button.btn.sm", { onclick: () => this.ws.ask(`Use the block "${b.name}" (${b.id}) from my block library in this design: read it (the blocks tool), draw its parts and connections into the schematic with its ports on this design's nets, keep its notes, and place its parts as it says.`) }, "Use in this design") : null,
      h("button.tbtn", { "data-tip": "Remove it from the library", onclick: async () => {
        if (!(await confirmDialog({ title: `Remove ${b.name}?`, text: "It goes from the library; designs that used it keep their parts.", ok: "Remove", danger: true }))) return;
        try { await api(`/api/blocks/${enc(b.id)}`, { method: "DELETE" }); this.load(); } catch (e) { toast(e.message, "error"); }
      } }, icon("trash-2", 12))));
  }
}
