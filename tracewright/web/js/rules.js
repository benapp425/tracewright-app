// The design rules DRC applies: the board constraints next to what the fab can make, the net classes
// and what each of their patterns really reaches, the custom rules file (checked as you type, and by
// KiCad before it is saved: KiCad silently ignores a file with one mistake), the severity of every
// kind of violation, and the width / via presets. Edit, then save: a checkpoint is taken first.
import { h, clear, api, toast, btn, menu, modal, confirmDialog, debounce, fmtTime } from "./util.js";
import { icon } from "./icons.js";

const enc = encodeURIComponent;
const SEV = [["error", "Error"], ["warning", "Warning"], ["ignore", "Ignore"]];
const CLASS_COLS = [["clearance", "Clearance"], ["track_width", "Track"], ["via_diameter", "Via"], ["via_drill", "Via hole"],
  ["diff_pair_width", "Pair width"], ["diff_pair_gap", "Pair gap"]];
const TEMPLATES = [
  ["Wider clearance for a net class", '# Nets of one class keep more space from everything else\n(rule "HV clearance"\n  (condition "A.NetClass == \'HV\'")\n  (constraint clearance (min 0.5mm)))'],
  ["Keep an area clear of copper", '# Nothing on copper inside a rule area named Antenna\n(rule "antenna keepout"\n  (condition "A.intersectsArea(\'Antenna\')")\n  (constraint disallow track via zone))'],
  ["Minimum width for one net", '(rule "5V width"\n  (condition "A.NetName == \'/+5V\'")\n  (constraint track_width (min 0.5mm)))'],
  ["Differential pair geometry", '(rule "USB pair"\n  (condition "A.inDiffPair(\'/USB_D*\')")\n  (constraint diff_pair_gap (min 0.15mm) (max 0.2mm))\n  (constraint diff_pair_uncoupled (max 5mm)))'],
  ["Via size for a net class", '(rule "power vias"\n  (condition "A.NetClass == \'Power\' && A.Type == \'Via\'")\n  (constraint via_diameter (min 0.8mm)))'],
  ["Courtyard spacing", '(rule "courtyards"\n  (constraint courtyard_clearance (min 0.25mm)))'],
  ["Clearance inside one footprint", '(rule "J1 pegs"\n  (condition "A.memberOfFootprint(\'J1\') && B.memberOfFootprint(\'J1\')")\n  (constraint hole_clearance (min 0.15mm)))'],
];

const num = (v) => v === null || v === undefined || v === "" ? "" : String(+(+v).toFixed(4));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export class RulesView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid; this.editing = false;
    this.box = h("div.panel.rulesview");
    el.appendChild(this.box);
    ws.ev.on("rules.changed", () => { if (!this.dirty()) this.load(); });
    ws.ev.on("board.changed", () => { this.stale = true; });
    this.load();
  }

  shown() { if (this.stale && !this.dirty()) this.load(); }
  destroy() {}

  async load() {
    this.stale = false;
    try { this.orig = await api(`/api/projects/${enc(this.pid)}/rules`); }
    catch (e) { clear(this.box).appendChild(h("div.panel-inner", h("div.empty", h("div.eicon", icon("triangle-alert", 20)), h("h3", "The rules could not be read"), h("p", e.message)))); return; }
    this.reset();
  }

  reset() {
    const o = this.orig;
    this.edit = o.empty ? null : {
      constraints: Object.fromEntries(o.constraints.map((c) => [c.key, c.value])),
      classes: o.classes.map((c) => ({ ...c })), patterns: o.patterns.map((p) => ({ pattern: p.pattern, netclass: p.netclass })),
      severities: Object.fromEntries(o.severities.flatMap((g) => g.rows.map((r) => [r.key, r.severity]))),
      dru: o.dru.text, presets: JSON.parse(JSON.stringify(o.presets)),
    };
    this.druCheck = { rules: o.dru.rules, problems: o.dru.problems };
    this.probe = null;
    this.render();
  }

  // what changed, section by section
  changes() {
    const o = this.orig, e = this.edit, out = {};
    if (!e) return out;
    const cons = {};
    for (const c of o.constraints) if (String(e.constraints[c.key]) !== String(c.value) && e.constraints[c.key] !== "") cons[c.key] = +e.constraints[c.key];
    if (Object.keys(cons).length) out.constraints = cons;
    const sev = {};
    for (const g of o.severities) for (const r of g.rows) if (e.severities[r.key] !== r.severity) sev[r.key] = e.severities[r.key];
    if (Object.keys(sev).length) out.severities = sev;
    const strip = (cs) => cs.map((c) => Object.fromEntries(["name", ...CLASS_COLS.map((x) => x[0])].map((k) => [k, c[k] === "" || c[k] === null || c[k] === undefined ? null : +c[k] || (k === "name" ? c[k] : +c[k])])));
    if (!same(strip(e.classes), strip(o.classes))) out.classes = e.classes.map((c) => ({ ...Object.fromEntries(Object.entries(c).filter(([k]) => k === "name" || CLASS_COLS.some((x) => x[0] === k))) }));
    if (!same(e.patterns, o.patterns.map((p) => ({ pattern: p.pattern, netclass: p.netclass })))) out.patterns = e.patterns;
    if (e.dru !== o.dru.text) out.dru = e.dru;
    if (!same(e.presets, o.presets)) out.presets = e.presets;
    return out;
  }

  dirty() { return Object.keys(this.changes()).length > 0; }

  count() {
    const c = this.changes();
    return (Object.keys(c.constraints || {}).length) + (Object.keys(c.severities || {}).length) + (c.classes ? 1 : 0) +
      (c.patterns ? 1 : 0) + (c.dru !== undefined ? 1 : 0) + (c.presets ? 1 : 0);
  }

  // ------------------------------------------------------------------ page
  render() {
    const o = this.orig;
    const scroll = this.box.scrollTop;
    const inner = h("div.panel-inner.rules");
    clear(this.box).appendChild(inner);
    if (o.empty || !this.edit) {
      inner.appendChild(h("div.empty", h("div.eicon", icon("sliders-horizontal", 20)), h("h3", "No board yet"),
        h("p", "Design rules appear here once the board exists.")));
      return;
    }
    const n = this.count();
    this.head = h("div.rules-head",
      h("div.verdict", h("div.vic", icon("sliders-horizontal", 20)), h("div", h("div.vt", "Design rules"),
        h("div.vs", `${o.files.project}${o.files.rules_exists ? " · " + o.files.rules : ""} · ${(o.fab.house || "").toUpperCase()} ${o.fab.layers}-layer limits`))),
      h("div.grow"),
      h("div.vbtns",
        btn("play", "Run DRC", { onclick: () => this.ws.runChecks(["drc", "dfm.rules", "pcb.netclasses"]), "data-tip": "Run DRC with the saved rules" }),
        h("button.btn.revert", { onclick: () => this.reset(), style: { display: n ? "" : "none" } }, "Revert"),
        h("button.btn.primary", { disabled: !n || this.saving, onclick: () => this.save() }, this.saving ? h("span.spinner") : icon("check", 14),
          h("span", this.saving ? "Saving…" : n ? `Save ${n} change${n > 1 ? "s" : ""}` : "Saved"))));
    inner.appendChild(this.head);
    if (o.kicad_open) inner.appendChild(h("div.rules-note.warn", icon("triangle-alert", 15), h("div",
      h("b", "Open in KiCad. "), "Save here, then reopen the board in KiCad without saving there, or KiCad will overwrite these rules.")));
    const nav = h("div.rules-nav", [["cons", "Constraints", "ruler"], ["classes", "Net classes", "cable"], ["custom", "Custom rules", "file-code"],
      ["sev", "Violations", "triangle-alert"], ["presets", "Presets", "layout-grid"]].map(([id, t, ic]) =>
      h("a", { onclick: () => { const s = inner.querySelector("#rules-" + id); if (s) s.scrollIntoView({ behavior: "smooth", block: "start" }); } }, icon(ic, 13), t)));
    inner.appendChild(nav);
    inner.appendChild(this.constraintsCard());
    inner.appendChild(this.classesCard());
    inner.appendChild(this.customCard());
    inner.appendChild(this.severityCard());
    inner.appendChild(this.presetsCard());
    this.box.scrollTop = scroll;
  }

  // the header follows the edits without redrawing the page (a redraw would take the cursor away)
  touch() {
    const n = this.count();
    const b = this.head && this.head.querySelector(".btn.primary");
    if (b && !this.saving) { b.disabled = !n; b.lastChild.textContent = n ? `Save ${n} change${n > 1 ? "s" : ""}` : "Saved"; }
    const rv = this.head && this.head.querySelector(".revert");
    if (rv) rv.style.display = n ? "" : "none";
  }

  card(id, title, sub, body, extra) {
    return h("section.rcard", { id: "rules-" + id }, h("div.rc-head", h("div.grow", h("h3", title), sub ? h("div.rc-sub", sub) : null), extra || null), body);
  }

  // ------------------------------------------------------------------ constraints
  constraintsCard() {
    const o = this.orig, e = this.edit;
    const below = o.constraints.filter((c) => c.fab_min !== null && c.fab_min !== undefined && +e.constraints[c.key] + 1e-9 < c.fab_min);
    const rows = o.constraints.map((c) => {
      const v = e.constraints[c.key];
      const changed = String(v) !== String(c.value);
      const lo = c.fab_min, rec = c.fab_rec;
      const st = lo !== null && lo !== undefined && +v + 1e-9 < lo ? "below" : rec && +v + 1e-9 < rec ? "tight" : "ok";
      const input = h("input.rnum", { type: "number", step: c.kind === "count" ? "1" : "0.005", min: "0", value: num(v),
        oninput: (ev) => { e.constraints[c.key] = ev.target.value; this.touch(); row.classList.toggle("changed", String(ev.target.value) !== String(c.value)); } });
      const row = h("div.rrow" + (changed ? ".changed" : ""),
        h("div.rl", h("div.rt", c.label), c.help ? h("div.rd", c.help) : null),
        h("div.rv", input, h("span.unit", c.kind === "count" ? "" : "mm")),
        h("div.rf", lo !== null && lo !== undefined ? h("span.fabchip." + st, { "data-tip": st === "below" ? "Below the fab's minimum" : st === "tight" ? `Below the recommended ${rec} mm` : "Within fab limits" },
          icon(st === "below" ? "circle-x" : st === "tight" ? "triangle-alert" : "circle-check", 12), `fab ${lo}${rec && rec !== lo ? ` · rec ${rec}` : ""}`) : h("span.muted.small", "")));
      return row;
    });
    return this.card("cons", "Board constraints", "Board Setup › Constraints", h("div.rrows", rows),
      below.length ? btn("arrow-up", `Raise ${below.length} to fab minimum`, { onclick: () => { for (const c of below) e.constraints[c.key] = c.fab_min; this.render(); } }, "sm") : null);
  }

  // ------------------------------------------------------------------ net classes
  classesCard() {
    const o = this.orig, e = this.edit;
    const byName = Object.fromEntries(o.classes.map((c) => [c.name, c]));
    const table = h("div.rtable.classes",
      h("div.rtr.rth", h("div", "Class"), ...CLASS_COLS.map(([, t]) => h("div", t)), h("div", "Nets"), h("div", "")));
    e.classes.forEach((c, i) => {
      const orig = byName[c.name];
      const cell = (k) => h("input.rnum", { type: "number", step: "0.005", min: "0", value: num(c[k]),
        oninput: (ev) => { c[k] = ev.target.value; this.touch(); } });
      const nets = orig ? orig.nets : [];
      table.appendChild(h("div.rtr" + (orig ? "" : ".new"),
        h("div", c.name === "Default" ? h("b", "Default") : h("input.rname", { value: c.name, oninput: (ev) => {
          const old = c.name; c.name = ev.target.value.trim();
          for (const p of e.patterns) if (p.netclass === old) p.netclass = c.name;
          this.touch(); } })),
        ...CLASS_COLS.map(([k]) => h("div", cell(k))),
        h("div", h("span.netcount" + (nets.length ? "" : ".zero"), { "data-tip": nets.length ? nets.slice(0, 30).join(", ") + (nets.length > 30 ? ` ... +${nets.length - 30}` : "") : "No nets get this class" },
          String(nets.length))),
        h("div", c.name === "Default" ? null : btn("trash-2", null, { "data-tip": "Remove this class (its nets go back to Default)", onclick: () => {
          e.classes.splice(i, 1); e.patterns = e.patterns.filter((p) => p.netclass !== c.name); this.render(); } }, "sm ghost"))));
    });
    const add = btn("plus", "Add a net class", { onclick: () => {
      const base = e.classes.find((c) => c.name === "Default") || {};
      let name = "NewClass", k = 2;
      while (e.classes.some((c) => c.name === name)) name = "NewClass" + k++;
      e.classes.push({ ...Object.fromEntries(CLASS_COLS.map(([x]) => [x, base[x]])), name });
      this.render();
    } }, "sm");
    // patterns
    const known = e.classes.map((c) => c.name);
    const opat = Object.fromEntries(o.patterns.map((p) => [p.pattern + "\u0000" + p.netclass, p]));
    const pats = h("div.rtable.patterns", h("div.rtr.rth", h("div", "Net name pattern"), h("div", "Class"), h("div", "Matches"), h("div", "")));
    e.patterns.forEach((p, i) => {
      const was = opat[p.pattern + "\u0000" + p.netclass];
      const sel = h("select", { onchange: (ev) => { p.netclass = ev.target.value; this.touch(); } }, known.map((k) => h("option", { value: k, selected: k === p.netclass }, k)));
      pats.appendChild(h("div.rtr",
        h("div", h("input.rname.mono", { value: p.pattern, placeholder: "*_VBUS or /regex/", oninput: (ev) => { p.pattern = ev.target.value; this.touch(); } })),
        h("div", sel),
        h("div", was ? (was.matches ? h("span.netcount", { "data-tip": was.examples.join(", ") }, `${was.matches} net${was.matches > 1 ? "s" : ""}`)
          : h("span.netcount.zero", { "data-tip": "Matches no net on the board: those nets get the Default rules" }, icon("triangle-alert", 11), "none")) : h("span.muted.small", "save to see")),
        h("div", btn("trash-2", null, { onclick: () => { e.patterns.splice(i, 1); this.render(); } }, "sm ghost"))));
    });
    const addPat = btn("plus", "Add pattern", { onclick: () => { e.patterns.push({ pattern: "", netclass: known[known.length - 1] || "Default" }); this.render(); } }, "sm");
    const unassigned = o.unassigned || [];
    return this.card("classes", "Net classes", "Patterns assign nets to classes.",
      h("div", table, h("div.row.rmore", add),
        h("h4.rsub", "Patterns", h("span.muted.small", "  First match wins · * and ? or /regex/")), pats, h("div.row.rmore", addPat),
        unassigned.length ? h("div.rd.rpad", h("b", `${unassigned.length}${unassigned.length >= 40 ? "+" : ""} nets on Default: `), unassigned.slice(0, 24).join(", "), unassigned.length > 24 ? " ..." : "") : null));
  }

  // ------------------------------------------------------------------ custom rules
  customCard() {
    const e = this.edit;
    const probs = (this.druCheck.problems || []);
    const errs = probs.filter((p) => p.severity === "error");
    const status = !e.dru.trim() ? h("span.badge", "No custom rules")
      : errs.length ? h("span.badge.err", icon("circle-x", 12), `${errs.length} error${errs.length > 1 ? "s" : ""}: KiCad would ignore this file`)
      : this.probe ? (this.probe.ok ? h("span.badge.ok", icon("shield-check", 12), "Verified in KiCad") : h("span.badge.err", icon("circle-x", 12), this.probe.why))
      : h("span.badge.ok", icon("circle-check", 12), `${(this.druCheck.rules || []).length} rules · no problems`);
    const list = h("div.drulist", (this.druCheck.rules || []).map((r) => h("div.drule",
      h("div.drh", h("b", r.name), r.severity ? h("span.badge.soft", r.severity) : null, r.layer ? h("span.badge.soft", r.layer) : null,
        h("div.grow"), h("span.muted.small", `line ${r.line}`)),
      r.condition ? h("code.dcond", r.condition) : h("span.muted.small", "All items"),
      h("div.dcons", r.constraints.map((c) => h("span.dchip", h("b", c.type.replace(/_/g, " ")),
        c.items ? " " + c.items.join(", ") : "", c.min ? ` ≥ ${c.min}` : "", c.max ? ` ≤ ${c.max}` : "", c.opt ? ` ~${c.opt}` : "", c.value ? " " + c.value : "", c.expr ? " " + c.expr : ""))))));
    const editor = this.editor();
    const tools = h("div.row.rmore",
      h("button.btn.sm", { onclick: (ev) => menu(ev.currentTarget, TEMPLATES.map(([t, body]) => ({ label: t, icon: "plus", run: () => {
        e.dru = (e.dru.trim() ? e.dru.trimEnd() + "\n" : "(version 1)\n") + body + "\n"; this.editing = true; this.recheck(); this.render(); } }))) }, icon("plus", 13), h("span", "Add rule"), icon("chevron-down", 12)),
      h("button.btn.sm", { onclick: () => { this.editing = !this.editing; this.render(); } }, icon(this.editing ? "list" : "file-code", 13), h("span", this.editing ? "Show rules" : "Edit file")),
      e.dru.trim() ? h("button.btn.sm", { disabled: this.probing, onclick: () => this.probeNow(), "data-tip": "Load the rules in KiCad to verify them" },
        this.probing ? h("span.spinner") : icon("shield-check", 13), h("span", this.probing ? "Verifying…" : "Verify in KiCad")) : null,
      h("div.grow"), status);
    return this.card("custom", "Custom rules", "The .kicad_dru file. Every edit is checked.",
      h("div", tools, this.editing ? editor : list, probs.length ? h("div.dprobs", probs.map((p) => h("div.dprob." + p.severity, { onclick: () => this.gotoLine(p.line) },
        icon(p.severity === "error" ? "circle-x" : "triangle-alert", 13), h("span.ln", `line ${p.line}`), h("span", p.message)))) : null));
  }

  editor() {
    const e = this.edit;
    const gutter = h("div.dgutter");
    const ta = h("textarea.dcode", { spellcheck: false, value: e.dru, placeholder: "(version 1)\n(rule \"name\"\n  (condition \"A.NetClass == 'Power'\")\n  (constraint clearance (min 0.3mm)))",
      oninput: () => { e.dru = ta.value; lines(); this.touch(); this.recheckSoon(); },
      onscroll: () => { gutter.scrollTop = ta.scrollTop; },
      onkeydown: (ev) => { if (ev.key === "Tab") { ev.preventDefault(); const s = ta.selectionStart; ta.setRangeText("  ", s, ta.selectionEnd, "end"); ta.dispatchEvent(new Event("input")); } } });
    const bad = new Set((this.druCheck.problems || []).filter((p) => p.severity === "error").map((p) => p.line));
    const lines = () => { const n = ta.value.split("\n").length; clear(gutter).append(...Array.from({ length: n }, (_, i) => h("div" + (bad.has(i + 1) ? ".bad" : ""), String(i + 1)))); };
    lines();
    this.ta = ta;
    return h("div.deditor", gutter, ta);
  }

  gotoLine(n) {
    if (!this.editing) { this.editing = true; this.render(); }
    const ta = this.ta;
    if (!ta) return;
    const pos = ta.value.split("\n").slice(0, n - 1).join("\n").length + (n > 1 ? 1 : 0);
    ta.focus(); ta.setSelectionRange(pos, pos + (ta.value.split("\n")[n - 1] || "").length);
  }

  recheckSoon() { clearTimeout(this.rt); this.rt = setTimeout(() => this.recheck(true), 350); }

  async recheck(keepEditor) {
    try {
      this.druCheck = await api(`/api/projects/${enc(this.pid)}/rules/check`, { body: { dru: this.edit.dru } });
      this.probe = null;
      if (keepEditor && this.editing && this.ta) {
        const pos = [this.ta.selectionStart, this.ta.selectionEnd], st = this.ta.scrollTop;
        const card = this.box.querySelector("#rules-custom");
        const fresh = this.customCard();
        card.replaceWith(fresh);
        this.ta.focus(); this.ta.setSelectionRange(...pos); this.ta.scrollTop = st;
      } else this.render();
    } catch (err) { /* offline: keep the last result */ }
  }

  async probeNow() {
    this.probing = true; this.render();
    try { this.probe = await api(`/api/projects/${enc(this.pid)}/rules/probe`, { body: { dru: this.edit.dru } }); }
    catch (e) { toast(e.message, "error"); }
    this.probing = false; this.render();
  }

  // ------------------------------------------------------------------ severities
  severityCard() {
    const o = this.orig, e = this.edit;
    const groups = o.severities.map((g) => h("div.sevgroup", h("h4.rsub", g.title), h("div.sevrows", g.rows.map((r) => {
      const seg = h("div.seg.tiny", SEV.map(([k, t]) => h("button." + k + (e.severities[r.key] === k ? ".on" : ""), { onclick: () => {
        e.severities[r.key] = k; for (const b of seg.children) b.classList.toggle("on", b.classList.contains(k)); row.classList.toggle("changed", k !== r.severity); this.touch(); } }, t)));
      const row = h("div.sevrow" + (e.severities[r.key] !== r.severity ? ".changed" : ""), h("span.grow", r.label, h("span.id", r.key)), seg);
      return row;
    }))));
    return this.card("sev", "Violation severities", "Ignored violations are not checked.", h("div.sevgrid", groups));
  }

  // ------------------------------------------------------------------ presets
  presetsCard() {
    const e = this.edit;
    const tw = h("div.chips", e.presets.track_widths.map((w, i) => h("span.pchip", `${num(w)} mm`, h("button", { onclick: () => { e.presets.track_widths.splice(i, 1); this.render(); } }, icon("x", 10)))),
      h("input.rnum.small", { type: "number", step: "0.05", placeholder: "Add mm", onkeydown: (ev) => { if (ev.key === "Enter" && +ev.target.value > 0) { e.presets.track_widths.push(+ev.target.value); e.presets.track_widths.sort((a, b) => a - b); this.render(); } } }));
    const vias = h("div.chips", e.presets.via_dimensions.map((v, i) => h("span.pchip", `${num(v.diameter)} / ${num(v.drill)} mm`, h("button", { onclick: () => { e.presets.via_dimensions.splice(i, 1); this.render(); } }, icon("x", 10)))),
      h("input.rnum.small", { placeholder: "0.6/0.3", onkeydown: (ev) => { const m = /^\s*([\d.]+)\s*\/\s*([\d.]+)\s*$/.exec(ev.target.value); if (ev.key === "Enter" && m) { e.presets.via_dimensions.push({ diameter: +m[1], drill: +m[2] }); this.render(); } } }));
    const dps = h("div.chips", e.presets.diff_pair_dimensions.map((d, i) => h("span.pchip", `${num(d.width)} / ${num(d.gap)} mm`, h("button", { onclick: () => { e.presets.diff_pair_dimensions.splice(i, 1); this.render(); } }, icon("x", 10)))),
      h("input.rnum.small", { placeholder: "0.2/0.15", onkeydown: (ev) => { const m = /^\s*([\d.]+)\s*\/\s*([\d.]+)\s*$/.exec(ev.target.value); if (ev.key === "Enter" && m) { e.presets.diff_pair_dimensions.push({ width: +m[1], gap: +m[2], via_gap: +m[2] }); this.render(); } } }));
    return this.card("presets", "Presets", "Sizes offered while routing. Press Enter to add.",
      h("div.rrows", h("div.rrow", h("div.rl", h("div.rt", "Track widths")), h("div.rv.wide", tw)),
        h("div.rrow", h("div.rl", h("div.rt", "Vias"), h("div.rd", "Diameter / hole")), h("div.rv.wide", vias)),
        h("div.rrow", h("div.rl", h("div.rt", "Differential pairs"), h("div.rd", "Width / gap")), h("div.rv.wide", dps))));
  }

  // ------------------------------------------------------------------ save
  async save() {
    const body = this.changes();
    if (!Object.keys(body).length) return;
    if (this.orig.kicad_open && !await confirmDialog({ title: "The board is open in KiCad", text: "After saving, reopen the board in KiCad without saving there, or KiCad will overwrite these rules.", ok: "Save rules" })) return;
    this.saving = true; this.render();
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/rules`, { method: "PUT", body });
      this.orig = r.state;
      this.saving = false;
      this.reset();
      toast(r.warnings && r.warnings.length ? `Saved with ${r.warnings.length} note${r.warnings.length > 1 ? "s" : ""}: ${r.warnings[0]}` : "Rules saved",
        r.warnings && r.warnings.length ? "warn" : "ok", 7000, { label: "Run DRC", run: () => this.ws.runChecks(["drc", "dfm.rules", "pcb.netclasses"]) });
    } catch (e) {
      this.saving = false; this.render();
      const errs = (e.data && e.data.errors) || [e.message];
      const m = modal({ title: "Rules not saved", icon: "circle-x", body: [h("p", "Fix these first:"), h("ul", errs.map((x) => h("li", x)))],
        actions: [h("button.btn.primary", { onclick: () => m.close() }, "OK")] });
    }
  }
}
