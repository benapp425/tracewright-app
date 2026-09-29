// The design checks: the verdict, filters, every finding with a way to see it, and a batch of findings
// to add to the review flags or send straight to Claude.
import { h, clear, api, toast, fmtTime, btn } from "./util.js";
import { icon } from "./icons.js";
import { waiveFinding } from "./signoff.js";

const ORDER = ["KiCad", "Schematic", "Parts & BOM", "Placement", "Routing", "Power", "High-speed", "Manufacturing", "Assembly", "Lessons"];
const ST_ICON = { pass: "circle-check", warn: "triangle-alert", fail: "circle-x", error: "bug", skipped: "circle-dot", na: "circle-minus", notrun: "circle", queued: "clock", running: null };
const enc = encodeURIComponent;

export class ChecksPanel {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid; this.open = new Set(); this.running = {}; this.filter = "all"; this.sel = new Map();
    this.box = h("div.panel");
    el.appendChild(this.box);
    ws.ev.on("checks.start", (e) => { this.running = { _all: true, _only: e.only || null, _t: Date.now() }; this.render(); });
    ws.ev.on("checks.progress", (e) => {
      if (!this.running._all) this.running = { _all: true, _t: Date.now() };
      this.running[e.id] = e.status; if (e.status === "running") this.running._cur = { id: e.id, t: Date.now() };
      this.render();
    });
    ws.ev.on("checks.done", (e) => {
      const was = this.running._all; this.running = {}; this.load();
      if (was && e.stopped && !e.error) toast("Checks stopped. Finished checks were updated.", "info", 5000);
    });
    ws.ev.on("checks.updated", () => { if (!Object.keys(this.running).length) this.load(); });
    this.tick = setInterval(() => { if (this.running._cur) { const el = this.box.querySelector(".crow .sti.running + .t .el"); if (el) el.textContent = this.elapsed(); } }, 1000);
    this.load();
  }

  shown() { this.load(); }
  destroy() { clearInterval(this.tick); }

  async load() {
    try { this.data = await api(`/api/projects/${enc(this.pid)}/checks`); } catch (e) { toast(e.message, "error"); return; }
    if (this.data.running && !this.running._all) this.running = { _all: true, _t: Date.now() };
    this.render();
  }

  elapsed() {
    const c = this.running._cur;
    if (!c) return "";
    const s = Math.round((Date.now() - c.t) / 1000);
    return s < 3 ? "" : ` · ${s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s"}`;
  }

  stop() {
    api(`/api/projects/${enc(this.pid)}/checks/stop`, { body: {} }).then(() => toast("Stopping after the current step…", "info", 3000)).catch((e) => toast(e.message, "error"));
  }

  render() {
    const d = this.data;
    if (!d) return;
    const scroll = this.box.scrollTop;
    const box = clear(this.box);
    const inner = h("div.panel-inner" + (this.sel.size ? ".checks-sel" : ""));
    box.appendChild(inner);
    const counts = d.counts || { error: 0, warning: 0, info: 0 };
    const busy = !!this.running._all;
    const cur = busy && this.running._cur ? (d.checks || []).find((c) => c.id === this.running._cur.id) : null;
    const verdict = d.never_run ? "Not checked yet" : counts.error ? "Not ready" : counts.warning ? "Review the warnings" : "All checks pass";
    const vcls = busy ? "" : d.never_run ? "" : counts.error ? "err" : counts.warning ? "warn" : "ok";
    inner.appendChild(h("div.checks-head",
      h("div.verdict", h("div.vic" + (vcls ? "." + vcls : ""), busy ? h("span.spinner.lg") : icon(vcls === "ok" ? "shield-check" : vcls === "err" ? "circle-x" : vcls === "warn" ? "triangle-alert" : "list-checks", 22)),
        h("div.vtext", h("div.vt", busy ? "Running checks…" : verdict),
          h("div.vs", busy ? (cur ? cur.title : "Starting…")
            : d.never_run ? `${(d.checks || []).length} design, fab and assembly checks`
            : `${counts.error} errors · ${counts.warning} warnings · ${counts.info} notes · ${fmtTime(d.generated)}${d.seconds ? ` · ${d.seconds} s` : ""}${d.stopped ? " · stopped early" : ""}`))),
      h("div.grow"),
      h("div.vbtns",
        busy ? btn("circle-stop", "Stop", { onclick: () => this.stop(), "data-tip": "Stop after the current step" }) : null,
        busy ? null : btn("refresh-cw", "Re-export and run", { "data-tip": "Export the netlist, ERC, DRC and plots again", onclick: () => api(`/api/projects/${enc(this.pid)}/checks/run`, { body: { refresh: true } }).catch((e) => toast(e.message, "error")) }),
        busy ? null : btn("play", "Run all checks", { onclick: () => this.ws.runChecks(), "data-kbd": "mod+shift+k", "data-tip": "Run all checks" }, counts.error + counts.warning ? "" : "primary"),
        counts.error + counts.warning && !busy ? btn("wrench", "Fix the findings", { onclick: () => this.ws.ask("Go through the check findings, most important first: fix what is clearly wrong, and for each warning either fix it or tell me why it can be waived.") }, "primary") : null)));
    const checks = d.checks || [];
    const n = (fn) => checks.filter(fn).length;
    const stat = (k, label, count) => h("div.stat" + (this.filter === k ? ".on" : ""), { onclick: () => { this.filter = k; this.render(); } }, h("b", String(count)), label);
    inner.appendChild(h("div.stats", stat("all", "checks", checks.length), stat("fail", "failing", n((c) => c.status === "fail" || c.status === "error")),
      stat("warn", "with warnings", n((c) => c.status === "warn")), stat("pass", "passing", n((c) => c.status === "pass")),
      n((c) => c.status === "skipped") ? stat("skipped", "skipped or n/a", n((c) => c.status === "skipped")) : null,
      n((c) => c.status === "not run") ? stat("notrun", "not run yet", n((c) => c.status === "not run")) : null));
    const keep = (c) => this.filter === "all" || (this.filter === "fail" ? c.status === "fail" || c.status === "error" : this.filter === "notrun" ? c.status === "not run" : c.status === this.filter);
    const groups = {};
    for (const c of checks) if (keep(c)) (groups[c.group] = groups[c.group] || []).push(c);
    const names = ORDER.concat(Object.keys(groups).filter((k) => !ORDER.includes(k)));
    let any = false;
    for (const g of names) {
      if (!groups[g]) continue;
      any = true;
      const bad = groups[g].filter((c) => c.status === "fail" || c.status === "error").length;
      inner.appendChild(h("div.cgroup", h("h4.label", g, bad ? h("span.badge.err", String(bad)) : null), groups[g].map((c) => this.row(c))));
    }
    if (!any) inner.appendChild(h("div.empty", h("div.eicon", icon("list-checks", 20)), h("h3", "No matching checks")));
    if (this.sel.size) inner.appendChild(this.selectionBar());
    this.box.scrollTop = scroll;
  }

  row(c) {
    const run = this.running[c.id];
    const only = this.running._only;
    const inRun = this.running._all && (!only || only.some((o) => o === c.id || o === c.group));
    const st = run || (inRun ? "queued" : c.status);
    const key = st === "not run" ? "notrun" : st === "skipped" && c.na && !run ? "na" : st;
    const nf = (c.findings || []).length;
    const worst = (c.findings || []).some((f) => f.severity === "error") ? "err" : (c.findings || []).some((f) => f.severity === "warning") ? "warn" : "";
    const el = h("div.crow" + (this.open.has(c.id) ? ".open" : ""),
      h("div.ch", { onclick: () => { el.classList.toggle("open"); if (el.classList.contains("open")) this.open.add(c.id); else this.open.delete(c.id); } },
        h("span.sti." + key, key === "running" ? h("span.spinner", { style: { width: "14px", height: "14px" } }) : icon(ST_ICON[key] || "circle", 16)),
        h("span.t.ellipsis", c.title, key === "running" ? h("span.el.muted", this.elapsed()) : c.scope && !inRun ? h("span.scope", c.scope) : null),
        key === "na" ? h("span.badge.soft", "n/a") : key === "notrun" && c.new ? h("span.badge.soft", "new") : null,
        c.waived && !inRun ? h("span.badge.soft", { title: "Findings waived with a reason (tracewright.json checks.waive)" }, `${c.waived} waived`) : null,
        nf && !inRun ? h("span.badge" + (worst ? "." + worst : ""), `${nf} finding${nf === 1 ? "" : "s"}`) : null, h("span.id", c.id), h("span.chev", icon("chevron-right", 14))),
      h("div.cb",
        c.doc ? h("div.doc", tidyDoc(c.doc)) : null,
        c.status === "skipped" ? h("div.small.muted", { style: { margin: "0 4px 8px" } }, (c.na ? "Not applicable: " : "Skipped: ") + (c.reason || "")) : null,
        c.status === "not run" ? h("div.small.muted", { style: { margin: "0 4px 8px" } }, c.new ? "New check. Not run on this project yet." : "Not run yet.") : null,
        !nf && c.status === "pass" ? h("div.small.ok-t", { style: { margin: "0 4px 6px" } }, c.scope ? `Checked ${c.scope}: nothing found.` : "No findings.") : null,
        ...(c.findings || []).slice(0, 300).map((f, i) => this.finding(c, f, i)),
        nf > 300 ? h("div.small.muted", `${nf - 300} more in build/checks_report.md`) : null,
        h("div.row", { style: { margin: "8px 4px 0" } },
          btn("play", "Run this check", { disabled: !!this.running._all, onclick: () => api(`/api/projects/${enc(this.pid)}/checks/run`, { body: { only: [c.id] } }).catch((e) => toast(e.message, "error")) }, "sm"),
          nf ? btn("check-check", "Select all", { onclick: () => { (c.findings || []).forEach((f, i) => this.sel.set(`${c.id}|${i}`, [c, f])); this.render(); } }, "sm ghost") : null)));
    return el;
  }

  finding(c, f, i) {
    const w = f.where || {};
    const key = `${c.id}|${i}`;
    const loc = [w.ref, w.net && w.net.split("/").pop(), w.sheet, (w.x !== undefined && w.x !== null) ? `(${(+w.x).toFixed(1)}, ${(+w.y).toFixed(1)})` : null].filter(Boolean).join(" · ");
    const chk = h("input.fchk", { type: "checkbox", checked: this.sel.has(key), onchange: (e) => { e.target.checked ? this.sel.set(key, [c, f]) : this.sel.delete(key); this.render(); } });
    // Claude's waiver on an error waits for the user: approve it, or reject it (Claude fixes the finding instead)
    const wv = f.waiver ? h("div.fwaiver", icon("shield-check", 12), h("span", h("b", "Claude proposes to waive it: "), f.waiver.reason || "(no reason)"),
      btn("check", "Approve", { onclick: () => this.waiver("approve", f) }, "sm"), btn("x", "Reject", { onclick: () => this.waiver("reject", f) }, "sm ghost")) : null;
    return h("div.finding" + (this.sel.has(key) ? ".sel" : ""), chk,
      h("span.sev." + f.severity),
      h("div.m", f.message, f.hint ? h("div.hint", f.hint) : null, wv),
      loc ? h("span.loc", { "data-tip": "Show on board", onclick: () => this.ws.locate(w) }, icon("target", 12), loc) : null,
      h("div.fa",
        f.waiver ? null : btn("shield-check", null, { "data-tip": "Waive it (with the reason)", onclick: async () => {
          try { if (await waiveFinding(this.pid, f)) toast("Waived. It stops counting when the checks run again.", "ok", 4000, { label: "Run checks", run: () => api(`/api/projects/${enc(this.pid)}/checks/run`, { body: {} }) }); }
          catch (e) { toast(e.message, "error"); }
        } }, "sm ghost"),
        btn("flag", null, { "data-tip": "Add to review", onclick: async () => { await this.ws.review.fromFinding(c, f); toast("Added to review", "ok", 2500, { label: "Show", run: () => this.ws.toggleReview(true) }); } }, "sm ghost")));
  }

  async waiver(action, f) {
    try {
      await api(`/api/projects/${enc(this.pid)}/waivers`, { body: { action, key: f.key } });
      toast(action === "approve" ? "Approved. It stops counting when the checks run again." : "Rejected: Claude is told to fix it instead.", "ok", 4000,
        action === "approve" ? { label: "Run checks", run: () => api(`/api/projects/${enc(this.pid)}/checks/run`, { body: {} }) } : null);
      f.waiver = null; this.render();
    } catch (e) { toast(e.message, "error"); }
  }

  selectionBar() {
    const items = [...this.sel.values()];
    const add = async () => {
      const ids = [];
      for (const [c, f] of items) ids.push((await this.ws.review.fromFinding(c, f)).id);
      this.sel.clear();
      this.render();
      return ids;
    };
    return h("div.selectionbar", icon("check-check", 15, "accent-t"), h("span", `${items.length} finding${items.length === 1 ? "" : "s"} selected`),
      btn("flag", "Add to review", { onclick: async () => { await add(); toast("Added to review", "ok", 2500, { label: "Show", run: () => this.ws.toggleReview(true) }); } }, "sm"),
      btn("send", "Send to Claude", { onclick: async () => { const ids = await add(); await this.ws.review.send(ids, ""); } }, "sm primary"),
      btn("x", null, { "data-tip": "Clear selection", onclick: () => { this.sel.clear(); this.render(); } }, "sm ghost"));
  }
}

// A check's docstring as prose: lines joined, blank lines kept as paragraph breaks.
function tidyDoc(doc) {
  return doc.split(/\n\s*\n/).map((p) => p.split("\n").map((l) => l.trim()).filter(Boolean).join(" ")).filter(Boolean).join("\n\n");
}
