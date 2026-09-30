// The run monitor: the whole window for a run Claude does on its own. What it is doing now, whether it
// needs you, the plan and stages, the board as it changes (parts moving into place, the router drawing
// copper in), the recent steps with any that failed, the checks, and the time and usage so far. Notes
// and Stop from here reach Claude the same way as from the chat.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { Film, partShapes, applyFrame } from "./timelapse.js";
import { stepText, ICON, toolName } from "./chat.js";

const HIDE = new Set(["agenda", "AskUserQuestion"]);

// "1 h 8 min", "4 min 12 s", "38 s"
export function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), hr = Math.floor(m / 60);
  if (hr) return `${hr} h ${m % 60} min`;
  if (m) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${s} s`;
}

export class MissionControl {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.onKey = (e) => { if (e.key === "Escape" && e.target.tagName !== "TEXTAREA") { e.preventDefault(); this.close(); } };
  }

  get isOpen() { return !!this.el; }

  async open() {
    if (this.el) return;
    this.st = null; this.queue = []; this.playing = false; this.nframes = 0; this.loaded = false; this.waiting = null; this.pending = 0;
    this.reset();
    this.build();
    document.body.appendChild(this.el);
    requestAnimationFrame(() => this.el.classList.add("on"));
    addEventListener("keydown", this.onKey, true);
    this.ro = new ResizeObserver(() => this.drawBoard());
    this.ro.observe(this.canvas);
    this.wire();
    this.tick = setInterval(() => this.clock(), 1000);
    await this.load();
  }

  close() {
    if (!this.el) return;
    removeEventListener("keydown", this.onKey, true);
    this.ro.disconnect();
    clearInterval(this.tick);
    cancelAnimationFrame(this.anim);
    for (const off of this.off || []) off();
    this.el.classList.remove("on");
    const el = this.el; this.el = null;
    this.dismissed = this.turn;                         // not again for this run
    setTimeout(() => el.remove(), 180);
  }

  // a new run: its steps start again (the board picture carries on)
  reset() {
    this.cards = {}; this.steps = 0; this.errors = 0; this.said = ""; this.endedAt = null;
    this.startedAt = this.ws.chat && this.ws.chat.startedAt ? this.ws.chat.startedAt / 1000 : Date.now() / 1000;
  }

  // the plan's usage limit (tracewright/usage.py): how much of the window is used and when it resets
  plan(d) {
    if (!this.planWrap) return;
    this.planWrap.hidden = !d;
    if (!d) return;
    this.planEl.textContent = d.label.charAt(0).toUpperCase() + d.label.slice(1);
    this.planWrap.className = "mc-plan" + (d.status === "rejected" ? " bad" : d.status === "allowed_warning" || (d.used || 0) >= 0.8 ? " warn" : "");
  }

  // ------------------------------------------------------------------ layout
  build() {
    const card = (title, ...kids) => h("section.mc-card", h("div.mc-ch", h("span", title)), ...kids);
    this.canvas = h("canvas.mc-canvas");
    this.film = new Film(this.canvas, [16, 16, 16, 16]);
    this.statusEl = h("span.mc-status");
    this.elapsedEl = h("span.mc-meta-v");
    this.costEl = h("span.mc-meta-v");
    this.planEl = h("span.mc-meta-v");
    this.planWrap = h("span.mc-plan", { hidden: true, "data-tip": "Your Claude plan's usage limit, as Claude Code last reported it. Runs here share it with Claude Code anywhere else." },
      icon("hourglass", 13), this.planEl);
    this.phaseEl = h("div.mc-phase");
    this.stepEl = h("div.mc-step");
    this.saidEl = h("div.mc-said");
    this.agEl = h("div.mc-agenda");
    this.agHead = h("div.mc-planhead");
    this.feedEl = h("div.mc-feed");
    this.failEl = h("span.mc-fails");
    this.checksEl = h("div.mc-checks");
    this.stagesEl = h("div.mc-stages");
    this.bline = h("div.mc-bline");
    this.banner = h("div.mc-banner");
    this.input = h("textarea.mc-input", { rows: 1, placeholder: "Add a note for Claude",
      onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); } } });
    this.stopBtn = btn("square", "Stop", { onclick: () => this.ws.chat.stop() }, "sm mc-stop");
    this.el = h("div.mc-overlay",
      h("div.mc-win",
        h("header.mc-head",
          h("div.mc-title", h("b", this.ws.p.name), this.statusEl),
          h("div.mc-meta", h("span", icon("clock", 13), this.elapsedEl), h("span", { "data-tip": "Usage of your Claude account in this conversation" }, icon("gauge", 13), this.costEl), this.planWrap),
          h("div.grow"),
          h("label.mc-auto", { "data-tip": "Open this view when an autonomous run starts" },
            h("input", { type: "checkbox", checked: localStorage.getItem("tw.mission.auto") !== "0", onchange: (e) => localStorage.setItem("tw.mission.auto", e.target.checked ? "1" : "0") }),
            "Open for autonomous runs"),
          this.stopBtn,
          btn("layout-grid", "Workspace", { onclick: () => this.close(), "data-tip": "Back to the workspace", "data-kbd": "esc" }, "sm")),
        h("div.mc-grid",
          h("div.mc-col",
            card("Plan", this.agHead, this.agEl),
            card("Stages", this.stagesEl)),
          h("div.mc-center",
            h("div.mc-board", this.canvas),
            this.bline),
          h("div.mc-col",
            this.banner,
            h("section.mc-card.mc-now", h("div.mc-ch", h("span", "Now")), this.phaseEl, this.stepEl, this.saidEl),
            h("section.mc-card.mc-steps", h("div.mc-ch", h("span", "Recent steps"), h("div.grow"), this.failEl), this.feedEl),
            card("Checks", this.checksEl))),
        h("footer.mc-foot", h("div.mc-compose", this.input,
          h("button.btn.primary.sm", { onclick: () => this.send() }, icon("send", 13), "Send")))));
  }

  // ------------------------------------------------------------------ data
  async load() {
    const enc = encodeURIComponent(this.pid);
    api("/api/usage").then((d) => this.plan(d.plan)).catch(() => {});
    const [tl, board, pr, sess] = await Promise.all([
      api(`/api/projects/${enc}/timelapse`).catch(() => ({ frames: [] })),
      api(`/api/projects/${enc}/board`).catch(() => null),
      api(`/api/projects/${enc}`).catch(() => null),
      api(`/api/projects/${enc}/sessions`).catch(() => null)]);
    if (!this.el) return;
    this.film.setShapes(partShapes(board));
    this.nframes = (tl.frames || []).length;
    this.loaded = true;
    let st = null;
    for (const fr of tl.frames || []) st = applyFrame(st, fr);
    this.st = st;
    if (st) this.film.fit(st);
    this.drawBoard(); this.renderBoardLine();
    if (pr) this.project(pr);
    this.busy = sess ? !!sess.busy : false;
    this.pending = sess ? (sess.pending || []).length : 0;
    const cur = sess && (sess.sessions || []).find((s) => s.sid === sess.current);
    if (cur && cur.cost != null) this.cost = cur.cost;
    if (sess && sess.current) {
      const s = await api(`/api/projects/${enc}/sessions/${sess.current}`).catch(() => null);
      if (s && this.el) this.backfill(s.transcript || []);
    }
    this.setAgenda(this.ws.chat && this.ws.chat.agenda);
    this.phase(this.busy ? (this.ws.chat && this.ws.chat.phase) || "working" : null);
    this.clock();
  }

  // the run so far, from the conversation (the view may open part way through)
  backfill(recs) {
    let start = 0;
    for (let i = recs.length - 1; i >= 0; i--) if (recs[i].kind === "user" && recs[i].by !== "app") { start = i; break; }
    const run = recs.slice(start);
    if (run[0] && run[0].t) this.startedAt = Math.min(this.startedAt, run[0].t);
    this.endedAt = null;
    for (const r of run) {
      if ((r.kind === "done" || r.kind === "error") && r.t) this.endedAt = r.t;
      if (r.kind === "tool") this.addStep(r, r.t);
      else if (r.kind === "tool_result") this.stepDone(r);
      else if (r.kind === "assistant") this.say(r.text);
      else if (r.kind === "waiting") this.waiting = r.until ? r : null;
      else if (r.kind === "user" && r.by === "app") this.waiting = null;
    }
  }

  wire() {
    const ev = this.ws.ev;
    this.off = [
      ev.on("agent.tool", (e) => this.addStep(e)),
      ev.on("agent.tool_result", (e) => this.stepDone(e)),
      ev.on("agent.text_done", (e) => this.say(e.text)),
      ev.on("agent.text", (e) => { this.live = (this.live || "") + (e.delta || ""); this.saidEl.textContent = lastLines(this.live); }),
      ev.on("agent.text_start", () => { this.live = ""; }),
      ev.on("agent.agenda", (e) => this.setAgenda(e.agenda)),
      ev.on("agent.status", (e) => { this.busy = !!e.busy; this.endedAt = e.busy ? null : Date.now() / 1000; if (e.busy) this.waiting = null; this.phase(e.busy ? e.phase : null, e); }),
      ev.on("agent.user", (e) => { if (e.by !== "app") { this.reset(); this.startedAt = Date.now() / 1000; clear(this.feedEl); this.saidEl.textContent = ""; this.renderFails(); } }),
      ev.on("agent.question", () => { this.pending++; this.bannerSay("Claude has a question", "Answer", () => { this.close(); this.ws.chat.showChat(); }); }),
      ev.on("agent.question_done", () => { this.pending = Math.max(0, this.pending - 1); if (!this.pending) this.bannerSay(null); this.status(); }),
      ev.on("agent.permission", () => { this.pending++; this.bannerSay("Claude needs your approval", "Open chat", () => { this.close(); this.ws.chat.showChat(); }); }),
      ev.on("agent.permission_done", () => { this.pending = Math.max(0, this.pending - 1); if (!this.pending) this.bannerSay(null); this.status(); }),
      ev.on("agent.waiting", (e) => { this.waiting = e.until ? e : null; this.status(); }),
      ev.on("agent.done", (e) => { if (e.session_cost != null) { this.cost = e.session_cost; this.clock(); } }),
      ev.on("usage.plan", (e) => this.plan(e)),
      ev.on("timelapse.frame", () => this.more()),
      ev.on("checks.done", () => api(`/api/projects/${encodeURIComponent(this.pid)}`).then((pr) => this.project(pr)).catch(() => {})),
      ev.on("stages", (e) => this.renderStages(e.stages)),
      ev.on("board.changed", () => this.reshape()),
    ];
  }

  // new frames: animate each in turn (a quick run of router frames plays through quickly)
  async more() {
    if (!this.loaded) return;                           // load() reads every frame so far
    if (this.fetching) { this.again = true; return; }
    this.fetching = true;
    try {
      const r = await api(`/api/projects/${encodeURIComponent(this.pid)}/timelapse?since=${this.nframes || 0}`);
      const frs = r.frames || [];
      this.nframes = (this.nframes || 0) + frs.length;
      this.queue = (this.queue || []).concat(frs);
      if (!this.playing) this.playQueue();
    } catch (e) { /* the next frame retries */ } finally {
      this.fetching = false;
      if (this.again) { this.again = false; this.more(); }
    }
  }

  playQueue() {
    const fr = (this.queue || []).shift();
    if (!fr || !this.el) { this.playing = false; return; }
    this.playing = true;
    const prev = this.st;
    this.st = applyFrame(prev, fr);
    this.renderBoardLine();
    const glow = { fp: new Set(Object.keys(fr.fp || {})), tr: new Set((fr["tr+"] || []).map((x) => x[0])), vi: new Set((fr["vi+"] || []).map((x) => x[0])) };
    if (fr.k) glow.fp.clear();
    const dur = Math.max(120, (fr.tmp ? 260 : 900) / Math.max(1, this.queue.length / 3));
    const t0 = performance.now();
    const step = (now) => {
      const u = Math.min(1, (now - t0) / dur);
      this.film.draw(this.st, { prev, t: Math.min(1, u * 1.5), glow, g: 1 - u * 0.5 });
      this.glow = glow;
      if (u < 1 && this.el) this.anim = requestAnimationFrame(step); else this.playQueue();
    };
    this.anim = requestAnimationFrame(step);
  }

  // parts added or changed shape: their outlines from the board as it is now
  async reshape() {
    clearTimeout(this.rs);
    this.rs = setTimeout(async () => {
      const b = await api(`/api/projects/${encodeURIComponent(this.pid)}/board`).catch(() => null);
      if (b && this.el) { this.film.setShapes(partShapes(b)); if (!this.playing) this.drawBoard(); }
    }, 600);
  }

  drawBoard() { if (this.st && !this.playing) this.film.draw(this.st, { glow: this.glow, g: 0.35 }); }

  // ------------------------------------------------------------------ the parts of the view
  addStep(e, t) {
    const name = toolName(e.name || "");
    if (HIDE.has(name) || this.cards[e.id]) return;
    const [label, detail] = stepText(e.name, e.input);
    this.steps++;
    const when = t ? new Date(t * 1000) : new Date();
    const st = h("span.mc-fst", h("span.spinner", { style: { width: "10px", height: "10px", borderWidth: "1.5px" } }));
    const row = h("div.mc-fi.run", h("span.mc-fic", icon(ICON[name] || "circle-dot", 13)),
      h("div.grow", h("div.mc-fl", label), detail ? h("div.mc-fd", detail) : null), h("span.mc-ft", when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), st);
    row._st = st;
    this.cards[e.id] = row;
    this.feedEl.prepend(row);
    while (this.feedEl.children.length > 60) this.feedEl.lastChild.remove();
    this.stepEl.textContent = label;
  }

  stepDone(e) {
    const row = this.cards[e.id];
    if (!row) return;
    row.classList.remove("run");
    clear(row._st).appendChild(icon(e.is_error ? "x" : "check", 12));
    row._st.className = "mc-fst " + (e.is_error ? "err" : "ok");
    if (e.is_error) {
      row.classList.add("err"); this.errors++; this.renderFails();
      if (e.text) row.querySelector(".grow").appendChild(h("div.mc-fe", String(e.text).split("\n").find((l) => l.trim()) || ""));
    }
  }

  renderFails() {
    this.failEl.textContent = this.errors ? `${this.errors} failed` : "";
    this.failEl.classList.toggle("on", !!this.errors);
  }

  say(text) {
    this.live = "";
    if (!text || !text.trim()) return;
    this.said = text;
    this.saidEl.textContent = lastLines(text);
  }

  // one line under the board: what is on it now
  renderBoardLine() {
    const s = (this.st && this.st.stats) || {};
    const bits = [];
    if (s.parts) bits.push(`${s.on || 0} of ${s.parts} parts placed`);
    if (s.tracks) bits.push(`${s.tracks} tracks`);
    if (s.vias) bits.push(`${s.vias} vias`);
    if (s.len) bits.push(`${Math.round(s.len)} mm of copper`);
    this.bline.textContent = bits.join(" · ") || "No board yet";
  }

  setAgenda(ag) {
    clear(this.agEl); clear(this.agHead);
    const items = (ag && ag.items) || [];
    if (!items.length) { this.agEl.appendChild(h("div.mc-empty", "No plan yet")); return; }
    const done = items.filter((i) => i.status === "done" || i.status === "skipped").length;
    this.agHead.append(h("div.mc-agtitle", h("b", ag.title || "Plan"), h("span", `${done} of ${items.length}`)),
      h("div.mc-bar", h("i", { style: { width: `${Math.round(100 * done / items.length)}%` } })));
    for (const it of items) this.agEl.appendChild(h("div.mc-ag." + it.status,
      h("span.mc-agb", it.status === "done" ? icon("check", 11) : it.status === "skipped" ? icon("circle-minus", 11) : it.status === "active" ? h("span.mc-agp") : null),
      h("div.grow", h("div", it.text), it.note ? h("div.mc-agn", it.note) : null)));
    const act = items.find((i) => i.status === "active");
    if (act && !this.stepEl.textContent) this.stepEl.textContent = act.text;
  }

  status() {
    if (!this.el) return;
    let cls = "idle", text = "Idle";
    if (this.busy && this.pending) { cls = "wait"; text = "Waiting for you"; }
    else if (this.busy) { cls = "run"; text = "Running"; }
    else if (this.waiting && this.waiting.until) {
      cls = "pause"; text = `Paused until ${new Date(this.waiting.until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} (usage limit)`;
    } else if (this.endedAt) { cls = "done"; text = "Finished"; }
    this.statusEl.className = "mc-status " + cls;
    this.statusEl.textContent = text;
    this.el.classList.toggle("idle", !this.busy);
  }

  phase(ph, e) {
    this.stopBtn.style.display = this.busy ? "" : "none";
    this.input.placeholder = this.busy ? "Add a note for Claude" : "Message Claude";
    this.status();
    if (!this.busy) {
      this.phaseEl.textContent = e && e.seconds ? `Finished after ${fmtDuration(e.seconds)}` : this.waiting ? "Waiting for the usage limit to reset"
        : this.endedAt ? `Finished at ${new Date(this.endedAt * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Not running";
      return;
    }
    this.phaseEl.textContent = String(ph || "working").replace("mcp__tw__", "").replace(/^using /, "").replace(/^\w/, (m) => m.toUpperCase());
  }

  project(pr) {
    this.renderStages(pr.stages || []);
    clear(this.checksEl);
    const ch = pr.checks;
    if (!ch) { this.checksEl.appendChild(h("div.mc-empty", "Not run yet")); return; }
    const k = ch.counts || {}, w = ch.waived || {};
    const waived = w.total != null ? w.total : (w.error || 0) + (w.warning || 0) + (w.info || 0);
    const v = ch.verdict || "";
    this.checksEl.append(h("div.mc-verdict." + (v === "checks pass" ? "ok" : v === "not ready" ? "bad" : "warn"), v ? v[0].toUpperCase() + v.slice(1) : "Checked"),
      h("div.mc-counts", `${k.error || 0} errors · ${k.warning || 0} warnings · ${k.info || 0} notes` + (waived ? ` · ${waived} waived` : "")));
  }

  renderStages(stages) {
    clear(this.stagesEl);
    for (const s of stages || []) this.stagesEl.appendChild(h("div.mc-stage." + (s.status || "todo"), { "data-tip": s.note || s.title },
      h("span.mc-sb", s.status === "done" ? icon("check", 10) : s.status === "active" ? h("span.mc-agp") : s.status === "blocked" ? icon("x", 10) : null),
      h("span.grow", s.title), s.note && s.status !== "todo" ? h("span.mc-sn", s.note) : null));
  }

  bannerSay(text, action, run) {
    clear(this.banner);
    this.banner.className = "mc-banner" + (text ? " on" : "");
    this.status();
    if (!text) return;
    this.banner.append(icon("message-square", 14), h("b", text), h("div.grow"), h("button.btn.sm.primary", { onclick: run }, action));
  }

  clock() {
    if (!this.el) return;
    const s = (this.busy || !this.endedAt ? Date.now() / 1000 : this.endedAt) - this.startedAt;
    this.elapsedEl.textContent = fmtDuration(s);
    this.costEl.textContent = this.cost != null ? `$${Number(this.cost).toFixed(2)}` : "—";
  }

  send() {
    const text = this.input.value.trim();
    if (!text) return;
    this.input.value = "";
    const chat = this.ws.chat;
    if (chat.busy) chat.steer(text);
    else { chat.input.value = text; chat.send(); }
    toast(chat.busy ? "Note sent" : "Sent", "ok", 2200);
  }
}

// The end of Claude's latest text as plain lines: links become their text, list and heading marks go.
function lastLines(text) {
  const t = String(text || "").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/^\s*[-*+]\s+/gm, "· ")
    .replace(/^\s*#+\s*/gm, "").replace(/[*_`>]/g, "").replace(/\n{2,}/g, "\n").trim();
  return t.length > 420 ? "…" + t.slice(-420).replace(/^\S*\s/, "") : t;
}
