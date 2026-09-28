// Mission Control: the full-window view of Claude working on its own. The board as it changes
// (parts gliding into place, the router drawing copper in), Claude's agenda, what it is doing now,
// a feed of its steps, and the numbers as they move: time, steps, parts placed, nets routed, checks,
// stages. Notes and Stop from here reach Claude the same way as from the chat.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { Film, partShapes, applyFrame } from "./timelapse.js";
import { stepText, KIND, ICON, toolName } from "./chat.js";

const KINDS = [["command", "Commands", "terminal"], ["board edit", "Board edits", "move"], ["check run", "Checks", "list-checks"], ["view", "Looks", "image"],
  ["read", "Reads", "file-text"], ["query", "Queries", "circuit-board"], ["file edit", "File edits", "pencil"], ["lookup", "Lookups", "search"], ["search", "Searches", "search"], ["step", "Other", "circle-dot"]];
const HIDE = new Set(["agenda", "AskUserQuestion"]);
const fmtClock = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60), h2 = Math.floor(m / 60); return h2 ? `${h2}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`; };

// A number that counts to its new value.
class Counter {
  constructor(el, fmt = (v) => String(Math.round(v))) { this.el = el; this.fmt = fmt; this.v = 0; this.el.textContent = fmt(0); }
  set(v) {
    v = Number(v) || 0;
    if (v === this.target) return;
    this.target = v;
    if (document.hidden) { this.v = v; this.el.textContent = this.fmt(v); return; }      // no frames while hidden
    const from = this.v, t0 = performance.now();
    cancelAnimationFrame(this.raf);
    const step = (now) => {
      const u = Math.min(1, (now - t0) / 450), e = 1 - Math.pow(1 - u, 3);
      this.v = from + (v - from) * e; this.el.textContent = this.fmt(this.v);
      if (u < 1) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }
}

export class MissionControl {
  constructor(ws) {
    this.ws = ws; this.pid = ws.pid;
    this.onKey = (e) => { if (e.key === "Escape" && e.target.tagName !== "TEXTAREA") { e.preventDefault(); this.close(); } };
  }

  get isOpen() { return !!this.el; }

  async open() {
    if (this.el) return;
    this.st = null; this.stats = []; this.queue = []; this.playing = false; this.nframes = 0; this.loaded = false;
    this.reset();
    this.build();
    document.body.appendChild(this.el);
    requestAnimationFrame(() => this.el.classList.add("on"));
    addEventListener("keydown", this.onKey, true);
    this.ro = new ResizeObserver(() => { this.drawBoard(); this.spark(); });
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
    setTimeout(() => el.remove(), 220);
  }

  // a new run: its counters start again (the board picture carries on)
  reset() {
    this.feed = []; this.kinds = {}; this.steps = 0; this.errors = 0; this.cards = {}; this.said = ""; this.endedAt = null;
    this.startedAt = this.ws.chat && this.ws.chat.startedAt ? this.ws.chat.startedAt / 1000 : Date.now() / 1000;
    if (this.c) { this.c.steps.set(0); this.c.errs.set(0); }
  }

  // ------------------------------------------------------------------ layout
  build() {
    const card = (title, ic, ...kids) => h("section.mc-card", h("div.mc-ch", icon(ic, 13), h("span", title)), ...kids);
    this.canvas = h("canvas.mc-canvas");
    this.film = new Film(this.canvas, [16, 16, 16, 86]);
    this.orb = h("div.mc-orb", h("i"), h("i"), h("i"));
    this.phaseEl = h("div.mc-phase");
    this.stepEl = h("div.mc-step");
    this.saidEl = h("div.mc-said");
    this.agEl = h("div.mc-agenda");
    this.agRing = h("div.mc-ring");
    this.feedEl = h("div.mc-feed");
    this.kindsEl = h("div.mc-kinds");
    this.checksEl = h("div.mc-checks");
    this.stagesEl = h("div.mc-stages");
    this.banner = h("div.mc-banner");
    this.sparkEl = h("canvas.mc-spark");
    const big = (cls) => h("b." + cls);
    this.n = {
      clock: big("mc-clock"), steps: big("mc-num"), rate: big("mc-num"), errs: big("mc-num"),
      on: big("mc-bnum"), tracks: big("mc-bnum"), vias: big("mc-bnum"), len: big("mc-bnum"), routed: big("mc-bnum"),
    };
    this.c = { steps: new Counter(this.n.steps), errs: new Counter(this.n.errs), on: new Counter(this.n.on), tracks: new Counter(this.n.tracks),
      vias: new Counter(this.n.vias), len: new Counter(this.n.len, (v) => `${Math.round(v)}`), routed: new Counter(this.n.routed) };
    this.partsOf = h("small"); this.netsOf = h("small"); this.meter = h("i");
    this.input = h("textarea.mc-input", { rows: 1, placeholder: "Add a note for Claude",
      onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); } } });
    this.stopBtn = h("button.btn.mc-stop", { onclick: () => this.ws.chat.stop() }, icon("square", 12), "Stop");
    this.el = h("div.mc-overlay",
      h("div.mc-win",
        h("header.mc-head", h("div.mc-live", h("span.mc-dot"), "MISSION CONTROL"), h("div.mc-title", h("b", this.ws.p.name), this.modeEl = h("span.mc-mode")),
          h("div.grow"), h("label.mc-auto", { "data-tip": "Open automatically during autonomous runs" },
            h("input", { type: "checkbox", checked: localStorage.getItem("tw.mission.auto") !== "0", onchange: (e) => localStorage.setItem("tw.mission.auto", e.target.checked ? "1" : "0") }),
            "Open for autonomous runs"),
          h("div.mc-elapsed", icon("clock", 13), this.n.clock),
          btn("x", null, { onclick: () => this.close(), "data-tip": "Back to the workspace", "data-kbd": "esc" }, "sm ghost")),
        h("div.mc-grid",
          h("div.mc-col",
            card("Claude's plan", "list-todo", h("div.mc-planhead", this.agRing, this.agTitle = h("div.mc-agtitle")), this.agEl),
            card("This run", "activity",
              h("div.mc-nums", h("div", this.n.steps, h("span", "steps")), h("div", this.n.rate, h("span", "per minute")), h("div", this.n.errs, h("span", "failed"))),
              this.kindsEl)),
          h("div.mc-center",
            h("div.mc-board", this.canvas, h("div.mc-live2", h("span.mc-dot"), "LIVE BOARD"),
              h("div.mc-bstats",
                h("div", h("span", "Parts placed"), h("div", this.n.on, this.partsOf)), h("div", h("span", "Tracks"), this.n.tracks),
                h("div", h("span", "Vias"), this.n.vias), h("div", h("span", "Copper"), h("div", this.n.len, h("small", " mm"))),
                h("div.mc-routed", h("span", "Nets routed"), h("div", this.n.routed, this.netsOf), h("div.mc-meter", this.meter), this.sparkEl)))),
          h("div.mc-col",
            h("section.mc-card.mc-now", h("div.mc-nowtop", this.orb, h("div.grow", this.phaseEl, this.stepEl)), this.saidEl),
            card("Activity", "history", this.feedEl),
            card("Checks and stages", "list-checks", this.checksEl, this.stagesEl))),
        h("footer.mc-foot", this.banner, h("div.mc-compose", icon("message-square", 15), this.input,
          h("button.btn.primary", { onclick: () => this.send() }, icon("send", 13), "Send"), this.stopBtn))));
  }

  // ------------------------------------------------------------------ data
  async load() {
    const enc = encodeURIComponent(this.pid);
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
    for (const fr of tl.frames || []) { st = applyFrame(st, fr); if (fr.t >= this.startedAt - 5) this.stats.push(fr.stats || {}); }
    this.st = st;
    if (st) this.film.fit(st);
    this.drawBoard(); this.renderBoardStats(); this.spark();
    if (pr) { this.project(pr); }
    this.busy = sess ? !!sess.busy : false;
    if (sess && sess.current) {
      const s = await api(`/api/projects/${enc}/sessions/${sess.current}`).catch(() => null);
      if (s && this.el) this.backfill(s.transcript || []);
    }
    this.setAgenda(this.ws.chat && this.ws.chat.agenda);
    this.phase(this.busy ? (this.ws.chat && this.ws.chat.phase) || "working" : null);
    this.clock();
  }

  // the run so far, from the conversation (Mission Control may open part way through)
  backfill(recs) {
    let start = 0;
    for (let i = recs.length - 1; i >= 0; i--) if (recs[i].kind === "user") { start = i; break; }
    const run = recs.slice(start);
    if (run[0] && run[0].t) this.startedAt = Math.min(this.startedAt, run[0].t);
    this.endedAt = null;
    for (const r of run) {
      if ((r.kind === "done" || r.kind === "error") && r.t) this.endedAt = r.t;
      if (r.kind === "tool") this.addStep(r, r.t);
      else if (r.kind === "tool_result") this.stepDone(r);
      else if (r.kind === "assistant") this.say(r.text);
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
      ev.on("agent.status", (e) => { this.busy = !!e.busy; this.endedAt = e.busy ? null : Date.now() / 1000; this.phase(e.busy ? e.phase : null, e); }),
      ev.on("agent.user", () => { this.reset(); this.startedAt = Date.now() / 1000; clear(this.feedEl); this.renderKinds(); this.saidEl.textContent = ""; }),
      ev.on("agent.question", () => this.bannerSay("question", "Claude has a question", "Answer", () => { this.close(); this.ws.chat.showChat(); })),
      ev.on("agent.question_done", () => this.bannerSay(null)),
      ev.on("agent.permission", () => this.bannerSay("question", "Claude needs your approval", "Open chat", () => { this.close(); this.ws.chat.showChat(); })),
      ev.on("agent.permission_done", () => this.bannerSay(null)),
      ev.on("agent.done", (e) => { if (e.session_cost) this.cost = e.session_cost; }),
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
    this.stats.push(this.st.stats || {});
    this.renderBoardStats(); this.spark();
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
    const kind = KIND(name);
    this.steps++; this.kinds[kind] = (this.kinds[kind] || 0) + 1;
    this.c.steps.set(this.steps);
    this.renderKinds();
    const when = t ? new Date(t * 1000) : new Date();
    const st = h("span.mc-fst", h("span.spinner", { style: { width: "10px", height: "10px", borderWidth: "1.5px" } }));
    const row = h("div.mc-fi.run", h("span.mc-fic", icon(ICON[name] || "circle-dot", 13)),
      h("div.grow", h("div.mc-fl", label), detail ? h("div.mc-fd", detail) : null), h("span.mc-ft", when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })), st);
    row._st = st;
    this.cards[e.id] = row;
    this.feedEl.prepend(row);
    while (this.feedEl.children.length > 80) this.feedEl.lastChild.remove();
    this.stepEl.textContent = label;
  }

  stepDone(e) {
    const row = this.cards[e.id];
    if (!row) return;
    row.classList.remove("run");
    clear(row._st).appendChild(icon(e.is_error ? "x" : "check", 12));
    row._st.className = "mc-fst " + (e.is_error ? "err" : "ok");
    if (e.is_error) { row.classList.add("err"); this.errors++; this.c.errs.set(this.errors); }
  }

  say(text) {
    this.live = "";
    if (!text || !text.trim()) return;
    this.said = text;
    this.saidEl.textContent = lastLines(text);
    this.saidEl.classList.remove("in"); void this.saidEl.offsetWidth; this.saidEl.classList.add("in");
  }

  renderKinds() {
    clear(this.kindsEl);
    const max = Math.max(1, ...Object.values(this.kinds));
    for (const [k, label, ic] of KINDS) {
      const n = this.kinds[k] || 0;
      if (!n) continue;
      this.kindsEl.appendChild(h("div.mc-kind", icon(ic, 12), h("span.mc-kl", label), h("div.mc-kbar", h("i", { style: { width: `${Math.max(4, 100 * n / max)}%` } })), h("b", String(n))));
    }
    if (!this.kindsEl.children.length) this.kindsEl.appendChild(h("div.mc-empty", "No steps yet"));
  }

  renderBoardStats() {
    const s = (this.st && this.st.stats) || {};
    this.c.on.set(s.on || 0); this.c.tracks.set(s.tracks || 0); this.c.vias.set(s.vias || 0); this.c.len.set(s.len || 0); this.c.routed.set(s.routed || 0);
    this.partsOf.textContent = s.parts ? ` of ${s.parts}` : "";
    this.netsOf.textContent = s.nets ? ` of ${s.nets}` : "";
    this.meter.style.width = s.nets ? `${Math.round(100 * (s.routed || 0) / s.nets)}%` : "0%";
  }

  // nets routed across this run
  spark() {
    const cv = this.sparkEl;
    if (!cv || !this.el) return;
    const r = cv.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.max(1, Math.round(r.width * dpr)); cv.height = Math.max(1, Math.round(r.height * dpr));
    const c = cv.getContext("2d"), pts = this.stats.filter((s) => s.nets).map((s) => s.routed / s.nets);
    c.clearRect(0, 0, cv.width, cv.height);
    if (pts.length < 2) return;
    const W = cv.width, H = cv.height, n = pts.length;
    const X = (i) => (i / (n - 1)) * W, Y = (v) => H - 2 * dpr - v * (H - 4 * dpr);
    const grad = c.createLinearGradient(0, 0, 0, H); grad.addColorStop(0, "rgba(67,194,131,.35)"); grad.addColorStop(1, "rgba(67,194,131,0)");
    c.beginPath(); pts.forEach((v, i) => (i ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v)))); c.lineTo(W, H); c.lineTo(0, H); c.closePath(); c.fillStyle = grad; c.fill();
    c.beginPath(); pts.forEach((v, i) => (i ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v)))); c.strokeStyle = "#43c283"; c.lineWidth = 1.5 * dpr; c.stroke();
  }

  setAgenda(ag) {
    clear(this.agEl); clear(this.agRing); this.agTitle.textContent = "";
    const items = (ag && ag.items) || [];
    if (!items.length) { this.agEl.appendChild(h("div.mc-empty", "No agenda yet")); this.agRing.append(ring(0, 0)); return; }
    const done = items.filter((i) => i.status === "done" || i.status === "skipped").length;
    this.agRing.append(ring(done, items.length));
    this.agTitle.append(h("b", ag.title || "The plan"), h("span", `${done} of ${items.length} done`));
    for (const it of items) this.agEl.appendChild(h("div.mc-ag." + it.status,
      h("span.mc-agb", it.status === "done" ? icon("check", 11) : it.status === "skipped" ? icon("circle-minus", 11) : it.status === "active" ? h("span.mc-agp") : null),
      h("div.grow", h("div", it.text), it.note ? h("div.mc-agn", it.note) : null)));
    const act = items.find((i) => i.status === "active");
    if (act) this.stepEl.textContent = act.text;
  }

  phase(ph, e) {
    this.el && this.el.classList.toggle("idle", !this.busy);
    this.stopBtn.style.display = this.busy ? "" : "none";
    this.input.placeholder = this.busy ? "Add a note for Claude" : "Message Claude";
    if (!this.busy) {
      this.phaseEl.textContent = e && e.seconds ? `Finished in ${fmtClock(e.seconds)}` : "Idle";
      this.orb.classList.add("done");
      return;
    }
    this.orb.classList.remove("done");
    this.phaseEl.textContent = String(ph || "working").replace("mcp__tw__", "").replace(/^\w/, (m) => m.toUpperCase()) + "...";
  }

  project(pr) {
    this.modeEl.textContent = pr.run_mode === "autonomous" ? (pr.unattended ? "Autonomous" : "Autonomous (intake)") : "Check in";
    this.renderStages(pr.stages || []);
    clear(this.checksEl);
    const ch = pr.checks;
    if (!ch) { this.checksEl.appendChild(h("div.mc-empty", "No checks run yet.")); return; }
    const k = ch.counts || {};
    const pill = (cls, n, label) => h("div.mc-pill." + cls, h("b", String(n || 0)), h("span", label));
    const v = ch.verdict || "";
    this.checksEl.append(h("div.mc-verdict." + (v === "checks pass" ? "ok" : v === "not ready" ? "bad" : "warn"), v ? v[0].toUpperCase() + v.slice(1) : "Checked"),
      h("div.mc-pills", pill("err", k.error, "errors"), pill("warn", k.warning, "warnings"), pill("na", k.info, "notes")));
  }

  renderStages(stages) {
    clear(this.stagesEl);
    for (const s of stages || []) this.stagesEl.appendChild(h("span.mc-stage." + (s.status || "todo"), { "data-tip": s.note || s.title }, s.status === "done" ? icon("check", 10) : null, s.title));
  }

  bannerSay(kind, text, action, run) {
    clear(this.banner);
    this.banner.className = "mc-banner" + (kind ? " on " + kind : "");
    if (!kind) return;
    this.banner.append(icon("message-square", 14), h("b", text), h("div.grow"), h("button.btn.sm.primary", { onclick: run }, action));
  }

  clock() {
    if (!this.el) return;
    const s = (this.busy || !this.endedAt ? Date.now() / 1000 : this.endedAt) - this.startedAt;
    this.n.clock.textContent = fmtClock(s);
    const min = Math.max(1 / 60, s / 60);
    this.n.rate.textContent = this.steps ? (this.steps / min).toFixed(this.steps / min < 10 ? 1 : 0) : "0";
  }

  send() {
    const text = this.input.value.trim();
    if (!text) return;
    this.input.value = "";
    const chat = this.ws.chat;
    if (chat.busy) chat.steer(text);
    else { chat.input.value = text; chat.send(); }
    toast(chat.busy ? "Note sent" : "Sent to Claude", "ok", 2200);
  }
}

// The end of Claude's latest text as plain lines: links become their text, list and heading marks go.
function lastLines(text) {
  const t = String(text || "").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/^\s*[-*+]\s+/gm, "· ")
    .replace(/^\s*#+\s*/gm, "").replace(/[*_`>]/g, "").replace(/\n{2,}/g, "\n").trim();
  return t.length > 420 ? "…" + t.slice(-420).replace(/^\S*\s/, "") : t;
}

function ring(done, total) {
  const R = 22, C = 2 * Math.PI * R, f = total ? done / total : 0;
  return h("div.mc-ringw", { html: `<svg width="56" height="56" viewBox="0 0 56 56"><circle cx="28" cy="28" r="${R}" class="bg"/><circle cx="28" cy="28" r="${R}" class="fg" stroke-dasharray="${C}" stroke-dashoffset="${C * (1 - f)}"/></svg>` },
    h("b", total ? `${Math.round(100 * f)}%` : "–"));
}
