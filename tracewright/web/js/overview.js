// The Overview tab: a project at a glance. The board's picture and where the design is, what to do
// next (each a click away: a message to Claude, a view, a command), the numbers that matter (checks,
// board, parts, cost), and what happened lately.
import { h, clear, api, fmtTime, btn } from "./util.js";
import { icon } from "./icons.js";
import { runCommand } from "./app.js";

const enc = encodeURIComponent;

export class OverviewPanel {
  constructor(el, ws) {
    this.ws = ws; this.pid = ws.pid;
    this.el = h("div.ov");
    el.appendChild(h("div.panel", h("div.panel-inner.ov-inner", this.el)));
    for (const e of ["checks.done", "board.changed", "stages", "history.snapshot", "bom.changed", "agent.done"]) ws.ev.on(e, () => this.soon());
    this.load();
  }

  shown() { this.load(); }
  soon() { clearTimeout(this.t); this.t = setTimeout(() => this.load(), 800); }

  async load() {
    try { this.d = await api(`/api/projects/${enc(this.pid)}/overview`); } catch (e) { clear(this.el).appendChild(h("div.small.muted", e.message)); return; }
    this.render();
  }

  act(a) {
    if (!a) return;
    if (a.kind === "chat") { this.ws.ask(a.text); }
    else if (a.kind === "tab") this.ws.show(a.tab);
    else if (a.kind === "command") runCommand(a.name);
  }

  render() {
    const d = this.d, p = d.project, b = d.board, c = d.checks, bom = d.bom;
    const el = clear(this.el);
    const stages = p.stages || [];
    const active = stages.find((s) => s.status === "active");
    const thumb = h("div.ov-thumb", d.has_pcb ? { style: { backgroundImage: `url(/api/projects/${enc(this.pid)}/thumb?t=${Date.now()})` } } : null,
      d.has_pcb ? null : h("div.ov-noimg", icon(d.has_sch ? "waypoints" : "pencil", 26), h("span", d.has_sch ? "No board yet" : "Not started")));
    el.appendChild(h("section.ov-hero", thumb,
      h("div.ov-hi",
        h("div.ov-kind", p.kind === "imported" ? "Imported project" : p.kind === "in_place" ? "Opened in place" : p.kind === "demo" ? "Demo" : "Project",
          h("span", "·"), h("span", d.sourcing === "self" ? "Self-assembled" : "JLC turnkey"), h("span", "·"), h("span", `Edited ${fmtTime(p.updated)}`)),
        h("h1", p.name),
        p.description ? h("p.ov-desc", p.description) : null,
        h("div.ov-stages", stages.map((s) => h("div.ov-st." + s.status, { "data-tip": s.note || s.description || s.title }, h("i"), h("span", s.title)))),
        h("div.ov-actions",
          btn("sparkles", "Ask Claude", { onclick: () => this.ws.ask("") }, "primary"),
          btn("list-checks", "Run checks", { onclick: () => runCommand("run-checks") }),
          d.has_pcb ? btn("shopping-cart", "Order", { onclick: () => this.ws.show("outputs") }) : null,
          active ? h("span.ov-now", icon("circle-dot", 12), `Now: ${active.title}`) : null))));
    if (d.next.length) el.appendChild(h("section.ov-sec", h("div.ov-h", icon("list-todo", 14), "Next steps"),
      h("div.ov-next", d.next.map((n, i) => h("button.ov-step", { onclick: () => this.act(n.action), style: { animationDelay: `${i * 0.05}s` } },
        h("span.ov-si", icon(n.icon || "arrow-right", 16)), h("div.grow", h("b", n.title), h("span", n.detail)),
        h("span.ov-go", n.action.kind === "chat" ? "Ask Claude" : n.action.kind === "tab" ? "Open" : "Run", icon("arrow-right", 13)))))));
    const cc = (c && c.counts) || {};
    const tiles = [
      tile("list-checks", "Checks", c ? (c.verdict === "checks pass" ? "Passing" : c.verdict === "not ready" ? `${cc.error} error${cc.error === 1 ? "" : "s"}` : `${cc.warning} warning${cc.warning === 1 ? "" : "s"}`) : "Not run",
        c ? `${cc.error || 0} errors · ${cc.warning || 0} warnings · ${cc.info || 0} notes` : `${d.check_count || ""} checks`.trim(), c ? (c.verdict === "checks pass" ? "ok" : c.verdict === "not ready" ? "bad" : "warn") : "", () => this.ws.show("checks")),
      tile("circuit-board", "Board", b ? `${b.w} × ${b.h} mm` : "—",
        b ? `${b.layers} layers · ${b.placed}/${b.parts} placed · ${b.nets ? Math.round(100 * b.routed / b.nets) : 0}% routed` : "No board yet", "", () => this.ws.show("board"),
        b && b.nets ? b.routed / b.nets : null),
      tile("list", "Parts", bom ? `${bom.parts} parts` : "—", bom ? `${bom.lines} lines · ${bom.basic || 0} Basic · ${bom.extended || 0} Extended${bom.no_lcsc ? ` · ${bom.no_lcsc} without LCSC` : ""}` : "No parts yet", "", () => this.ws.show("bom")),
      tile("shopping-cart", "5 boards", d.estimate ? `≈ $${Math.round(d.estimate)}${d.estimate_parts ? "" : " + parts"}` : "—",
        !d.estimate ? "Needs a board" : !d.estimate_parts ? "Parts not priced yet"
          : d.sourcing === "self" ? "Boards, stencil and parts" : "Made and assembled at JLC", "", () => this.ws.show(d.estimate_parts ? "outputs" : "bom")),
    ];
    el.appendChild(h("section.ov-tiles", tiles));
    if (d.activity.length) el.appendChild(h("section.ov-sec", h("div.ov-h", icon("history", 14), "Recent activity", h("div.grow"), h("button.linkbtn", { onclick: () => this.ws.show("history") }, "History")),
      h("div.ov-act", d.activity.map((a) => h("div.ov-ai" + (a.kind === "conversation" ? ".conv" : ""), { onclick: () => a.kind === "conversation" ? this.ws.chat.useSession(a.sid) : this.ws.show("history") },
        h("span.ov-aic", icon(a.kind === "conversation" ? "message-square" : a.by === "claude" ? "sparkles" : "bookmark", 13)),
        h("span.grow.ellipsis", a.text), a.turns ? h("span.tiny.muted", `${a.turns} turn${a.turns === 1 ? "" : "s"}`) : null, h("span.tiny.muted", fmtTime(a.when)))))));
  }
}

function tile(ic, label, value, sub, cls, onclick, frac) {
  return h("button.ov-tile" + (cls ? "." + cls : ""), { onclick }, h("div.ov-tl", icon(ic, 14), h("span", label)), h("b", value), h("span.ov-ts", sub),
    frac !== null && frac !== undefined ? h("div.ov-meter", h("i", { style: { width: `${Math.round(100 * frac)}%` } })) : null);
}
