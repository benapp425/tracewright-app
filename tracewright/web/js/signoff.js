// Sign-off: the last look before ordering. It opens with the verdict and what is left to do (each with its button),
// then every requirement with what shows it is met, the waivers as cards (a plain title, the reasoning, where it is
// shown; Claude's waivers on errors wait here for your approval), what only the built board can show, the stages and
// how the run went. The review packet is the same page, printable. See signoff.py.
import { h, clear, api, toast, btn, fmtTime, confirmDialog, promptDialog } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";
import { fmtDuration } from "./mission.js";

const enc = encodeURIComponent;
const SEV = { error: ["circle-x", "Error"], warning: ["triangle-alert", "Warning"], info: ["info", "Note"] };
const EV_KIND = { check: "Check", calc: "Calculation", datasheet: "Data sheet", sim: "Simulation", hardware: "Built board", note: "Note" };

export class SignoffView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.open = new Set();                               // waivers whose reasoning is shown in full
    this.box = h("div.page-inner.so");
    el.appendChild(h("div.panel", this.box));            // .panel scrolls inside the view (.page only in a flex column)
    for (const e of ["waivers", "signoff", "checks.done", "stages"]) ws.ev.on(e, () => { if (this.el.classList.contains("on")) this.load(); else this.stale = true; });
    this.load();
  }

  shown() { this.stale = false; this.load(); }

  async load() {
    try { this.s = await api(`/api/projects/${enc(this.pid)}/signoff`); } catch (e) { toast(e.message, "error"); return; }
    this.render();
  }

  render() {
    const s = this.s, box = this.box;
    const scroller = box.closest(".panel"), top = scroller ? scroller.scrollTop : 0;
    clear(box);
    const so = s.signoff;
    const state = so && so.valid ? ["ok", "badge-check", "Signed off"] : so ? ["warn", "triangle-alert", "Changed since sign-off"]
      : s.can_sign ? ["ok", "circle-check", "Ready to sign off"] : ["err", "circle-x", "Not ready"];
    box.appendChild(h("div.page-head", h("div.grow", h("h1", "Sign-off"),
      h("p.muted", "The last look before ordering: what the checks found, each requirement's evidence, the waivers, and what only the built board can show.")),
      btn("file-text", "Review packet", { "data-tip": "This page as one printable page: save it as PDF", onclick: () => this.packet() }, "sm"),
      h("span.badge.lg." + state[0], icon(state[1], 13), state[2])));
    box.appendChild(this.verdict(s, so, state));
    box.appendChild(this.requirements(s.requirements || []));
    box.appendChild(this.waivers(s.waivers || []));
    box.appendChild(this.bringup(s));
    box.appendChild(this.stages(s.stages || []));
    const r = this.report(s.report);
    if (r) box.appendChild(r);
    if (scroller) scroller.scrollTop = top;            // an approval redraws the page: it keeps its place
  }

  // ------------------------------------------------------------------ the verdict and what is left
  verdict(s, so, state) {
    const c = s.checks, n = c.counts || {};
    const waived = Object.values(c.waived || {}).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0);
    const act = {
      run: () => btn("play", "Run the checks", { onclick: () => this.runChecks() }, "sm"),
      findings: () => btn("arrow-right", "Findings", { onclick: () => this.ws.show("checks") }, "sm"),
      waivers: () => btn("arrow-down", "Show them", { onclick: () => this.box.querySelector(".so-wgroup.proposed")?.scrollIntoView({ behavior: "smooth", block: "start" }) }, "sm"),
    };
    const todo = (s.todo || []).map((t) => h("div.so-todo", icon("circle-dot", 13), h("span.grow", t.text), act[t.action] ? act[t.action]() : null));
    const reqs = s.requirements || [], withEv = reqs.filter((r) => r.evidence.length).length;
    const bu = s.bringup_steps;
    const facts = h("div.so-facts",
      h("span", icon("list-checks", 13), !c.generated ? "Checks not run" : [`Checks ${fmtTime(c.generated)}`, c.fresh ? "" : " (before the last change)", ": ",
        h("b" + (n.error ? ".t-err" : ""), `${n.error || 0} error${n.error === 1 ? "" : "s"}`), `, ${n.warning || 0} warning${n.warning === 1 ? "" : "s"}`, waived ? `, ${waived} waived` : ""]),
      reqs.length ? h("span", icon("badge-check", 13), `${withEv} of ${reqs.length} requirement${reqs.length === 1 ? "" : "s"} with evidence`) : null,
      bu ? h("span", icon("wrench", 13), `${bu.steps} bring-up step${bu.steps === 1 ? "" : "s"}`) : null);
    const note = h("input", { placeholder: "A note with the sign-off (optional)", maxLength: 400 });
    let sign;
    if (so && so.valid) {
      sign = h("div.so-signrow", h("div.so-signed", icon("badge-check", 15),
        h("span", `Signed off by ${so.by} ${fmtTime(so.at)}${so.commit ? ` (design at ${so.commit})` : ""}.`), so.note ? h("span.muted", `“${so.note}”`) : null),
        h("span.grow"), btn("undo-2", "Take it back", { onclick: () => this.revoke() }, "sm ghost"),
        btn("shopping-cart", "Order", { onclick: () => this.ws.show("outputs") }, "sm primary"));
    } else {
      sign = h("div.so-signrow", note, btn("badge-check", "Sign off the design", { disabled: !s.can_sign, onclick: () => this.sign(note.value) }, "primary"));
    }
    return h("section.so-verdict." + state[0],
      h("div.so-vh", icon(state[1], 18), h("b", so && !so.valid ? "The design changed after it was signed off: sign it off again before ordering."
        : so && so.valid ? "Signed off. Ordering is open." : s.can_sign ? "Nothing stands in the way of signing off." : "Before you can sign off")),
      todo.length ? h("div.so-todos", todo) : null,
      facts, sign);
  }

  // ------------------------------------------------------------------ requirements and their evidence
  requirements(reqs) {
    if (!reqs.length) return this.section("badge-check", "Requirements", h("div.small.muted",
      "No requirements recorded yet: the guided start's list, the bullets of docs/requirements.md and the limits you set appear here."));
    const rows = reqs.map((r) => h("div.so-req",
      h("div.so-rq", h("div.so-rqt", r.text, r.kind === "limit" ? h("span.so-tag", "Limit") : null), r.value ? h("div.so-rqv", r.value) : null),
      h("div.so-evs", r.evidence.length ? r.evidence.map((e) => h("div.so-ev." + (e.status || "ok"),
        h("span.so-evd"), h("span.so-evk", EV_KIND[e.kind] || "Note"), h("span.so-evl", e.label || ""), e.ref ? h("span.so-evr", e.ref) : null))
        : h("div.so-ev.none", h("span.so-evd"), h("span.so-evl", "No evidence yet")))));
    const n = reqs.filter((r) => r.evidence.length).length;
    return this.section("badge-check", "Requirements", h("div.so-reqs", rows), `${n} of ${reqs.length} with evidence`);
  }

  // ------------------------------------------------------------------ waivers as cards, grouped by what they need
  waivers(ws) {
    if (!ws.length) return this.section("shield-check", "Waivers", h("div.small.muted", "No findings are waived."));
    const groups = [["proposed", "Waiting for your approval"], ["applies", "In force"], ["unused", "No longer needed"]];
    const out = [];
    for (const [st, title] of groups) {
      const list = ws.filter((w) => w.state === st);
      if (!list.length) continue;
      out.push(h("div.so-wgroup." + st, h("div.so-wgh", h("span", title), h("span.so-n", String(list.length)), h("span.grow"),
        st === "unused" ? btn("trash-2", "Remove them", { "data-tip": "Their findings are gone: these waivers do nothing", onclick: () => this.prune() }, "sm ghost") : null),
        list.map((w) => this.card(w))));
    }
    const waiting = ws.filter((w) => w.state === "proposed").length;
    return this.section("shield-check", "Waivers", out, waiting ? `${ws.length} · ${waiting} waiting for you` : `${ws.length}`);
  }

  card(w) {
    const sev = SEV[w.severity];
    const full = this.open.has(w.key);
    const reason = w.reason || "(no reason given)";
    const short = w.summary || "";
    const more = reason.length > (short ? short.length : 0) + 12;
    const body = full ? h("div.so-cr", reason) : short ? h("div.so-cr.short", short) : (more ? null : h("div.so-cr", reason));
    const toggle = more ? h("button.so-more", { onclick: () => { if (full) this.open.delete(w.key); else this.open.add(w.key); this.render(); } },
      full ? "Show less" : "Show the reasoning") : null;
    const acts = w.state === "proposed" ? [btn("check", "Approve", { onclick: () => this.waiver("approve", w) }, "sm primary"), btn("x", "Reject", { onclick: () => this.waiver("reject", w) }, "sm")]
      : w.state === "unused" ? [btn("trash-2", "Remove", { onclick: () => this.waiver("reject", w, true) }, "sm ghost")]
      : [btn("undo-2", "Take back", { "data-tip": "The finding counts again, and Claude is told to fix it instead", onclick: () => this.waiver("reject", w) }, "sm ghost")];
    return h("div.so-card." + w.state,
      h("div.so-ch", sev ? h("span.so-sev." + w.severity, icon(sev[0], 12), sev[1]) : null, w.check ? h("span.so-area", w.check) : null, h("span.grow"),
        h("span.so-who", `${w.by === "user" ? "You" : "Claude"}${w.at ? " · " + fmtTime(w.at) : ""}${w.approved && w.by !== "user" ? " · approved" : ""}`)),
      h("div.so-ct", w.title),
      w.message && w.message !== w.title ? h("div.so-cm", "Waives: ", w.message) : null,
      body, toggle,
      (w.sources || []).length ? h("div.so-srcs", w.sources.map((x) => h("span.so-src", icon("book-open", 12), x))) : null,
      h("div.so-cf", h("span.so-key", w.key), h("span.grow"), ...acts));
  }

  // ------------------------------------------------------------------ the built board, stages, the run
  bringup(s) {
    const bu = s.bringup_steps;
    const open = btn("arrow-right", s.bringup ? "Open the bring-up plan" : "Bring-up", { onclick: () => this.ws.show("docs") }, "sm ghost");
    if (!bu) return this.section("wrench", "Needs the built board", h("div.so-line", h("span", "No bring-up plan yet (docs/bring-up.md): what to measure on the first board, power first."), h("span.grow"), open));
    const pct = bu.steps ? Math.round(100 * bu.done / bu.steps) : 0;
    return this.section("wrench", "Needs the built board", [h("div.so-line",
      h("span", `${bu.done} of ${bu.steps} bring-up step${bu.steps === 1 ? "" : "s"} done: what the files cannot prove, power first, then each interface.`),
      h("span.grow"), open), h("div.so-bar", h("i", { style: { width: pct + "%" } }))]);
  }

  stages(st) {
    const open = st.filter((x) => x.status !== "done" && x.status !== "skipped" && x.id !== "release");
    return this.section("list-todo", "Stages", [
      h("div.so-stages", st.map((x) => h("span.so-st." + x.status, { "data-tip": `${x.title}: ${x.status}${x.note ? " — " + x.note : ""}` }, x.title))),
      open.length ? h("div.small.muted", `Not done: ${open.map((x) => x.title).join(", ")}.`) : null]);
  }

  report(r) {
    if (!r || !r.stages || !r.stages.length) return null;
    const tr = (x) => h("tr", h("td", x.title), h("td", x.start ? fmtTime(new Date(x.start * 1000).toISOString()) : ""),
      h("td.n", fmtDuration(x.wall_s)), h("td.n", x.claude_s ? fmtDuration(x.claude_s) : "—"), h("td.n", String(x.turns)), h("td.n", x.cost ? `$${x.cost.toFixed(2)}` : "—"));
    return this.section("activity", "How the run went", [
      h("table.so-report", h("thead", h("tr", ["Stage", "Started", "Took", "Claude working", "Turns", "Cost"].map((t) => h("th", t)))),
        h("tbody", r.stages.map(tr)),
        h("tfoot", h("tr", h("td", "All"), h("td"), h("td.n", r.first && r.last ? fmtDuration(Math.round(r.last - r.first)) : ""),
          h("td.n", fmtDuration(r.claude_s)), h("td.n", String(r.turns)), h("td.n", r.cost ? `$${r.cost.toFixed(2)}` : "—")))),
      h("div.tiny.faint", "Cost at API prices; on a Claude subscription it comes out of the plan's usage instead.")]);
  }

  section(ic, title, body, sub) {
    return h("section.so-sec", h("div.so-h", icon(ic, 14), h("b", title), sub ? h("span.so-sub", sub) : null), body);
  }

  // ------------------------------------------------------------------ actions
  runChecks() {
    api(`/api/projects/${enc(this.pid)}/checks/run`, { body: {} }).then(() => toast("Running the checks…", "info")).catch((e) => toast(e.message, "error"));
  }

  packet() {
    const url = `/api/projects/${enc(this.pid)}/signoff/packet`;
    if (isNative) native.save(url + "?download=1");      // saved as HTML: open it and print to PDF
    else window.open(url, "_blank", "noopener");
  }

  async waiver(action, w, quiet) {
    if (action === "reject" && !quiet) {
      const ok = await confirmDialog({ title: w.state === "proposed" ? "Reject this waiver?" : "Take this waiver back?",
        text: "The finding counts again, and Claude is told to fix it instead.", ok: w.state === "proposed" ? "Reject" : "Take it back" });
      if (!ok) return;
    }
    try { this.s = await api(`/api/projects/${enc(this.pid)}/waivers`, { body: { action, key: w.key } }); this.render(); }
    catch (e) { toast(e.message, "error"); }
    if (action === "approve") toast("Approved. It stops counting when the checks run again.", "ok");
  }

  async prune() {
    try { this.s = await api(`/api/projects/${enc(this.pid)}/waivers`, { body: { action: "prune" } }); this.render(); toast("Removed the waivers no longer needed.", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }

  async sign(note) {
    try { this.s = await api(`/api/projects/${enc(this.pid)}/signoff`, { body: { note } }); this.render(); toast("Signed off. Ordering is open.", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }

  async revoke() {
    const ok = await confirmDialog({ title: "Take the sign-off back?", text: "Ordering closes until you sign the design off again.", ok: "Take it back" });
    if (!ok) return;
    try { this.s = await api(`/api/projects/${enc(this.pid)}/signoff`, { method: "DELETE" }); this.render(); } catch (e) { toast(e.message, "error"); }
  }
}

// a waiver of the user's own, from a finding in the checks view
export async function waiveFinding(pid, f) {
  const reason = await promptDialog({ title: "Waive this finding?", text: f.message, label: "Why it is acceptable on this board", ok: "Waive", multiline: true });
  if (!reason || !reason.trim()) return null;
  return api(`/api/projects/${enc(pid)}/waivers`, { body: { action: "add", key: f.key, reason, severity: f.severity, message: f.message } });
}
