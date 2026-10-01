// Sign-off: the last look before ordering. Whether the checks ran on the design as it is now and what they
// found, each waiver (Claude's waivers on errors wait here for your approval), the stages, what only the
// built board can show, and how the run went; then Sign off, which ordering needs. See signoff.py.
import { h, clear, api, toast, btn, fmtTime, confirmDialog, promptDialog } from "./util.js";
import { icon } from "./icons.js";
import { fmtDuration } from "./mission.js";

const enc = encodeURIComponent;
const SEV = { error: "circle-x", warning: "triangle-alert", info: "info" };

export class SignoffView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.box = h("div.page-inner.so");
    el.appendChild(h("div.panel", this.box));            // .panel scrolls inside the view (.page only in a flex column)
    for (const e of ["waivers", "signoff", "checks.done", "stages"]) ws.ev.on(e, () => { if (this.el.classList.contains("on")) this.load(); else this.stale = true; });
    this.load();
  }

  shown() { if (this.stale || !this.s) { this.stale = false; this.load(); } else this.load(); }

  async load() {
    try { this.s = await api(`/api/projects/${enc(this.pid)}/signoff`); } catch (e) { toast(e.message, "error"); return; }
    this.render();
  }

  render() {
    const s = this.s, box = clear(this.box);
    const so = s.signoff;
    const state = so && so.valid ? ["ok", "badge-check", "Signed off"] : so ? ["warn", "triangle-alert", "Changed since sign-off"]
      : s.can_sign ? ["info", "circle-check", "Ready to sign off"] : ["err", "circle-x", "Not ready"];
    box.appendChild(h("div.page-head", h("div.grow", h("h1", "Sign-off"),
      h("p.muted", "The last look before ordering: what the checks found, the waivers, and what only the built board can show.")),
      h("span.badge.lg." + state[0], icon(state[1], 13), state[2])));

    // the checks
    const c = s.checks, n = c.counts || {};
    const when = c.generated ? fmtTime(c.generated) : null;
    box.appendChild(this.section("list-checks", "Checks",
      h("div.so-line", !c.generated ? h("span", "The checks have not run yet.")
        : h("span", `Ran ${when}${c.fresh ? " on the design as it is now" : ""}.`,
          " ", h("b" + (n.error ? ".t-err" : ".t-ok"), `${n.error || 0} error${n.error === 1 ? "" : "s"}`), `, ${n.warning || 0} warning${n.warning === 1 ? "" : "s"}`,
          (c.waived && c.waived.total) || Object.values(c.waived || {}).some(Boolean) ? `, ${Object.values(c.waived || {}).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0)} waived` : ""),
        c.generated && !c.fresh ? h("span.so-warn", icon("triangle-alert", 13), "The design changed after this run.") : null,
        h("span.grow"),
        btn("play", c.fresh ? "Run again" : "Run the checks", { onclick: () => api(`/api/projects/${enc(this.pid)}/checks/run`, { body: {} })
          .then(() => toast("Running the checks…", "info")).catch((e) => toast(e.message, "error")) }, "sm"),
        btn("arrow-right", "Findings", { onclick: () => this.ws.show("checks") }, "sm ghost"))));

    // waivers: the ones waiting for you first
    const ws = s.waivers || [], waiting = ws.filter((w) => w.state === "proposed");
    const rows = ws.map((w) => h("div.so-w." + w.state,
      h("span.so-sev." + (w.severity || "info"), icon(SEV[w.severity] || "info", 13), w.severity || "—"),
      h("div.grow", h("div.so-wm", w.message || w.key), h("div.so-wr", h("span.muted", w.by === "user" ? "You: " : "Claude: "), w.reason || "(no reason given)"),
        h("div.tiny.faint", w.key)),
      w.state === "proposed" ? h("div.so-act",
        btn("check", "Approve", { onclick: () => this.waiver("approve", w) }, "sm primary"),
        btn("x", "Reject", { onclick: () => this.waiver("reject", w) }, "sm")) :
        h("div.so-act", h("span.small.muted", w.approved && w.by !== "user" ? "approved" : w.by === "user" ? "yours" : "applies"),
          btn("x", null, { "data-tip": "Take this waiver back", onclick: () => this.waiver("reject", w) }, "sm ghost"))));
    box.appendChild(this.section("shield-check", "Waivers", ws.length ? rows : h("div.small.muted", "No findings are waived."),
      waiting.length ? `${waiting.length} waiting for you` : ws.length ? `${ws.length}` : null));

    // stages and what needs the built board
    const st = s.stages || [];
    const open = st.filter((x) => x.status !== "done" && x.status !== "skipped" && x.id !== "release");
    box.appendChild(this.section("list-todo", "Stages",
      h("div.so-stages", st.map((x) => h("span.so-st." + x.status, { "data-tip": `${x.title}: ${x.status}${x.note ? " — " + x.note : ""}` }, x.title))),
      open.length ? h("div.small.muted", `Not done: ${open.map((x) => x.title).join(", ")}.`) : null));
    box.appendChild(this.section("wrench", "Needs the built board",
      h("div.so-line", h("span", s.bringup ? "The bring-up plan lists what the files cannot prove: power first, then each interface." :
        "No bring-up plan yet (docs/bring-up.md): what to measure on the first board."), h("span.grow"),
        s.bringup ? btn("arrow-right", "Bring-up plan", { onclick: () => { this.ws.show("docs"); } }, "sm ghost") : null)));

    // how the run went
    const r = s.report;
    if (r && r.stages && r.stages.length) {
      const tr = (x) => h("tr", h("td", x.title), h("td", x.start ? fmtTime(new Date(x.start * 1000).toISOString()) : ""),
        h("td.n", fmtDuration(x.wall_s)), h("td.n", x.claude_s ? fmtDuration(x.claude_s) : "—"), h("td.n", String(x.turns)), h("td.n", x.cost ? `$${x.cost.toFixed(2)}` : "—"));
      box.appendChild(this.section("activity", "How the run went",
        h("table.so-report", h("thead", h("tr", ["Stage", "Started", "Took", "Claude working", "Turns", "Cost"].map((t) => h("th", t)))),
          h("tbody", r.stages.map(tr)),
          h("tfoot", h("tr", h("td", "All"), h("td"), h("td.n", r.first && r.last ? fmtDuration(Math.round(r.last - r.first)) : ""),
            h("td.n", fmtDuration(r.claude_s)), h("td.n", String(r.turns)), h("td.n", r.cost ? `$${r.cost.toFixed(2)}` : "—")))),
        h("div.tiny.faint", "Cost at API prices; on a Claude subscription it comes out of the plan's usage instead.")));
    }

    // sign off
    const note = h("input", { placeholder: "A note with the sign-off (optional)", maxLength: 400 });
    const signBtn = btn("badge-check", so && so.valid ? "Signed off" : "Sign off the design", { disabled: !s.can_sign || (so && so.valid), onclick: () => this.sign(note.value) }, "primary");
    box.appendChild(h("div.so-sign" + (s.can_sign ? "" : ".blocked"),
      so ? h("div.so-signed" + (so.valid ? "" : ".stale"), icon(so.valid ? "badge-check" : "triangle-alert", 15),
        h("span", so.valid ? `Signed off by ${so.by} ${fmtTime(so.at)}${so.commit ? ` (design at ${so.commit})` : ""}.`
          : `Signed off by ${so.by} ${fmtTime(so.at)}, but the design has changed since: sign it off again before ordering.`),
        so.note ? h("span.muted", `“${so.note}”`) : null, h("span.grow"),
        btn("undo-2", "Take it back", { onclick: () => this.revoke() }, "sm ghost")) : null,
      !s.can_sign ? h("div.so-need", h("div.so-needh", "Before you can sign off"), h("ul.so-block", s.blockers.map((b) => h("li", b)))) : null,
      !(so && so.valid) ? h("div.row", note, signBtn) : null,
      so && so.valid ? h("div.row", btn("shopping-cart", "Order", { onclick: () => this.ws.show("outputs") }, "primary")) : null));
  }

  section(ic, title, body, sub) {
    return h("section.so-sec", h("div.so-h", icon(ic, 14), h("b", title), sub ? h("span.so-sub", sub) : null), body);
  }

  async waiver(action, w) {
    if (action === "reject") {
      const ok = await confirmDialog({ title: w.state === "proposed" ? "Reject this waiver?" : "Take this waiver back?",
        text: "The finding counts again, and Claude is told to fix it instead.", ok: w.state === "proposed" ? "Reject" : "Take it back" });
      if (!ok) return;
    }
    try { this.s = await api(`/api/projects/${enc(this.pid)}/waivers`, { body: { action, key: w.key } }); this.render(); }
    catch (e) { toast(e.message, "error"); }
    if (action === "approve") toast("Approved. It stops counting when the checks run again.", "ok");
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
