// The conversation with Claude: its narration, the steps it takes (one readable line each, grouped and
// folded once a burst of work is over), its agenda as a live checklist, notes you send while it works
// (read at its next step), its questions docked above the composer, permission prompts, the context
// you attach (selections, KiCad's selection) and the review flags you send.
import { h, clear, api, toast, lightbox, fmtTime, btn, menu, copyText } from "./util.js";
import { icon } from "./icons.js";
import { markdown } from "./markdown.js";
import { native } from "./native.js";

export const ICON = { status: "info", board: "circuit-board", show: "target", annotate: "map-pin", place: "move", route: "route", copper: "layers",
  sync_board: "refresh-cw", silk: "pencil", run_checks: "list-checks", render: "image", parts: "microchip", stage: "list-todo", lessons: "book-open",
  snapshot: "bookmark", kicad: "plug", outputs: "package", review: "flag", agenda: "list-todo",
  Bash: "terminal", Read: "file-text", Grep: "search", Glob: "search", Edit: "pencil", Write: "pencil", MultiEdit: "pencil", NotebookEdit: "pencil",
  WebFetch: "external-link", WebSearch: "search", TodoWrite: "list-todo", Skill: "sparkles", Task: "bot", Agent: "bot", TaskOutput: "terminal", TaskStop: "circle-stop" };
const LIVE_STEPS = 3;                              // a burst of work in progress shows its latest steps
const HIDDEN = new Set(["agenda", "AskUserQuestion"]);   // drawn elsewhere (the agenda card, the question card)

export function toolName(n) { return n.startsWith("mcp__tw__") ? n.slice(9) : n; }
const tail = (p, n = 2) => String(p || "").split("/").filter(Boolean).slice(-n).join("/");
const host = (u) => { try { return new URL(u).host.replace(/^www\./, ""); } catch { return String(u || "").slice(0, 40); } };
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
// a shell command without the `cd <project> &&` it usually starts with, on one line
const shortCmd = (c) => String(c || "").replace(/^\s*cd\s+("[^"]*"|'[^']*'|\S+)\s*(&&|;)\s*/, "").replace(/\s+/g, " ").trim().slice(0, 110);
// a command Claude gave no description for, in words (the command itself is the detail)
function cmdLabel(c) {
  const w = shortCmd(c).split(/\s+/), exe = (w[0] || "").split("/").pop();
  if (/^python3?$/.test(exe)) return w[1] === "-" || w[1] === "-c" || !w[1] ? "Ran a Python script" : `Ran ${tail(w[1], 1)}`;
  if (w[0] === "./tw" || exe === "tw") return `Ran tw ${w.slice(1, 3).join(" ")}`;
  if (exe === "kicad-cli") return `Ran KiCad: ${w.slice(1, 3).join(" ")}`;
  if (exe === "git") return `git ${w[1] || ""}`;
  if (["ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "stat", "du", "file", "diff"].includes(exe)) return "Looked at files";
  if (["cp", "mv", "mkdir", "touch", "zip", "unzip", "rm"].includes(exe)) return "Organised files";
  if (exe === "sed" || exe === "awk") return `Edited text (${exe})`;
  if (exe === "sleep") return "Waited";
  return null;
}
// a large input arrives cut short, as JSON text: pull a field out of it
const fromCut = (inp, key) => { const m = String(inp._truncated || "").match(new RegExp(`"${key}":\\s*"((?:[^"\\\\]|\\\\.)*)"`)); return m ? m[1].replace(/\\"/g, '"') : ""; };

// What a step did, in words: the label is what you read, the detail is quieter context.
export function stepText(name, inp) {
  const n = toolName(name);
  inp = inp || {};
  const cut = !!inp._truncated;
  const arr = (x) => Array.isArray(x) ? x : [];
  switch (n) {
    case "status": return ["Checked the project status"];
    case "board": return [`Read the board: ${inp.what || "summary"}${inp.ref ? " " + inp.ref : ""}${inp.net ? " " + inp.net : ""}`];
    case "show": { const w = [...arr(inp.refs), ...arr(inp.nets)]; return [w.length ? `Pointed at ${w.slice(0, 6).join(", ")}${w.length > 6 ? ` +${w.length - 6}` : ""}` : "Pointed at a spot on the board", inp.note]; }
    case "annotate": return ["Pinned notes on the board"];
    case "place": { const m = arr(inp.moves); return [cut ? "Moved parts" : `Moved ${plural(m.length, "part")}`, m.map((x) => x.ref).slice(0, 10).join(", ")]; }
    case "route": return [inp.nets ? `Routed ${inp.nets.slice(0, 4).join(", ")}${inp.nets.length > 4 ? ` +${inp.nets.length - 4}` : ""}` : "Routed the unrouted nets", inp.engine === "freerouting" ? "Freerouting" : ""];
    case "copper": return [`Edited the copper${cut ? "" : ` (${[...new Set(arr(inp.ops).map((o) => o.op))].join(", ")})`}`];
    case "sync_board": return ["Updated the board from the schematic"];
    case "silk": return ["Tidied the silkscreen"];
    case "run_checks": return [inp.only ? `Ran checks: ${inp.only.join(", ")}` : "Ran all checks"];
    case "render": return [`Looked at the ${inp.what === "board" ? "board" : inp.what || "design"}${inp.sheet ? ` (${inp.sheet})` : ""}${inp.region ? ", zoomed in" : ""}`, arr(inp.layers).join(", ")];
    case "parts": return [`Looked up parts: ${inp.query || ""}`];
    case "stage": return [`Stage ${inp.stage || ""}: ${inp.status || ""}`, inp.note];
    case "lessons": return [inp.action === "add" ? `Recorded a lesson: ${inp.title || ""}` : `Checked the lessons${inp.query ? ": " + inp.query : ""}`];
    case "snapshot": return ["Saved a checkpoint", inp.message];
    case "kicad": return [`KiCad: ${inp.action || "status"}`];
    case "canvas": return [{ requirements: "Updated the requirements", diagram: "Drew the block diagram", connectors: "Laid out the connectors",
      parts: "Updated the parts list" }[inp.section] || "Updated the canvas"];
    case "ready_to_start": return ["Ready to start"];
    case "outputs": return ["Generated fab outputs"];
    case "review": return [inp.action === "resolve" ? `Resolved review flag ${inp.id || ""}` : inp.action === "add" ? "Added a review flag" : "Read review flags"];
    case "Bash": { const d = inp.description || fromCut(inp, "description"), raw = inp.command || fromCut(inp, "command"), c = shortCmd(raw);
      return d ? [d, c] : cmdLabel(raw) ? [cmdLabel(raw), c] : [c || "Ran a command"]; }
    case "Read": return [`Read ${tail(inp.file_path || fromCut(inp, "file_path"))}`];
    case "Write": return [`Wrote ${tail(inp.file_path || fromCut(inp, "file_path"))}`];
    case "Edit": case "MultiEdit": return [`Edited ${tail(inp.file_path || fromCut(inp, "file_path"))}`];
    case "NotebookEdit": return [`Edited ${tail(inp.notebook_path)}`];
    case "Grep": return [`Searched for “${inp.pattern || ""}”`, inp.path ? tail(inp.path) : ""];
    case "Glob": return [`Listed ${inp.pattern || "files"}`];
    case "WebSearch": return [`Searched the web: ${inp.query || ""}`];
    case "WebFetch": return [`Read ${host(inp.url)}`, inp.url];
    case "Skill": return [`Used the ${inp.skill || inp.name || ""} skill`];
    case "Task": case "Agent": return [`Subagent: ${inp.description || "a task"}`];
    case "TaskOutput": return ["Read a background command's output"];
    case "TaskStop": return ["Stopped a background task"];
    default: { const s = cut ? "" : JSON.stringify(inp); return [n.replace(/_/g, " "), s.length > 90 ? s.slice(0, 90) + "..." : s]; }
  }
}

// "claude-haiku-4-5-20251001" -> "Haiku 4.5", "claude-opus-5-5" -> "Opus 5.5"
export function modelLabel(id) {
  if (!id || id === "default") return "Claude";
  const m = String(id).replace(/^claude-/, "").replace(/-\d{8}$/, "").match(/^([a-z]+)-(\d+)(?:-(\d+))?/);
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? "." + m[3] : ""}` : String(id);
}

// Kinds of step, for the one-line summary of a finished burst of work.
export const KIND = (n) => ({ Bash: "command", Read: "read", Grep: "search", Glob: "search", Write: "file edit", Edit: "file edit", MultiEdit: "file edit",
  render: "view", run_checks: "check run", place: "board edit", route: "board edit", copper: "board edit", silk: "board edit", sync_board: "board edit",
  parts: "lookup", lessons: "lookup", WebSearch: "lookup", WebFetch: "lookup", board: "query", status: "query" })[n] || "step";

export class Chat {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.cards = {}; this.cur = null; this.busy = false; this.sel = []; this.kicadSel = []; this.pending = 0;
    this.agenda = null; this.agOpen = localStorage.getItem("tw.agenda.open") !== "0";
    this.build();
    this.load();
    this.wire();
  }

  destroy() { clearInterval(this.tick); }

  build() {
    this.titleEl = h("span.ellipsis", "Claude");
    this.costEl = h("span.cost", { "data-tip": "Estimated at API prices. Not billed on a Claude subscription." });
    this.modeBtn = h("button.modepill", { onclick: () => this.toggleMode() });
    this.el.append(h("div.chathead",
      h("button.ctitle", { onclick: (e) => this.sessionsMenu(e.currentTarget), "data-tip": "Conversations" }, h("span.cmark", icon("sparkles", 12)), this.titleEl, icon("chevron-down", 12)),
      h("div.grow"), this.modeBtn,
      this.mcBtn = btn("activity", null, { onclick: () => this.ws.mission(), "data-tip": "Run monitor", "data-kbd": "mod+shift+m" }, "sm ghost mcbtn"),
      btn("ellipsis", null, { onclick: (e) => this.chatMenu(e.currentTarget), "data-tip": "Conversation" }, "sm ghost")));
    this.agendaEl = h("div.agenda.hidden");
    this.msgs = h("div.msgs");
    this.status = h("div.statusline");
    this.dock = h("div.qdock");
    this.chips = h("div.chips");
    this.input = h("textarea", { placeholder: "Message Claude", rows: 1,
      onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); } },
      oninput: () => this.grow() });
    this.stopBtn = h("button.stopbtn", { onclick: () => this.stop(), "data-tip": "Stop Claude", "data-kbd": "mod+." }, icon("square", 11));
    this.sendBtn = h("button.sendbtn.idle", { onclick: () => this.send(), "data-tip": "Send", "data-kbd": "enter" }, icon("arrow-up", 16));
    this.modelEl = h("span.model");
    this.hintEl = h("span.tiny.faint", "⇧↩ new line");
    this.el.append(this.agendaEl, this.msgs, this.status, this.dock, h("div.composer", h("div.composer-box", this.chips, this.input,
      h("div.composer-bar", this.modelEl, h("div.grow"), this.hintEl, this.stopBtn, this.sendBtn))));
  }

  grow() {
    this.input.style.height = "auto";
    this.input.style.height = Math.min(240, this.input.scrollHeight + 2) + "px";
    this.sendBtn.classList.toggle("idle", !this.input.value.trim());
  }

  async load() {
    const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/sessions`);
    this.sessions = r.sessions || [];
    this.sid = r.current;
    this.renderTitle();
    clear(this.msgs); clear(this.dock);
    this.cards = {}; this.steps = null; this.steerEls = {};
    this.setAgenda(null);
    this.pendingIds = new Set((r.pending || []).map((p) => p.id));
    if (this.sid) {
      const s = await api(`/api/projects/${encodeURIComponent(this.pid)}/sessions/${this.sid}`);
      this.renderTranscript(s.transcript || [], r.busy);
      this.costEl.textContent = s.meta.cost ? `≈ $${s.meta.cost.toFixed(2)}` : "";
    }
    if (!this.msgs.children.length) this.suggestions();
    for (const p of r.pending || []) (p.kind === "question" ? this.question(p) : this.permission(p));
    this.setBusy(r.busy);
    api(`/api/projects/${encodeURIComponent(this.pid)}`).then((pr) => this.renderMode(pr)).catch(() => {});
    const st = await api("/api/settings").catch(() => ({}));
    this.modelEl.textContent = modelLabel(st.model);
  }

  renderTitle() {
    const s = this.sessions.find((x) => x.sid === this.sid);
    this.titleEl.textContent = s ? s.title : "New conversation";
  }

  sessionsMenu(anchor) {
    menu(anchor, [
      { label: "New conversation", icon: "plus", kbd: "mod+shift+n", run: () => this.newSession() },
      this.sessions.length ? "-" : null, this.sessions.length ? { head: "Conversations" } : null,
      ...this.sessions.slice(0, 14).map((s) => ({ label: s.title, checked: s.sid === this.sid, hint: fmtTime(s.updated), run: () => this.useSession(s.sid) })),
    ]);
  }

  chatMenu(anchor) {
    const cost = this.costEl.textContent;
    menu(anchor, [
      { label: "New conversation", icon: "plus", kbd: "mod+shift+n", run: () => this.newSession() },
      { label: "Conversations…", icon: "messages-square", run: () => this.sessionsMenu(this.el.querySelector(".ctitle")) },
      { label: "Run monitor", icon: "activity", kbd: "mod+shift+m", run: () => this.ws.mission() },
      "-",
      { label: this.mode === "autonomous" ? "Switch to step by step" : "Switch to autonomous", icon: this.mode === "autonomous" ? "messages-square" : "zap", run: () => this.toggleMode() },
      cost ? { label: `Cost: ${cost}`, icon: "info", disabled: true, hint: "API prices" } : null,
      "-",
      { label: "Hide chat", icon: "panel-left-close", kbd: "mod+\\", run: () => this.ws.toggleChat() },
    ], { align: "end" });
  }

  async newSession() {
    if (this.busy) { toast("Stop Claude first", "warn"); return; }
    await api(`/api/projects/${encodeURIComponent(this.pid)}/sessions`, { body: {} });
    await this.load();
    this.input.focus();
  }

  async useSession(sid) {
    if (!sid || sid === this.sid) return;
    try { await api(`/api/projects/${encodeURIComponent(this.pid)}/sessions/${sid}/use`, { body: {} }); } catch (e) { toast(e.message, "error"); return; }
    await this.load();
  }

  // The empty conversation: a line, and the next likely actions (each fills in the message to send).
  suggestions() {
    const p = this.ws.p;
    const acts = [];
    if (p.kind === "imported" || p.kind === "in_place") {
      acts.push(["search", "Review design", "Review this design (skill import-review): summarise it, run every check, and tell me what is verified, what looks wrong, and what needs the built board. Don't change anything yet."]);
      acts.push(["package", "Prepare for JLC", "Get this design ready to order from JLCPCB with assembly: check the BOM has LCSC codes with stock, fit the CPL to JLC's footprints, check the fab rules, and tell me what's left."]);
    } else if (!p.has_sch || ((p.stages || []).find((s) => s.id === "brief") || {}).status === "active") {
      acts.push(["sparkles", "Start the design", "Start the design from the brief: ask me only what changes the design, then write docs/requirements.md."]);
      acts.push(["layers", "Propose an architecture", "Propose an architecture for this board: block diagram, power tree with currents, and the key parts (with JLC stock). Keep it simple."]);
    } else if (!p.has_pcb) {
      acts.push(["circuit-board", "Start the layout", "Create the board from the schematic (sync_board), then propose an outline, mounting holes and connector positions before placing anything."]);
    } else {
      acts.push(["list-checks", "Run checks", "Run all the checks and explain the findings, most important first."]);
      acts.push(["move", "Place parts", "Place the parts in functional groups, one group at a time, explaining each step. Show me each group before moving on."]);
      acts.push(["route", "Route board", "Route the board: supplies first, then pairs, then signals. Stream it and run the routing checks afterwards."]);
    }
    const box = h("div.suggest", h("div.shead", h("span.small", "For this stage")),
      h("div.schips", acts.map(([ic, t, msg]) => h("button.schip", { onclick: () => { this.input.value = msg; this.grow(); this.input.focus(); } }, icon(ic, 13), t)),
        h("button.schip", { onclick: () => this.ws.flagTool() }, icon("flag", 13), "Flag areas")));
    this.msgs.appendChild(box);
  }

  renderTranscript(recs, busy) {
    const answered = {}, steerState = {};
    let agenda = null;
    for (const r of recs) {
      if (r.kind === "answer") answered[r.id] = r.answers;
      else if (r.kind === "steer_read") steerState[r.id] = "read";
      else if (r.kind === "steer_dropped") steerState[r.id] = "dropped";
    }
    this.replaying = true;
    const ran = [];                                  // older transcripts logged every slow command as a "task" too
    const wasStep = (d) => { const b = String(d || "").trim().slice(0, 50); return b.length > 3 && ran.some((x) => { const a = x.slice(0, 50); return a.startsWith(b) || b.startsWith(a); }); };
    for (const [i, r] of recs.entries()) {
      if (r.kind === "tool" && r.input) {
        for (const k of [r.input.description || fromCut(r.input, "description"), r.input.command || fromCut(r.input, "command")]) if (k) ran.push(String(k).trim());
        if (ran.length > 40) ran.splice(0, ran.length - 40);
      }
      if (r.kind === "task" && wasStep(r.description)) continue;
      if (r.kind === "question") {
        if (r.id in answered) this.put(this.answerSummary(r.questions, answered[r.id]));
        else if (!(this.pendingIds || new Set()).has(r.id)) this.put(this.answerSummary(r.questions, null));
        else this.put(this.qPlaceholder(r.id, r.questions));   // still open: the card itself is in the dock
        continue;
      }
      if (r.kind === "user" && r.by === "app") this.resumeLine();
      else if (r.kind === "user") { this.userMsg(r.text, r.attachments); if (agenda && agendaDone(agenda)) agenda = null; }
      else if (r.kind === "waiting") this.waitLine(r, i === recs.length - 1 || recs.slice(i + 1).every((x) => x.kind !== "user"));
      else if (r.kind === "assistant") { const b = this.assistantBlock(); b.text = r.text; this.renderMd(b, true); this.cur = null; }
      else if (r.kind === "tool") this.toolCard(r);
      else if (r.kind === "tool_result") this.toolResult(r);
      else if (r.kind === "done") { this.closeSteps(); this.turnMeta(r); }
      else if (r.kind === "auto") { if (r.reason !== "steer") this.autoLine(); }
      else if (r.kind === "question_skipped") this.skippedLine(r.questions);
      else if (r.kind === "task") this.taskLine(r);
      else if (r.kind === "agenda") agenda = r;
      else if (r.kind === "steer") this.steerMsg(r.id, r.text, steerState[r.id] || (busy ? "queued" : "dropped"));
      else if (r.kind === "error") { this.closeSteps(); this.errorLine(r.text, i === recs.length - 1 ? this.lastUserText : null); }
    }
    if (!busy) this.closeSteps();
    this.replaying = false;
    this.setAgenda(agenda);
    this.scroll(true);
  }

  wire() {
    const ev = this.ws.ev;
    ev.on("agent.waiting", (e) => this.waitLine(e, true));
    ev.on("agent.user", (e) => {
      if (e.by === "app") { this.clearWait(); this.resumeLine(); this.sid = e.sid; return; }
      this.clearWait();
      if (e.text !== this.lastSent) this.userMsg(e.text, e.attachments);
      this.lastSent = null; this.sid = e.sid;
      if (this.agenda && agendaDone(this.agenda)) this.setAgenda(null);     // a new request: the finished plan goes
    });
    ev.on("agent.status", (e) => {
      this.phase = e.phase || this.phase;
      this.setBusy(e.busy, e.phase);
      if (e.busy) return;
      this.closeSteps();
      if ((e.seconds || 0) >= 30) native.notify("Claude finished", `${this.ws.p.name} · ${Math.round(e.seconds / 60) || 1} min`, this.pid);
      this.refreshSessions();
      for (const c of this.dock.querySelectorAll(".card-q")) this.questionDone({ id: c.dataset.q, answers: null });   // the turn ended
      this.setPending(0);
    });
    ev.on("agent.text_start", () => { this.cur = this.assistantBlock(); });
    ev.on("agent.text", (e) => { if (!this.cur) this.cur = this.assistantBlock(); this.cur.text += e.delta; this.renderMd(this.cur); });
    ev.on("agent.text_done", (e) => { if (!this.cur) this.cur = this.assistantBlock(); this.cur.text = e.text; this.renderMd(this.cur, true); this.cur = null; });
    ev.on("agent.tool", (e) => { this.cur = null; this.toolCard(e); });
    ev.on("agent.tool_result", (e) => this.toolResult(e));
    ev.on("agent.agenda", (e) => { if (!e.sid || e.sid === this.sid) this.setAgenda(e.agenda); });
    ev.on("agent.steer", (e) => this.steerEvent(e));
    ev.on("agent.permission", (e) => { this.permission(e); this.setPending(this.pending + 1);
      native.notify("Claude needs your OK", `${e.tool}: ${e.reason || "a command outside the safe list"}`, this.pid); native.attention(); });
    ev.on("agent.question", (e) => { this.question(e); this.setPending(this.pending + 1);
      native.notify("Claude has a question", ((e.questions || [])[0] || {}).question || "", this.pid); native.attention(); });
    ev.on("agent.auto", (e) => { this.cur = null; if (e.reason !== "steer") this.autoLine(); });
    ev.on("agent.question_skipped", (e) => this.skippedLine(e.questions));
    ev.on("agent.task", (e) => this.taskLine(e));
    ev.on("agent.question_done", (e) => { this.questionDone(e); this.setPending(this.pending - 1); });
    ev.on("agent.permission_done", (e) => {
      const c = this.el.querySelector(`[data-perm="${e.id}"]`);
      if (c) c.remove();
      this.put(h("div.noteline", icon(e.allow ? "check" : "x", 13), e.allow ? "You allowed it." : "You declined it."));
      this.setPending(this.pending - 1);
    });
    ev.on("agent.done", (e) => { this.closeSteps(); this.turnMeta(e); if (e.session_cost) this.costEl.textContent = `≈ $${e.session_cost.toFixed(2)}`; });
    ev.on("agent.error", (e) => { this.closeSteps(); if (e.message !== "stopped") this.errorLine(e.message, this.lastUserText); else this.put(h("div.noteline", icon("circle-stop", 13), "Stopped.")); });
  }

  setPending(n) { this.pending = Math.max(0, n); native.badge(this.pending ? String(this.pending) : ""); }

  async refreshSessions() {
    const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/sessions`).catch(() => null);
    if (r) { this.sessions = r.sessions; this.sid = r.current || this.sid; this.renderTitle(); }
  }

  scroll(force) {
    const m = this.msgs;
    if (force || m.scrollHeight - m.scrollTop - m.clientHeight < 160) m.scrollTop = m.scrollHeight;
  }

  // Anything but a step ends the burst of steps before it (which then folds into one line).
  put(el) {
    this.dropSuggest();
    if (!el.classList.contains("steps")) { this.closeSteps(); this.steps = null; }
    this.msgs.appendChild(el); this.scroll(); return el;
  }
  dropSuggest() { const s = this.msgs.querySelector(".suggest"); if (s) s.remove(); }

  userMsg(text, attachments) {
    this.lastUserText = text;
    const flags = (attachments || []).filter((a) => a && a.kind === "flag");
    if (flags.length) {
      this.put(h("div.msg.user.flags", h("div.row", { style: { gap: "6px", fontWeight: 600 } }, icon("flag", 14), `${flags.length} review flag${flags.length === 1 ? "" : "s"} sent`),
        h("div.col", { style: { gap: "3px", marginTop: "6px", fontSize: "12.5px" } }, flags.slice(0, 12).map((a) => h("a", { style: { color: "inherit", cursor: "pointer" },
          onclick: () => { const f = this.ws.review.get(a.id); if (f) this.ws.showFlag(f); } }, a.label))),
        flags.length > 12 ? h("div.small.muted", `and ${flags.length - 12} more`) : null));
      this.scroll(true);
      return;
    }
    const other = (attachments || []).filter((a) => a && a.kind !== "flag");
    this.put(h("div.msg.user", text, other.length ? h("div.att", other.map((a) => h("span.ctx", a.label || a.ref || a))) : null));
    this.scroll(true);
  }

  assistantBlock() {
    const md = h("div.md");
    const el = this.put(h("div.msg.assistant", md));
    return { el, md, text: "", t: 0 };
  }

  renderMd(b, final) {
    const now = performance.now();
    if (!final && now - b.t < 70) { clearTimeout(b.pending); b.pending = setTimeout(() => this.renderMd(b, true), 80); return; }
    b.t = now;
    b.md.innerHTML = markdown(b.text);
    if (final) for (const pre of b.md.querySelectorAll("pre")) pre.appendChild(btn("copy", null, { "data-tip": "Copy", onclick: () => copyText(pre.querySelector("code").textContent) }, "sm ghost copy"));
    this.scroll();
  }

  // ------------------------------------------------------------------ the steps Claude takes
  stepsBox() {
    if (this.steps && this.steps.isConnected && this.msgs.lastElementChild === this.steps) return this.steps;
    this.steps = h("div.steps.live");
    this.put(this.steps);
    return this.steps;
  }

  toolCard(e) {
    const name = toolName(e.name);
    if (HIDDEN.has(name)) { this.cards[e.id] = null; return; }
    const box = this.stepsBox();
    const [label, detail] = stepText(e.name, e.input);
    const body = h("div.step-b", h("div.label", "Input"), h("pre", JSON.stringify(e.input, null, 1)));
    const status = h("span.step-st", h("span.spinner", { style: { width: "11px", height: "11px", borderWidth: "1.5px" } }));
    const step = h("div.step.run", { "data-kind": KIND(name) }, h("div.step-h", { onclick: () => step.classList.toggle("open") },
      h("span.step-ic", icon(ICON[name] || "circle-dot", 13)), h("span.step-name", label),
      detail ? h("span.step-sum", detail) : null, h("span.step-time"), status), body);
    step._status = status; step._body = body; step._t = this.replaying ? 0 : performance.now();
    this.cards[e.id] = step;
    box.appendChild(step);
    this.foldLive(box);
    this.scroll();
  }

  // A burst in progress: the latest few steps, the earlier ones behind "N earlier steps".
  foldLive(box) {
    const steps = [...box.querySelectorAll(":scope > .step")];
    const hidden = Math.max(0, steps.length - LIVE_STEPS);
    steps.forEach((s, i) => s.classList.toggle("fold", i < hidden));
    let more = box.querySelector(":scope > .more");
    if (!hidden) { if (more) more.remove(); return; }
    if (!more) { more = h("div.more", { onclick: () => box.classList.toggle("unfold") }); box.insertBefore(more, box.firstChild); }
    clear(more).append(icon("chevron-right", 12), `${plural(hidden, "earlier step")}`);
  }

  // A finished burst folds into one line: how many steps of which kinds, any failures, the images it made.
  closeSteps(box) {
    box = box || this.steps;
    if (!box || !box.classList.contains("live")) return;
    box.classList.remove("live");
    const steps = [...box.querySelectorAll(":scope > .step")];
    const more = box.querySelector(":scope > .more");
    if (more) more.remove();
    steps.forEach((s) => s.classList.remove("fold"));
    if (steps.length <= 1) return;
    const kinds = {};
    for (const s of steps) kinds[s.dataset.kind] = (kinds[s.dataset.kind] || 0) + 1;
    const words = Object.entries(kinds).sort((a, b) => b[1] - a[1]).map(([k, n]) => k === "step" ? null : plural(n, k)).filter(Boolean).slice(0, 3);
    const errs = steps.filter((s) => s.classList.contains("err")).length;
    const imgs = [...box.querySelectorAll(".step-img img")].slice(-4);
    const sum = h("div.ssum", { onclick: () => box.classList.toggle("closed") },
      icon("chevron-right", 12), h("span.ss-n", plural(steps.length, "step")), words.length ? h("span.ss-k", words.join(", ")) : null,
      errs ? h("span.ss-err", icon("x", 11), plural(errs, "failed", "failed")) : null,
      imgs.length ? h("span.ss-imgs", imgs.map((im) => h("img", { src: im.src, onclick: (ev) => { ev.stopPropagation(); lightbox(im.src); } }))) : null);
    box.insertBefore(sum, box.firstChild);
    box.classList.add("closed");
  }

  toolResult(e) {
    const step = this.cards[e.id];
    if (!step) return;
    clear(step._status).appendChild(icon(e.is_error ? "x" : "check", 12));
    step._status.className = "step-st " + (e.is_error ? "err" : "ok");
    step.classList.remove("run");
    if (e.is_error) step.classList.add("err");
    if (step._t) {
      const s = (performance.now() - step._t) / 1000;
      if (s >= 2) step.querySelector(".step-time").textContent = s >= 90 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`;
    }
    step._body.append(h("div.label", { style: { marginTop: "8px" } }, "Result"), h("pre", e.text || ""));
    if (e.media) {
      const src = `/api/projects/${encodeURIComponent(this.pid)}/media/${e.media}`;
      step.insertBefore(h("div.step-img", h("img", { src, onclick: (ev) => { ev.stopPropagation(); lightbox(src); } })), step._body);
    }
    this.scroll();
  }

  // ------------------------------------------------------------------ the agenda
  setAgenda(ag) {
    this.agenda = ag && (ag.items || []).length ? ag : null;
    const el = this.agendaEl;
    clear(el);
    el.classList.toggle("hidden", !this.agenda);
    if (!this.agenda) { this.renderStatus(); return; }
    const items = this.agenda.items;
    const done = items.filter((i) => i.status === "done" || i.status === "skipped").length;
    const act = items.find((i) => i.status === "active");
    const all = done === items.length;
    const pct = Math.round(100 * done / items.length);
    const R = 8, C = 2 * Math.PI * R;
    const ring = h("span.ag-ring", { html: `<svg width="20" height="20" viewBox="0 0 20 20"><circle cx="10" cy="10" r="${R}" class="bg"/>`
      + `<circle cx="10" cy="10" r="${R}" class="fg" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - done / items.length)}"/></svg>` });
    const open = all && !this.busy ? !!this.agDoneOpen : this.agOpen;       // a finished plan folds to one line
    el.classList.toggle("open", open);
    el.classList.toggle("complete", all);
    el.append(h("div.ag-head", { onclick: () => {
      if (all && !this.busy) this.agDoneOpen = !open;
      else { this.agOpen = !open; localStorage.setItem("tw.agenda.open", this.agOpen ? "1" : "0"); }
      this.setAgenda(this.agenda); } },
      all ? h("span.ag-ring.full", icon("check", 12)) : ring,
      h("div.ag-t", h("b", this.agenda.title || "Claude's plan"), !open && act ? h("span.ag-now", act.text) : null),
      h("span.ag-count", all ? "done" : `${done} of ${items.length}`), icon(open ? "chevron-up" : "chevron-down", 13)),
      h("div.ag-bar", h("div.ag-fill", { style: { width: pct + "%" } })),
      open ? h("div.ag-list", items.map((it, i) => h("div.ag-item." + it.status,
        h("span.ag-box", it.status === "done" ? icon("check", 11) : it.status === "skipped" ? icon("circle-minus", 12) : it.status === "active" ? h("span.ag-pulse") : null),
        h("div.ag-txt", h("span", it.text), it.note ? h("div.ag-note", it.note) : null)))) : null);
    this.renderStatus();
  }

  // ------------------------------------------------------------------ notes sent while Claude works
  steerMsg(id, text, state) {
    const cap = h("div.steer-cap");
    const el = this.put(h("div.msg.user.steer", { "data-steer": id || "" }, h("div", text), cap));
    el._text = text;
    this.steerState(el, state);
    if (id) this.steerEls[id] = el;
    this.scroll(true);
    return el;
  }

  steerState(el, state) {
    const cap = el.querySelector(".steer-cap");
    el.dataset.state = state;
    clear(cap);
    if (state === "sending" || state === "queued") cap.append(h("span.spinner", { style: { width: "9px", height: "9px", borderWidth: "1.5px" } }), "Queued for Claude's next step");
    else if (state === "read") cap.append(icon("check-check", 12), "Read");
    else cap.append(icon("circle-alert", 12), "Not delivered",
      h("button.linkbtn", { onclick: () => { this.input.value = el._text; this.grow(); this.input.focus(); } }, "Send again"));
  }

  steerEvent(e) {
    let el = this.steerEls[e.id];
    if (!el && e.status === "queued") {
      el = [...this.msgs.querySelectorAll(".msg.steer[data-state=sending]")].find((x) => x._text === e.text);   // the one just sent here
      if (el) { el.dataset.steer = e.id; this.steerEls[e.id] = el; }
      else el = this.steerMsg(e.id, e.text, "queued");
    }
    if (el) this.steerState(el, e.status);
  }

  async steer(text) {
    const el = this.steerMsg(null, text, "sending");
    try {
      const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/chat/steer`, { body: { text } });
      if (!el.dataset.steer) { el.dataset.steer = r.id; this.steerEls[r.id] = el; }
      if (el.dataset.state === "sending") this.steerState(el, "queued");
    } catch (e) {
      if (e.status === 409 || /not working/i.test(e.message)) {             // it had just finished: a new message then
        el.remove();
        this.input.value = text; this.setBusy(false); this.send();
      } else { this.steerState(el, "dropped"); toast(e.message, "error"); }
    }
  }

  // ------------------------------------------------------------------ permissions and questions (docked above the composer)
  permission(e) {
    const inp = e.input || {};
    const what = e.tool === "Bash" ? inp.command : e.tool.startsWith("Write") || e.tool === "Edit" ? inp.file_path : JSON.stringify(inp, null, 1);
    const answer = async (allow, always) => {
      await api(`/api/projects/${encodeURIComponent(this.pid)}/chat/permission`, { body: { id: e.id, allow, always } }).catch((er) => toast(er.message, "error"));
    };
    this.dock.appendChild(h("div.card-perm", { "data-perm": e.id },
      h("div.ph", icon("shield-check", 15), `Claude wants to use ${e.tool}`),
      e.reason ? h("div.small.muted", { style: { marginTop: "3px" } }, e.reason) : null,
      h("pre", what || ""),
      h("div.row.wrap", h("button.btn.sm.primary", { onclick: () => answer(true, false) }, "Allow"),
        h("button.btn.sm", { onclick: () => answer(true, true) }, e.tool === "Bash" ? `Always allow ${String(inp.command || "").trim().split(/\s+/)[0]}` : "Always allow"),
        h("div.grow"), h("button.btn.sm.ghost.danger", { onclick: () => answer(false, false) }, "Deny"))));
    this.showChat();
  }

  qPlaceholder(id, qs) {
    return h("div.qplace", { "data-qp": id, onclick: () => { const c = this.dock.querySelector(`[data-q="${id}"]`); if (c) { c.classList.add("flash"); setTimeout(() => c.classList.remove("flash"), 700); } } },
      icon("message-square", 13), (qs || []).length > 1 ? `${qs.length} questions from Claude` : "A question from Claude", icon("arrow-down", 12));
  }

  // Claude's questions (AskUserQuestion): one at a time, options as cards (1-9 picks one), your own words
  // always possible; a single pick moves on to the next question, the last one sends the answers.
  question(e) {
    if (this.dock.querySelector(`[data-q="${e.id}"]`)) return;
    const qs = e.questions || [];
    if (!this.msgs.querySelector(`[data-qp="${e.id}"]`)) this.put(this.qPlaceholder(e.id, qs));
    const picked = qs.map(() => new Set());
    const own = qs.map((q) => h("input.qown", { placeholder: q.kind === "number" ? `A number${q.min !== undefined ? " from " + q.min : ""}${q.max !== undefined ? " to " + q.max : ""}`
      : q.kind === "text" || !(q.options || []).length ? "Your answer" : "Other", type: q.kind === "number" ? "number" : "text",
      onkeydown: (ev) => { if (ev.key === "Enter") { ev.preventDefault(); next(); } }, oninput: () => paint() }));
    let at = 0;
    const answered = (i) => picked[i].size > 0 || own[i].value.trim() !== "";
    const card = h("div.card-q", { "data-q": e.id, tabindex: "-1" });
    const body = h("div.qbody");
    const tabs = h("div.qtabs");
    const back = h("button.btn.sm.ghost", { onclick: () => { at = Math.max(0, at - 1); paint(); } }, icon("chevron-left", 13), "Back");
    const ok = h("button.btn.sm.primary", { onclick: () => next() });
    const submit = async (skip) => {
      const answers = {};
      if (!skip) qs.forEach((q, i) => {
        const mine = own[i].value.trim(), sel = [...picked[i]];
        if (q.multiSelect) { const all = mine ? [...sel, mine] : sel; if (all.length) answers[q.question] = all; }
        else if (mine) answers[q.question] = mine;
        else if (sel.length) answers[q.question] = sel[0];
      });
      if (!skip && !Object.keys(answers).length) { toast("Choose an option or type an answer", "warn"); return; }
      ok.disabled = true;
      try { await api(`/api/projects/${encodeURIComponent(this.pid)}/chat/answer`, { body: { id: e.id, answers } }); }
      catch (er) { toast(er.message, "error"); ok.disabled = false; }
    };
    const next = () => {
      if (!answered(at) && !qs[at].multiSelect) { toast("Choose an option or type an answer", "warn"); return; }
      if (at < qs.length - 1) { at++; paint(); } else submit(false);
    };
    const paint = () => {
      const q = qs[at] || {};
      clear(tabs);
      if (qs.length > 1) qs.forEach((x, i) => tabs.appendChild(h("button.qtab" + (i === at ? ".on" : "") + (answered(i) ? ".done" : ""), { onclick: () => { at = i; paint(); } },
        answered(i) ? icon("check", 10) : h("span.qn", String(i + 1)), x.header || `Question ${i + 1}`)));
      clear(body);
      body.append(q.header && qs.length === 1 ? h("span.qhead", q.header) : null,
        h("div.qtext", q.question, q.multiSelect ? h("span.qmulti", "pick any") : null));
      if ((q.options || []).length && q.kind !== "text" && q.kind !== "number") {
        const opts = h("div.qopts");
        (q.options || []).forEach((o, k) => { const rec = /\(recommended\)/i.test(o.label); opts.appendChild(h("button.qopt" + (q.multiSelect ? ".multi" : "") + (picked[at].has(o.label) ? ".on" : ""), { onclick: () => pick(k) },
          h("span.qkey", String(k + 1)), h("div.grow", h("div.qo-t", o.label.replace(/\s*\(recommended\)\s*/i, " ").trim(), rec ? h("span.qrec", "recommended") : null), o.description ? h("div.qo-d", o.description) : null,
            o.preview ? h("pre.qprev", o.preview) : null), h("span.qmark", icon("check", 11)))); });
        body.appendChild(opts);
      }
      body.appendChild(own[at]);
      back.style.visibility = at > 0 ? "visible" : "hidden";
      clear(ok).append(at < qs.length - 1 ? "Next" : qs.length > 1 ? "Send answers" : "Answer", icon(at < qs.length - 1 ? "arrow-right" : "send", 13));
    };
    const pick = (k) => {
      const q = qs[at], o = (q.options || [])[k];
      if (!o) return;
      const set = picked[at], on = set.has(o.label);
      if (!q.multiSelect) { set.clear(); own[at].value = ""; }
      on ? set.delete(o.label) : set.add(o.label);
      paint();
      if (!q.multiSelect && !on && at < qs.length - 1) setTimeout(() => { at++; paint(); }, 160);
    };
    card.addEventListener("keydown", (ev) => {
      if (ev.target.tagName === "INPUT") return;
      if (/^[1-9]$/.test(ev.key)) { pick(Number(ev.key) - 1); ev.preventDefault(); }
      else if (ev.key === "Enter") { next(); ev.preventDefault(); }
    });
    card.append(h("div.qtop", h("span.qbadge", icon("message-square", 13)), h("b", qs.length > 1 ? `${qs.length} questions` : "Question"),
      h("div.grow"), h("button.btn.sm.ghost", { onclick: () => submit(true), "data-tip": "Use Claude's recommendations" }, "Skip")),
      tabs, body, h("div.qfoot", back, h("div.grow"), h("span.tiny.faint", (qs[0] || {}).options ? "1-9 picks · ↩ next" : "↩ next"), ok));
    card._questions = qs;
    paint();
    this.dock.appendChild(card);
    this.showChat();
    setTimeout(() => { if (!this.input.value.trim() && document.activeElement !== this.input) card.focus({ preventScroll: true }); }, 60);
  }

  questionDone(e) {
    const card = this.dock.querySelector(`[data-q="${e.id}"]`);
    const qs = card ? card._questions : [];
    if (card) card.remove();
    const done = this.answerSummary(qs, e.answers);
    const place = this.msgs.querySelector(`[data-qp="${e.id}"]`);
    if (place) place.replaceWith(done); else this.put(done);
    this.scroll();
  }

  answerSummary(questions, answers) {
    const head = {};
    for (const q of questions || []) head[q.question] = q.header || q.question;
    if (!answers) return h("div.qdone.muted", h("div", "Not answered:"), (questions || []).map((q) => h("div", "· " + q.question)));
    const keys = Object.keys(answers);
    if (!keys.length) return h("div.qdone.muted", "Skipped. Claude used its recommendations.");
    const plain = (v) => String(v).replace(/\s*\(recommended\)\s*/i, " ").trim();
    return h("div.qdone", h("div.qd-h", icon("message-square", 12), "You answered"), keys.map((k) => h("div", h("span.muted", (head[k] || k) + ": "), h("b", Array.isArray(answers[k]) ? answers[k].map(plain).join(", ") : plain(answers[k])))));
  }

  showChat() { if (this.ws.chatPane && this.ws.chatPane.classList.contains("collapsed")) this.ws.toggleChat(); this.scroll(true); }

  turnMeta(e) {
    const bits = [];
    if (e.duration_ms) bits.push(e.duration_ms >= 90000 ? `${(e.duration_ms / 60000).toFixed(1)} min` : `${(e.duration_ms / 1000).toFixed(0)} s`);
    if (e.turns) bits.push(`${e.turns} steps`);
    if (e.cost) bits.push(`≈ $${e.cost.toFixed(2)}`);
    if (bits.length) this.put(h("div.turnmeta", bits.map((b) => h("span", b))));
  }

  renderMode(pr) {
    this.mode = pr.run_mode || "autonomous";
    const auto = this.mode === "autonomous";
    this.modeBtn.className = auto ? "modepill auto" : "modepill";
    clear(this.modeBtn).append(icon(auto ? "zap" : "messages-square", 12), auto ? "Autonomous" : "Step by step");
    this.modeBtn.dataset.tip = auto ? "Questions first, then Claude completes the design. Click for step by step."
      : "Claude waits for your approval after each stage. Click for autonomous.";
  }

  async toggleMode() {
    const next = this.mode === "autonomous" ? "check_in" : "autonomous";
    try { this.renderMode(await api(`/api/projects/${encodeURIComponent(this.pid)}`, { method: "PATCH", body: { run_mode: next } })); }
    catch (e) { toast(e.message, "error"); }
  }

  skippedLine(questions) {
    const qs = (questions || []).map((q) => q.header || q.question).filter(Boolean);
    this.put(h("div.noteline", icon("zap", 13), `Decided without asking (see docs/decisions.md): ${qs.join("; ")}`));
  }

  autoLine() { this.put(h("div.noteline", icon("refresh-cw", 13), "Resumed after background work")); }

  // an unattended run stopped by the account's usage limit: when it carries on (the line goes when it does)
  waitLine(r, live) {
    if (!r.text) { this.clearWait(); return; }
    this.clearWait();
    const el = h("div.noteline" + (live && r.until ? ".waiting" : ""), icon("clock", 13), r.text);
    this.put(el);
    if (live && r.until) this.waitEl = el;
  }

  clearWait() { if (this.waitEl) { this.waitEl.classList.remove("waiting"); this.waitEl = null; } }

  resumeLine() { this.put(h("div.noteline", icon("play", 13), "The usage limit has reset: carrying on")); }

  taskLine(e) {
    const txt = e.status === "running" ? `Running in the background: ${e.description}` : e.status === "completed" ? `Finished in the background: ${e.description}` : `Background work ${e.status}: ${e.description}`;
    this.put(h("div.noteline", icon(e.status === "running" ? "loader" : e.status === "completed" ? "check" : "x", 13), txt));
  }

  errorLine(text, retry) {
    const line = h("div.errline", icon("circle-alert", 14), h("span.grow", text));
    if (retry) line.appendChild(h("button.btn.sm", { onclick: (ev) => { if (this.busy) return; ev.currentTarget.remove(); this.input.value = retry; this.send(); } }, "Retry"));
    this.put(line); this.scroll(true);
  }

  setBusy(b, phase) {
    this.busy = !!b;
    if (phase) this.phase = phase;
    if (this.busy && !this.startedAt) this.startedAt = Date.now();
    if (!this.busy) { this.startedAt = null; this.phase = null; }
    if (this.agenda) this.setAgenda(this.agenda); else this.renderStatus();
    this.el.classList.toggle("working", this.busy);
    this.mcBtn.classList.toggle("hot", this.busy);
    this.stopBtn.style.display = this.busy ? "" : "none";
    this.input.placeholder = this.busy ? "Add a note for Claude" : "Message Claude";
    this.sendBtn.dataset.tip = this.busy ? "Send note" : "Send";
    this.hintEl.textContent = this.busy ? "↩ sends a note · ⌘. stops" : "⇧↩ new line";
    this.grow();
    if (this.ws.reviewOpen) this.ws.reviewPanel.render();
  }

  // What Claude is doing now: its phase, the agenda step it is on, how long it has been working.
  renderStatus() {
    clearInterval(this.tick);
    clear(this.status);
    this.status.classList.toggle("on", this.busy);
    if (!this.busy) return;
    const el = h("span.el");
    const upd = () => { const s = Math.round((Date.now() - this.startedAt) / 1000); el.textContent = s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s} s`; };
    upd(); this.tick = setInterval(upd, 1000);
    const ag = this.agenda, act = ag && ag.items.find((i) => i.status === "active");
    const idx = act ? ag.items.indexOf(act) + 1 : 0;
    const ph = (this.phase || "working").replace("mcp__tw__", "").replace(/^using /, "using ");
    this.status.append(h("span.st-dot"), act ? h("span.st-step", h("b", `Step ${idx} of ${ag.items.length}`), act.text) : null,
      h("span.st-ph", ph + "..."), el);
  }

  async stop() { await api(`/api/projects/${encodeURIComponent(this.pid)}/chat/interrupt`, { body: {} }).catch(() => {}); }

  setSelection(items, source) { this.sel = (items || []).map((it) => ({ ...it, source })); this.renderChips(); }
  setKicadSelection(items) { this.kicadSel = items || []; this.renderChips(); }

  renderChips() {
    clear(this.chips);
    const label = (it) => it.ref ? it.ref + (it.pad ? "." + it.pad : "") : it.net ? it.net.split("/").pop() : it.label || it.kind;
    const many = this.sel.length > 4;
    for (const it of many ? this.sel.slice(0, 3) : this.sel) this.chips.appendChild(h("span.chip", icon(it.net ? "cable" : it.source === "schematic" ? "waypoints" : "microchip", 12), label(it),
      h("button", { "data-tip": "Leave out", onclick: () => { this.sel = this.sel.filter((x) => x !== it); this.renderChips(); } }, icon("x", 11))));
    if (many) this.chips.appendChild(h("span.chip", { "data-tip": this.sel.slice(3).map(label).join(", ") }, `+${this.sel.length - 3} more`,
      h("button", { "data-tip": "Leave them all out", onclick: () => { this.sel = []; this.renderChips(); } }, icon("x", 11))));
    if (this.kicadSel.length) this.chips.appendChild(h("span.chip.kicad", { "data-tip": "Selected in KiCad (Claude sees this)" }, icon("zap", 12),
      "KiCad: " + this.kicadSel.slice(0, 6).map(label).join(", ") + (this.kicadSel.length > 6 ? "..." : "")));
  }

  prefill(text) { this.input.value = text; this.grow(); this.input.focus(); this.input.setSelectionRange(text.length, text.length); }

  async send() {
    const text = this.input.value.trim();
    if (!text) return;
    if (this.busy) { this.input.value = ""; this.grow(); this.steer(text); return; }
    native.askNotify();
    const attachments = this.sel.map((it) => ({ label: (it.ref ? it.ref : it.net ? "net " + it.net : it.label) + (it.source ? ` (${it.source})` : ""), ...it }));
    this.input.value = ""; this.grow();
    this.lastSent = text;
    this.userMsg(text, attachments);
    if (this.agenda && agendaDone(this.agenda)) this.setAgenda(null);
    this.setBusy(true, "starting");
    try {
      const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/chat`, { body: { text, sid: this.sid, attachments } });
      this.sid = r.sid;
      this.sel = []; this.renderChips();
    } catch (e) { this.setBusy(false); this.errorLine(e.message, text); }
  }
}

function agendaDone(ag) { return (ag.items || []).every((i) => i.status === "done" || i.status === "skipped"); }
