// What needs your OK after one of Claude's runs (approvals.py): a short list at the end of the run, never a question in
// the middle of it. What Claude went ahead with (a part swapped once agreed, a connector moved, your own work changed,
// a looser rule): Keep, or Undo -- Claude puts it back and redoes what hung on it. What it asked (a change to the
// agreed limits, kept as agreed meanwhile): Approve or Decline. Shown in the run's card in the chat and at the top of
// the Review panel.
import { h, clear, api, toast, Emitter } from "./util.js";
import { icon } from "./icons.js";

const enc = encodeURIComponent;
const KIND_ICON = { parts: "microchip", floorplan: "frame", handmade: "pencil", rules: "ruler", limits: "sliders-horizontal", signed: "badge-check" };
const DONE = { kept: "Kept", undone: "Claude is putting it back", approved: "Approved", declined: "Declined" };

export class Approvals extends Emitter {
  constructor(ws) {
    super();
    this.ws = ws; this.pid = ws.pid; this.items = [];
    ws.ev.on("approvals.changed", (e) => { this.items = e.items || []; this.emit("changed", this.items); });
    this.load();
  }

  async load() {
    try { const r = await api(`/api/projects/${enc(this.pid)}/approvals`); this.items = r.items || []; this.emit("changed", this.items); } catch { /* offline */ }
  }

  pending() { return this.items.filter((x) => x.status === "pending"); }
  get(id) { return this.items.find((x) => x.id === id); }

  async decide(id, action) {
    try {
      const it = await api(`/api/projects/${enc(this.pid)}/approvals/${enc(id)}`, { body: { action } });
      this.items = this.items.map((x) => (x.id === id ? it : x));
      this.emit("changed", this.items);
      if (action === "undo") toast("Claude is putting it back", "info", 3500);
    } catch (e) { toast(e.message, "error"); }
  }

  // a list of items that keeps itself up to date: those of one run (ids), or every pending one
  list(ids) {
    const el = h("div.oklist");
    const draw = () => {
      clear(el);
      const items = ids ? ids.map((id) => this.get(id)).filter(Boolean) : this.pending();
      if (!items.length) { el.style.display = "none"; return; }
      el.style.display = "";
      const open = items.filter((x) => x.status === "pending");
      const asks = open.some((x) => x.group === "ask"), did = open.some((x) => x.group !== "ask");
      const sub = !open.length ? "" : asks && did ? "Keep or undo what Claude did; approve what it asks" : asks ? "Claude asks before changing these"
        : "Claude went ahead with these: keep them, or have them put back";
      el.appendChild(h("div.ok-h", icon("list-checks", 13), h("b", open.length ? `Needs your OK (${open.length})` : "Your OK"), h("span.ok-sub", sub)));
      for (const it of items) el.appendChild(this.row(it));
    };
    let seen = false;
    const off = this.on("changed", () => {
      if (seen && !el.isConnected) { off(); return; }                  // its card is gone: stop listening
      draw();
      if (el.isConnected) seen = true;
    });
    draw();
    return el;
  }

  row(it) {
    const busy = this.ws.chat && this.ws.chat.busy;
    const acts = it.status !== "pending" ? h("span.ok-done." + it.status, DONE[it.status] || it.status)
      : it.group === "ask"
        ? h("div.ok-acts", h("button.btn.sm", { onclick: () => this.decide(it.id, "decline"), "data-tip": "Keep the agreed one" }, "Decline"),
            h("button.btn.sm.primary", { onclick: () => this.decide(it.id, "approve") }, "Approve"))
        : h("div.ok-acts", h("button.btn.sm", { onclick: () => this.decide(it.id, "undo"), disabled: busy || undefined,
            "data-tip": busy ? "Available when Claude finishes" : "Claude puts it back and redoes what depended on it" }, icon("undo-2", 12), "Undo"),
            h("button.btn.sm.primary", { onclick: () => this.decide(it.id, "keep") }, "Keep"));
    const show = it.ref ? h("button.ok-ref", { onclick: () => { this.ws.show("board"); this.ws.view("board").highlight({ refs: [it.ref] }); }, "data-tip": "Show it" }, it.ref) : null;
    return h("div.ok-row." + it.status, h("span.ok-ic", icon(KIND_ICON[it.kind] || "circle-alert", 13)),
      h("div.ok-b", h("div.ok-t", it.title), it.detail ? h("div.ok-d", it.detail, show ? " " : null, show) : show), acts);
  }
}
