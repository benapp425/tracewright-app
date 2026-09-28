// The Order panel (top of the Order tab): how the board gets built -- JLC turnkey or by you -- whether
// the order is ready, the board as the fab sees it, a rough price, and the ways to the fab: PCBWay in
// one click (the upload its own KiCad plugin makes), JLC's quote page with the package ready, and the
// parts as DigiKey / Mouser / LCSC BOM files for self-assembly.
import { h, clear, api, toast, btn, modal, confirmDialog, copyText } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";
import { markdown } from "./markdown.js";
import { state } from "./app.js";

const enc = encodeURIComponent;

export class OrderPanel {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.qty = Number(localStorage.getItem("tw.order.qty") || 5);
    this.boards = Number(localStorage.getItem("tw.order.boards") || 3);
    this.el = h("section.order");
    this.logEl = h("div.or-log");
    ws.ev.on("order.progress", (e) => { this.logEl.textContent = e.text; this.logEl.classList.add("on"); });
    ws.ev.on("checks.done", () => this.load());
    ws.ev.on("bom.changed", () => this.load());
    this.load();
  }

  async load() {
    try { this.d = await api(`/api/projects/${enc(this.pid)}/order?qty=${this.qty}`); }
    catch (e) { this.d = null; clear(this.el).appendChild(h("div.small.muted", e.message)); return; }
    this.render();
  }

  async setMode(m) {
    if (this.d && this.d.mode === m) return;
    try { this.d = await api(`/api/projects/${enc(this.pid)}/order/mode`, { method: "PUT", body: { mode: m, qty: this.qty } }); this.render();
      toast(m === "jlc" ? "Switched to JLC turnkey" : "Switched to self-assembly", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }

  render() {
    const d = this.d, el = clear(this.el);
    if (!d) return;
    const m = d.mode;
    el.append(h("div.or-head", h("div.or-hic", icon("shopping-cart", 18)), h("div.grow", h("h2", "Order"))));
    el.appendChild(h("div.or-modes", Object.entries(d.modes).map(([k, v]) => h("button.or-mode" + (k === m ? ".on" : ""), { onclick: () => this.setMode(k) },
      h("div.or-mt", icon(v.icon, 16), h("b", v.title), k === m ? h("span.or-cur", icon("check", 11), "Selected") : null), h("div.or-md", v.text)))));
    if (!d.has_pcb) { el.appendChild(h("div.small.muted", { style: { marginTop: "10px" } }, "Available once the board exists.")); return; }
    const blockers = d.readiness.filter((r) => r.blocker && r.ok === false);
    const ready = h("div.or-card", h("div.label", "Readiness"), d.readiness.map((r) => h("div.or-rd" + (r.ok === true ? ".ok" : r.ok === false ? (r.blocker ? ".bad" : ".warn") : ".unk"),
      h("span.or-ri", icon(r.ok === true ? "check" : r.ok === false ? (r.blocker ? "x" : "triangle-alert") : "info", 12)),
      h("div.grow", h("div", r.title), r.detail ? h("div.or-rdd", r.detail) : null),
      r.fix && r.ok !== true ? h("button.linkbtn", { onclick: () => this.ws.show(r.fix) }, r.fix === "checks" ? "Checks" : "BOM") : null)));
    const s = d.specs;
    const spec = h("div.or-card", h("div.label", "Board specs"), h("div.or-specs",
      row("Size", `${s.w} × ${s.h} mm`), row("Layers", s.layers), row("Thickness", `${s.thickness} mm`), row("Copper", `${s.copper_oz} oz outer`),
      row("Finish", s.finish), row("Min track / drill", `${s.min_track ?? "-"} / ${s.min_drill ?? "-"} mm`),
      row("Parts", `${s.parts}${s.smd_sides.length > 1 ? ", SMD on both sides" : s.smd_sides.length ? ", SMD on the " + (s.smd_sides[0] === "F" ? "top" : "bottom") : ""}`)));
    const est = d.estimate;
    const qty = h("select.or-qty", { onchange: (e) => { this.qty = Number(e.target.value); localStorage.setItem("tw.order.qty", this.qty); this.load(); } },
      [5, 10, 15, 20, 30, 50, 100].map((n) => h("option", { value: n, selected: n === this.qty }, `${n} boards`)));
    const price = h("div.or-card", h("div.row", h("div.label.grow", "Estimate"), qty),
      h("div.or-lines", est.lines.map(([t, v]) => h("div.or-line", h("span", t), h("b", v ? `$${Number(v).toFixed(2)}` : "-")))),
      h("div.or-total", h("span", "Total"), h("b", `$${est.total.toFixed(2)}`)), h("div.or-note", est.note));
    el.appendChild(h("div.or-grid", ready, spec, price));
    const acts = h("div.or-acts");
    if (m === "jlc") {
      acts.append(h("div.or-agroup", h("div.or-at", "Assembled boards"),
        h("div.row.wrap", btn("external-link", "Order from JLCPCB", { onclick: () => this.jlc(), "data-tip": "Opens JLC's quote page with your files" }, "primary"),
          btn("zap", "Upload to PCBWay", { onclick: () => this.pcbway(), "data-tip": "Sends your files to PCBWay for a quote" }))));
    } else {
      const boards = h("input.or-boards", { type: "number", min: 1, max: 500, value: this.boards, onchange: (e) => { this.boards = Math.max(1, Number(e.target.value) || 1); localStorage.setItem("tw.order.boards", this.boards); } });
      acts.append(h("div.or-agroup", h("div.or-at", "Boards and stencil"),
          h("div.row.wrap", btn("external-link", "Order from JLCPCB", { onclick: () => this.jlc(), "data-tip": "Opens JLC's quote page with your files" }, "primary"),
            btn("zap", "Upload to PCBWay", { onclick: () => this.pcbway(), "data-tip": "Sends your files to PCBWay for a quote" }))),
        h("div.or-agroup", h("div.or-at", "Parts"), h("div.row.wrap", h("label.or-bl", "For", boards, "boards, plus spares"),
          btn("shopping-cart", "DigiKey", { onclick: () => this.parts("digikey") }), btn("shopping-cart", "Mouser", { onclick: () => this.parts("mouser") }),
          btn("shopping-cart", "LCSC", { onclick: () => this.parts("lcsc") }))));
    }
    if (blockers.length) acts.appendChild(h("div.notice.err", icon("circle-alert", 15), h("div.nb", h("b", "Not ready: "), blockers.map((b) => b.title).join("; ") + ". You can still get a quote.")));
    acts.appendChild(this.logEl);
    el.appendChild(acts);
    const files = Object.entries(d.files || {}).flatMap(([t, fs]) => fs.map((f) => ({ ...f, t })));
    if (files.length) el.appendChild(h("details.or-files", h("summary", `Order files (${files.length})`),
      files.map((f) => h("div.outfile", h("div.oic", icon(f.name.endsWith(".zip") ? "archive" : f.name.endsWith(".md") ? "file-text" : "table-2", 14)), h("span.n", `${f.t}/${f.name}`),
        btn("download", null, { "data-tip": "Download", onclick: () => native.save(`/api/projects/${enc(this.pid)}/file?raw=1&path=${enc(f.path)}`) }, "sm ghost")))));
  }

  busy(on, text) {
    this.el.classList.toggle("working", on);
    for (const b of this.el.querySelectorAll(".or-acts button")) b.disabled = on;
    if (on) { this.logEl.textContent = text || "Working…"; this.logEl.classList.add("on"); }
  }

  reveal(rel, abs) {
    if (isNative) native.reveal(abs);
    else if (!(state.info && state.info.server_mode)) api(`/api/projects/${enc(this.pid)}/reveal`, { body: { path: rel } }).catch(() => {});
  }

  async jlc() {
    this.busy(true, "Preparing the JLC package…");
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/order/jlc`, { body: { qty: this.qty } });
      native.openURL(r.url);
      this.reveal(r.dir_rel + "/ORDER-SHEET.md", r.dir);
      this.sheet(r);
      this.load();
    } catch (e) { toast(e.message, "error"); }
    finally { this.busy(false); }
  }

  sheet(r) {
    const files = h("div.or-sfiles", r.files.filter((f) => !f.endsWith(".md")).map((f) => h("div.outfile", h("div.oic", icon(f.endsWith(".zip") ? "archive" : "table-2", 14)), h("span.n", f),
      btn("download", null, { "data-tip": "Download", onclick: () => native.save(`/api/projects/${enc(this.pid)}/file?raw=1&path=${enc(r.dir_rel + "/" + f)}`) }, "sm ghost"))));
    const m = modal({ title: "JLCPCB quote page opened", icon: "external-link", cls: "wide",
      sub: isNative ? "Drag these files onto the page, then match the settings below." : "Download these files, add them on the page, then match the settings below.",
      body: [files, h("div.md.or-sheet", { html: markdown(r.sheet) })],
      actions: [btn("copy", "Copy settings", { onclick: () => { copyText(r.sheet); toast("Copied", "ok"); } }), btn("external-link", "Reopen page", { onclick: () => native.openURL(r.url) }),
        h("button.btn.primary", { onclick: () => m.close() }, "Done")] });
  }

  async pcbway() {
    const ok = await confirmDialog({ title: "Upload to PCBWay?", ok: "Upload", icon: "zap",
      text: "Sends the Gerbers, drill files, netlist, BOM and positions to PCBWay for a quote. Nothing is ordered until you check out." });
    if (!ok) return;
    this.busy(true, "Uploading to PCBWay…");
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/order/pcbway`, { body: {} });
      native.openURL(r.url);
      toast("PCBWay quote opened in your browser", "ok");
      this.load();
    } catch (e) { toast(e.message, "error", 9000); }
    finally { this.busy(false); }
  }

  async parts(vendor) {
    this.busy(true, "Preparing parts lists…");
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/order/parts`, { body: { boards: this.boards } });
      const f = r.files[vendor];
      native.openURL(r.links[vendor]);
      if (isNative) native.reveal(r.dir + "/" + f); else native.save(`/api/projects/${enc(this.pid)}/file?raw=1&path=${enc(r.dir_rel + "/" + f)}`);
      toast(`${vendor === "digikey" ? "DigiKey" : vendor === "mouser" ? "Mouser" : "LCSC"} BOM tool opened. Upload ${f}.` + (r.missing_mpn.length ? ` ${r.missing_mpn.length} lines without an MPN were skipped.` : ""), "ok", 8000);
      this.load();
    } catch (e) { toast(e.message, "error"); }
    finally { this.busy(false); }
  }
}

function row(k, v) { return h("div.or-sp", h("span", k), h("b", String(v))); }
