// A project's workspace: Claude on the left; the board, schematic, 3D model, checks, docs, files,
// history and outputs on the right; the review flags in a panel beside them.
import { h, clear, api, toast, menu, Events, fmtTime, btn, modal, popover, confirmDialog, promptDialog } from "./util.js";
import { icon } from "./icons.js";
import { topbar, state, go, command, runCommand } from "./app.js";
import { native, isNative } from "./native.js";
import { Chat } from "./chat.js";
import { BoardView } from "./board.js";
import { SchematicView } from "./schematic.js";
import { ChecksPanel } from "./checks.js";
import { Viewer3D } from "./viewer3d.js";
import { BomView } from "./bom.js";
import { RulesView } from "./rules.js";
import { FilesPanel, HistoryPanel, OutputsPanel, DocsPanel } from "./panels.js";
import { OverviewPanel } from "./overview.js";
import { Review, ReviewPanel } from "./review.js";
import { Approvals } from "./approvals.js";
import { TimelapsePlayer } from "./timelapse.js";
import { MissionControl } from "./mission.js";
import { GuidedCanvas } from "./guided.js";
import { Inspector } from "./inspector.js";
import { limitsPanel } from "./limits.js";
import { SignoffView } from "./signoff.js";
import { SimulateView } from "./simulate.js";

const enc = encodeURIComponent;
export const TABS = [
  ["overview", "Overview", "layout-grid"], ["board", "Board", "circuit-board"], ["schematic", "Schematic", "waypoints"], ["3d", "3D", "box"],
  ["bom", "BOM", "list"], ["checks", "Checks", "list-checks"], ["signoff", "Sign-off", "badge-check"], ["outputs", "Order", "shopping-cart"], ["rules", "Rules", "sliders-horizontal"],
  ["simulate", "Simulate", "activity"], ["docs", "Docs", "file-text"], ["files", "Files", "folder"], ["history", "History", "history"],
];
// Six places, each holding one or more views (Design: the board, schematic and 3D model ...).
export const PLACES = [
  ["overview", "Overview", "layout-grid", ["overview"]],
  ["design", "Design", "circuit-board", ["board", "schematic", "3d"]],
  ["parts", "Parts", "list", ["bom", "outputs"]],
  ["checks", "Checks", "list-checks", ["checks", "signoff", "rules"]],
  ["simulate", "Simulate", "activity", ["simulate"]],
  ["project", "Project", "folder", ["docs", "files", "history"]],
];
const SUB = { board: "Board", schematic: "Schematic", "3d": "3D", bom: "BOM", outputs: "Order", checks: "Checks", signoff: "Sign-off", rules: "Design rules", simulate: "Simulate",
  docs: "Docs", files: "Files", history: "History" };
export const placeOf = (view) => (PLACES.find((p) => p[3].includes(view)) || PLACES[1])[0];
export const tabKey = (i) => i < 9 ? `mod+${i + 1}` : null;
const VIEWS = { overview: OverviewPanel, board: BoardView, schematic: SchematicView, "3d": Viewer3D, bom: BomView, checks: ChecksPanel, signoff: SignoffView, rules: RulesView, simulate: SimulateView,
  files: FilesPanel, history: HistoryPanel, outputs: OutputsPanel, docs: DocsPanel };
const STAGE_ICON = { done: "check", blocked: "x", active: null, todo: null, skipped: null };

export class Workspace {
  constructor(root, pid, tab) {
    this.root = root; this.pid = pid;
    this.tab = TABS.find((t) => t[0] === tab) ? tab : localStorage.getItem("tw.tab." + pid) || "overview";
    this.views = {};
    this.ev = new Events(pid);
    this.review = new Review(this);
    this.approvals = new Approvals(this);
    this.unreg = [];
    this.build();
  }

  destroy() {
    this.root.classList.remove("guided", "canvas-on", "guided-peek");
    this.ev.close();
    this.unreg.forEach((f) => f());
    for (const v of Object.values(this.views)) v && v.destroy && v.destroy();
    this.chat && this.chat.destroy();
    native.badge("");
  }

  async build() {
    try { this.p = await api(`/api/projects/${enc(this.pid)}`); }
    catch (e) {
      this.root.appendChild(topbar([], []));
      this.root.appendChild(h("div.page", h("div.page-inner", h("div.empty", h("div.eicon", icon("folder", 22)), h("h3", "Project not found"), h("p", e.message),
        h("div.row", h("button.btn.primary", { onclick: () => go("") }, "Back to projects"))))));
      return;
    }
    document.title = `${this.p.name} — Tracewright`;
    native.context(this.pid, this.p.name);
    this.nameEl = h("span.ellipsis", this.p.name);
    this.stageEl = h("button.stagechip", { onclick: (e) => this.stagesPop(e.currentTarget) });
    this.liveEl = h("span.kdot");
    this.checkEl = h("button.pill", { onclick: () => this.show("checks"), "data-tip": "Checks", "data-kbd": "mod+6" });
    const guided = !!this.p.start_phase;
    this.startBtn = guided ? btn("play", "Start design", { onclick: () => this.startDesign() }, "sm primary startbtn") : null;
    this.peekBtn = guided ? btn("layout-grid", "Workspace", { onclick: () => this.peek(), "data-tip": "Look at the full workspace" }, "sm ghost peekbtn") : null;
    const bar = topbar([
      h("div.crumbs-top", h("span.cs", icon("chevron-right", 14)),
        h("button.cbtn", { onclick: (e) => this.projectMenu(e.currentTarget), "data-tip": this.p.root }, this.nameEl, icon("chevron-down", 13))),
      this.stageEl,
    ], [
      this.startBtn, this.peekBtn,
      this.checkEl,
      state.info.server_mode ? btn("download", "Download", { onclick: () => this.download() }, "sm")
        : this.kicadBtn = h("div.kicadsplit",
          h("button.btn.sm.kicadbtn", { onclick: () => this.openKicad(this.p.has_pcb ? "board" : this.p.has_sch ? "schematic" : "project"),
            "data-kbd": "mod+shift+b" }, this.liveEl, icon("external-link", 13), h("span", "Open in KiCad")),
          h("button.btn.sm.kicadmore", { onclick: (e) => this.kicadMenu(e.currentTarget), "data-tip": "Board, schematic or project; the live link" }, icon("chevron-down", 12))),
    ]);
    this.root.appendChild(bar);
    const ws = h("div.ws");
    this.chatPane = h("div.chatpane");
    this.chatPane.style.width = (+localStorage.getItem("tw.chatw") || 420) + "px";
    if (localStorage.getItem("tw.chat.hidden") === "1") this.chatPane.classList.add("collapsed");
    const split = h("div.splitter");
    this.main = h("div.mainpane");
    ws.append(this.chatPane, split, this.main);
    this.root.appendChild(ws);
    this.dragSplit(split);
    this.tabsEl = h("div.tabs");
    this.body = h("div.tabbody");
    this.reviewPanel = new ReviewPanel(this);
    this.mainBody = h("div.main-body", this.body);
    this.main.append(this.tabsEl, this.mainBody);
    this.inspector = new Inspector(this);                     // one part picked anywhere: what it is and connects
    this.inspector.mount(this.mainBody);
    this.renderStages(this.p.stages);
    this.renderLive(this.p.live);
    this.renderChecks(this.p.checks);
    this.chat = new Chat(this.chatPane, this);
    this.renderTabs();
    this.review.on("changed", () => { this.renderTabs(); this.viewIf("board", (v) => v.flagsChanged && v.flagsChanged()); });
    this.show(this.tab);
    if (localStorage.getItem("tw.review.open") === "1") this.toggleReview(true);
    if (guided) this.enterGuided();
    this.wire();
    this.commands();
    this.watchMission();
  }

  // ------------------------------------------------------------------ commands (menus, shortcuts, palette)
  commands() {
    const c = (name, spec) => this.unreg.push(command(name, { group: "Project", ...spec }));
    PLACES.forEach(([id, label, ic, views], i) => c("place:" + id, { title: `Go to ${label}`, icon: ic, kbd: tabKey(i), group: "View",
      run: () => this.show(this.lastIn(id)) }));
    TABS.forEach(([id, label, ic]) => c("tab:" + id, { title: `Show ${label === "Order" ? "the order" : label === "3D" ? "the 3D model" : label}`, icon: ic, group: "View",
      run: () => this.show(id) }));
    c("calculators", { title: "Calculators", icon: "calculator", kbd: "mod+shift+c", group: "Tools", run: () => runCommand("calculators") });
    c("focus", { title: "Focus mode", icon: "focus", group: "View", run: () => this.focusMode() });
    c("limits", { title: "Design limits…", icon: "ruler", run: () => this.limitsDialog() });
    c("selftest", { title: "Run the check self-test", icon: "flask-conical", group: "Tools", run: () => this.selftest() });
    c("toolkit", { title: "Update the project's toolkit", icon: "wrench", group: "Tools", run: () => this.updateToolkit() });
    c("archive", { title: "Archive project…", icon: "archive", run: () => this.archive() });
    c("run-checks", { title: "Run all checks", icon: "list-checks", kbd: "mod+shift+k", run: () => this.runChecks() });
    c("checkpoint", { title: "Save checkpoint", icon: "bookmark", kbd: "mod+s", run: () => this.checkpoint() });
    c("download", { title: "Download project", icon: "download", run: () => this.download() });
    c("reveal", { title: "Show in Finder", icon: "folder", when: () => !state.info.server_mode, run: () => this.reveal() });
    c("kicad-board", { title: "Open board in KiCad", icon: "circuit-board", kbd: "mod+shift+b", when: () => !state.info.server_mode, run: () => this.openKicad("board") });
    c("kicad-schematic", { title: "Open schematic in KiCad", icon: "waypoints", kbd: "mod+shift+e", when: () => !state.info.server_mode, run: () => this.openKicad("schematic") });
    c("kicad-project", { title: "Open project in KiCad", icon: "folder-open", when: () => !state.info.server_mode, run: () => this.openKicad("project") });
    c("flag", { title: "Flag an issue", icon: "flag", kbd: "mod+shift+f", group: "Review", run: () => this.flagTool() });
    c("review", { title: "Toggle review flags", icon: "panel-right", kbd: "mod+shift+r", group: "Review", run: () => this.toggleReview() });
    c("send-flags", { title: "Send flags to Claude", icon: "send", kbd: "mod+shift+enter", group: "Review", run: () => this.review.send([...this.review.sel], this.reviewPanel.note.value) });
    c("toggle-chat", { title: "Toggle chat", icon: "panel-left", kbd: "mod+\\", group: "View", run: () => this.toggleChat() });
    c("dictate", { title: "Dictate a message", icon: "mic", kbd: "mod+shift+d", group: "Chat", run: () => {
      if (this.chatPane.classList.contains("collapsed")) this.toggleChat();
      this.chat.toggleDictation();
    } });
    c("new-chat", { title: "New conversation", icon: "message-square", kbd: "mod+shift+n", run: () => this.chat.newSession() });
    c("stop", { title: "Stop Claude", icon: "circle-stop", kbd: "mod+.", run: () => this.chat.stop() });
    c("outputs", { title: "Generate fab outputs", icon: "package", run: () => { this.show("outputs"); this.view("outputs").generate(true); } });
    c("render3d", { title: "Render 3D views", icon: "camera", run: () => { this.show("3d"); this.view("3d").renders(true, true); } });
    c("find", { title: "Find a part or net", icon: "search", group: "View", run: () => this.find() });
    c("timelapse", { title: "Play timelapse", icon: "play", kbd: "mod+shift+t", group: "View", run: () => this.timelapse() });
    c("mission", { title: "Run monitor", icon: "activity", kbd: "mod+shift+m", group: "View", run: () => this.mission() });
    c("copper", { title: "Inspect copper by net", icon: "cable", group: "View", run: () => { this.show("board"); this.view("board").setPanel("copper"); } });
    c("rename", { title: "Rename project", icon: "pencil", run: () => this.rename() });
  }

  find() {
    if (this.tab !== "board" && this.tab !== "schematic") this.show("board");
    const v = this.views[this.tab];
    v && v.focusFind && v.focusFind();
  }

  // parts and nets for the command palette
  findItems() {
    const out = [];
    const b = this.views.board && this.views.board.data;
    if (b) {
      for (const f of b.footprints) out.push({ label: `${f.ref}  ${f.val || ""}`.trim(), icon: "microchip", desc: (f.lib || "").split(":").pop(),
        run: () => this.locate({ ref: f.ref }) });
      for (const n of (b.nets || []).slice(0, 400)) if (n) out.push({ label: n.split("/").pop(), icon: "cable", desc: "net", run: () => { this.show("board"); this.view("board").highlight({ nets: [n] }); } });
    }
    return out;
  }

  // ------------------------------------------------------------------ live events
  // ------------------------------------------------------------------ the guided start
  // Chat first: the chat alone, then the canvas beside it as Claude draws; Start opens the workspace.
  enterGuided() {
    this.guided = new GuidedCanvas(this);
    this.guidedPane = h("div.gdpane", this.guided.el);
    this.main.appendChild(this.guidedPane);
    this.root.classList.add("guided");
    if (this.startBtn) this.startBtn.style.display = "none";
  }

  guidedChanged(has, phase) {
    this.root.classList.toggle("canvas-on", !!has);
    if (this.startBtn) this.startBtn.style.display = phase === "ready" ? "" : "none";
  }

  async startDesign() {
    const b = this.startBtn;
    if (b) { b.disabled = true; b.classList.add("busy"); }
    try { await api(`/api/projects/${enc(this.pid)}/start`, { body: {} }); }
    catch (e) { toast(e.message, "error"); if (b) { b.disabled = false; b.classList.remove("busy"); } }
  }

  // a look at the full workspace during the intake (the canvas comes back with the same button)
  peek() {
    const on = this.root.classList.toggle("guided-peek");
    if (this.peekBtn) clear(this.peekBtn).append(icon(on ? "sparkles" : "layout-grid", 14), h("span", on ? "Canvas" : "Workspace"));
    if (on) this.show(this.tab);
  }

  exitGuided() {
    if (!this.guided) return;
    this.root.classList.add("guided-out");
    setTimeout(() => {
      this.root.classList.remove("guided", "canvas-on", "guided-peek", "guided-out");
      this.guidedPane && this.guidedPane.remove();
      this.guided = null;
      for (const b of [this.startBtn, this.peekBtn]) b && b.remove();
      this.startBtn = this.peekBtn = null;
      this.p.start_phase = null;
      this.show("overview");
      toast("Design started", "ok", 2200);
    }, 380);
  }

  wire() {
    const ev = this.ev;
    ev.on("canvas.update", (e) => this.guided && this.guided.set(e.canvas || {}));
    ev.on("project.start", (e) => {
      if (e.phase === "done") this.exitGuided();
      else if (this.guided) this.guided.set({ phase: e.phase });
    });
    ev.on("stages", (e) => this.renderStages(e.stages));
    ev.on("stock.alert", (e) => {                       // the stock watch: a part has just run short
      const parts = e.parts || [];
      if (!parts.length) return;
      const what = { out: "out of stock", low: "running low", gone: "discontinued" };
      const text = parts.length === 1 ? `${parts[0].refs.slice(0, 3).join(", ")} (${parts[0].lcsc}) is ${what[parts[0].state]}`
        : `${parts.length} parts are short for the planned order`;
      toast(text, "warn", 9000, { label: "BOM", run: () => this.show("bom") });
      native.notify("Stock watch", text, this.pid);
    });
    ev.on("project.changed", (e) => { if (e.summary) { this.p = { ...this.p, ...e.summary }; this.renderStages(this.p.stages); this.nameEl.textContent = this.p.name; } });
    ev.on("live.status", (e) => this.renderLive(e));
    ev.on("highlight", (e) => {
      const view = e.view === "schematic" ? "schematic" : "board";
      if (this.tab !== view) this.show(view);
      const v = this.view(view);
      v && v.highlight(e);
    });
    ev.on("board.moves", (e) => this.viewIf("board", (v) => v.animateMoves(e.moves)));
    ev.on("board.live", (e) => this.viewIf("board", (v) => v.liveMoves(e.moves)));
    ev.on("board.changed", (e) => { this.viewIf("board", (v) => { v.reload(e); v.placement = null; v.routing = null; if (v.panel === "placement") v.loadPlacement(true); if (v.panel === "routing") v.loadRouting(true); });
      this.viewIf("3d", (v) => v.stale()); this.inspector.invalidate(); });
    ev.on("routing.changed", () => this.viewIf("board", (v) => { v.routing = null; if (v.panel === "routing") v.loadRouting(true); }));
    ev.on("placement.changed", () => { this.viewIf("board", (v) => { v.placement = null; if (v.panel === "placement") v.loadPlacement(true); }); this.inspector.invalidate(); });
    ev.on("route.progress", (e) => this.viewIf("board", (v) => v.routeEvent(e)));
    ev.on("annotations", (e) => this.viewIf("board", (v) => v.setAnnotations(e.items)));
    ev.on("live.selection", (e) => { this.viewIf("board", (v) => v.kicadSelection(e.items)); this.chat.setKicadSelection(e.items); });
    ev.on("schematic.changed", () => { this.viewIf("schematic", (v) => v.reload()); this.inspector.invalidate(); });
    ev.on("schematic.notes", () => { this.viewIf("schematic", (v) => v.loadNotes()); this.inspector.invalidate(); });
    ev.on("checks.start", () => this.renderChecks(null, true));
    ev.on("checks.done", (e) => {
      if (e.error) { toast("Checks failed: " + e.error, "error"); this.renderChecks(this.p.checks); return; }
      api(`/api/projects/${enc(this.pid)}`).then((p) => { this.p.checks = p.checks; this.renderChecks(p.checks); });
    });
    ev.on("notice", (e) => toast(e.text, e.level === "error" ? "error" : e.level === "ok" ? "ok" : "info"));
    ev.on("history.snapshot", () => this.viewIf("history", (v) => v.reload(false)));
    ev.on("_disconnected", () => this.renderLive({ ...this.lastLive, _offline: true }));
    ev.on("_connected", () => { this.renderLive(this.lastLive || {}); this.review.load(); });
  }

  view(name) {
    if (!this.views[name]) {
      const holder = h("div.view");
      this.body.appendChild(holder);
      const v = this.views[name] = new VIEWS[name](holder, this);
      v.holder = holder;
      if (this.probed && this.probed.source !== name && v.probe && this.probed.refs.length) v.probe(this.probed.refs, this.probed.source);
    }
    return this.views[name];
  }

  viewIf(name, fn) { if (this.views[name]) fn(this.views[name]); }

  lastIn(place) {
    const views = (PLACES.find((p) => p[0] === place) || PLACES[1])[3];
    const v = localStorage.getItem(`tw.place.${this.pid}.${place}`);
    return views.includes(v) ? v : views[0];
  }

  show(name) {
    if (!VIEWS[name]) name = PLACES.find((p) => p[0] === name) ? this.lastIn(name) : "board";
    const was = this.tab && placeOf(this.tab);
    this.tab = name;
    localStorage.setItem("tw.tab." + this.pid, name);
    localStorage.setItem(`tw.place.${this.pid}.${placeOf(name)}`, name);
    const want = `#/p/${enc(this.pid)}/${name}`;
    if (location.hash !== want) history.replaceState(null, "", want);          // the address follows the view (no reload)
    if (was !== placeOf(name) || !this.tabsEl.querySelector(".subnav")) this.renderTabs();
    for (const [k, v] of Object.entries(this.views)) { v.holder.classList.toggle("on", k === name); if (k !== name && v.hidden) v.hidden(); }
    const v = this.view(name);
    v.holder.classList.add("on");
    v.shown && v.shown();
    for (const t of this.tabsEl.querySelectorAll(".tab")) t.classList.toggle("on", t.dataset.place === placeOf(name));
    for (const t of this.tabsEl.querySelectorAll(".subnav button")) t.classList.toggle("on", t.dataset.view === name);
  }

  renderTabs() {
    clear(this.tabsEl);
    if (this.chatPane.classList.contains("collapsed"))
      this.tabsEl.appendChild(btn("panel-left", null, { onclick: () => this.toggleChat(), "data-tip": "Show the chat", "data-kbd": "mod+\\" }, "sm ghost"));
    const nChecks = this.p.checks && this.p.checks.counts ? (this.p.checks.counts.error || 0) : 0;
    const here = placeOf(this.tab || "overview");
    PLACES.forEach(([id, label, ic, views], i) => {
      this.tabsEl.appendChild(h("div.tab" + (id === here ? ".on" : ""), { "data-place": id, onclick: () => this.show(id === here ? this.tab : this.lastIn(id)),
        "data-tip": label, "data-kbd": tabKey(i) }, icon(ic, 14), h("span", label),
        id === "checks" && nChecks ? h("span.count", { style: { background: "var(--err)", color: "#fff" } }, String(nChecks)) : null));
    });
    const views = (PLACES.find((p) => p[0] === here) || PLACES[1])[3];
    if (views.length > 1)
      this.tabsEl.appendChild(h("div.subnav", views.map((v) => h("button" + (v === this.tab ? ".on" : ""), { "data-view": v, onclick: () => this.show(v) }, SUB[v]))));
    const active = this.review.active().length;
    const open = this.reviewOpen;
    requestAnimationFrame(() => this.fitTabs());
    this.tabsEl.appendChild(h("div.tabs-right",
      h("button.btn.sm" + (open ? ".on" : ".ghost"), { onclick: () => this.toggleReview(), "data-tip": "Review flags", "data-kbd": "mod+shift+r" },
        icon("flag", 14), h("span", "Review"), active ? h("span.count" + (open ? "" : ".muted"), String(active)) : null)));
  }

  // the view alone: the chat and the review panel out of the way, until you leave it
  focusMode(on = !this.focused) {
    this.focused = on;
    document.documentElement.classList.toggle("focusmode", on);
    if (on) {
      this.preFocus = { chat: !this.chatPane.classList.contains("collapsed"), review: this.reviewOpen };
      if (this.preFocus.chat) this.toggleChat();
      if (this.reviewOpen) this.toggleReview(false);
      this.focusPill = h("button.focuspill", { onclick: () => this.focusMode(false) }, icon("focus", 13), "Focus mode", h("span.kbd", "esc"));
      document.body.appendChild(this.focusPill);
      this.onFocusKey = (e) => { if (e.key === "Escape" && !document.querySelector(".modal-bg, .menu, .tl-overlay, .mc-overlay")) this.focusMode(false); };
      addEventListener("keydown", this.onFocusKey);
    } else {
      if (this.focusPill) this.focusPill.remove();
      removeEventListener("keydown", this.onFocusKey);
      if (this.preFocus && this.preFocus.chat && this.chatPane.classList.contains("collapsed")) this.toggleChat();
      if (this.preFocus && this.preFocus.review) this.toggleReview(true);
    }
    for (const v of Object.values(this.views)) v.resize && v.resize();
  }

  // tabs as icons only when their labels do not fit
  fitTabs() {
    const el = this.tabsEl;
    el.classList.remove("compact");
    if (el.scrollWidth > el.clientWidth + 1) el.classList.add("compact");
    if (!this.tabsRO) { this.tabsRO = new ResizeObserver(() => this.fitTabs()); this.tabsRO.observe(el); }
  }

  // ------------------------------------------------------------------ review flags
  toggleReview(on = !this.reviewOpen) {
    this.reviewOpen = on;
    localStorage.setItem("tw.review.open", on ? "1" : "0");
    if (on) { this.reviewPanel.render(); this.mainBody.appendChild(this.reviewPanel.el); }
    else this.reviewPanel.el.remove();
    this.renderTabs();
    for (const v of Object.values(this.views)) v.resize && v.resize();
  }

  flagTool() {
    if (!["board", "schematic", "3d"].includes(this.tab)) this.show("board");
    const v = this.view(this.tab);
    v.flags && v.flags.toggle(true);
  }

  showFlag(f) {
    this.focusFlag = f.id;
    const w = f.where || {};
    const view = f.view === "schematic" || (f.view === "check" && w.sheet) ? "schematic" : f.view === "3d" ? "3d" : "board";
    if (f.view === "check" && w.x === undefined && !w.sheet) { this.show("checks"); return; }
    this.show(view);
    const v = this.view(view);
    v.focusFlag && v.focusFlag(f);
    if (this.reviewOpen) this.reviewPanel.render();
  }

  toggleChat() {
    const c = this.chatPane.classList.toggle("collapsed");
    localStorage.setItem("tw.chat.hidden", c ? "1" : "0");
    this.renderTabs();
    for (const v of Object.values(this.views)) v.resize && v.resize();
    if (!c) this.chat.input.focus();
  }

  // ------------------------------------------------------------------ the header
  renderStages(stages) {
    this.p.stages = stages || [];
    const st = this.p.stages;
    const active = st.find((s) => s.status === "active") || st.find((s) => s.status === "todo") || st[st.length - 1];
    const done = st.filter((s) => s.status === "done" || s.status === "skipped").length;
    clear(this.stageEl).append(h("span.segs", st.map((s) => h("i." + s.status))), h("span.sl", active ? active.title : "Stages"),
      h("span.sn", `${done}/${st.length}`));
    this.stageEl.dataset.tip = "Design stages";
  }

  stagesPop(anchor) {
    const pop = popover(anchor, h("div",
      h("h4", "Design stages"), h("div.small.muted", "Claude updates these as it works. Click one to change it."),
      h("div.stagelist", (this.p.stages || []).map((s) => h("div.st-row", { onclick: (e) => this.stageMenu(e.currentTarget, s, pop) },
        h("span.st-ic." + s.status, STAGE_ICON[s.status] ? icon(STAGE_ICON[s.status], 11) : null),
        h("div.grow", h("div.st-t", s.title), h("div.st-d", s.description), s.note ? h("div.st-n", s.note) : null),
        h("span.badge", s.status))))), { cls: "", align: "start" });
    pop.el.style.width = "360px";
  }

  stageMenu(anchor, s, pop) {
    menu(anchor, ["todo", "active", "done", "blocked", "skipped"].map((st) => ({ label: st[0].toUpperCase() + st.slice(1), checked: s.status === st, run: async () => {
      const r = await api(`/api/projects/${enc(this.pid)}/stage`, { body: { stage: s.id, status: st, note: s.note || "" } });
      this.renderStages(r); pop.close();
    } })).concat(["-", { label: "Note…", icon: "pencil", run: async () => {
      const n = await promptDialog({ title: `Note on ${s.title}`, value: s.note || "", ok: "Save" });
      if (n === null) return;
      const r = await api(`/api/projects/${enc(this.pid)}/stage`, { body: { stage: s.id, status: s.status, note: n } });
      this.renderStages(r); pop.close();
    } }]), { align: "end" });
  }

  renderLive(l) {
    this.lastLive = l || {};
    l = l || {};
    let cls = "", text = "KiCad closed", ic = "plug";
    if (l._offline) { text = "Reconnecting…"; cls = "err"; ic = "unlink"; }
    else if (state.info && state.info.server_mode) { text = "Server"; ic = "cloud"; }
    else if (!l.api) { text = "No KiCad link"; ic = "unlink"; }
    else if (l.running && l.board_open) { text = "Live in KiCad"; cls = "ok"; ic = "zap"; }
    else if (l.running) { text = l.sch_open ? "KiCad: schematic" : "KiCad open"; cls = "warn"; }
    else if (l.process && l.enabled) { text = "Restart KiCad"; cls = "warn"; }
    else if (l.process) { text = "KiCad API off"; cls = "warn"; }
    this.liveEl.className = "kdot" + (cls ? " " + cls : "") + (cls === "ok" ? " pulse" : "");
    this.liveText = text;
    if (this.kicadBtn) this.kicadBtn.firstChild.dataset.tip = `${text}${l.reason ? ": " + l.reason : l.version ? " · KiCad " + String(l.version).split(" ")[0] : ""}`;
  }

  renderChecks(c, running) {
    const el = this.checkEl;
    if (running) { el.className = "pill"; clear(el).append(h("span.spinner", { style: { width: "11px", height: "11px" } }), h("span", "Checking…")); return; }
    if (!c) { el.className = "pill"; clear(el).append(icon("list-checks", 13), h("span", "Not checked")); return; }
    const n = c.counts || {};
    el.className = "pill " + (n.error ? "err" : n.warning ? "warn" : "ok");
    clear(el).append(icon(n.error ? "circle-x" : n.warning ? "triangle-alert" : "circle-check", 13),
      h("span", n.error ? `${n.error} error${n.error === 1 ? "" : "s"}` : n.warning ? `${n.warning} warning${n.warning === 1 ? "" : "s"}` : "Checks pass"));
    if (this.tabsEl) this.renderTabs();
  }

  liveHelp() {
    const l = this.lastLive || {};
    if (state.info && state.info.server_mode) {
      const m = modal({ title: "KiCad on a server", icon: "cloud", body: [
        h("p", "KiCad runs on the server without a window. Follow Claude's work in the board and schematic views."),
        h("p", "To edit in your own KiCad, sync the project with GitHub from the History tab, or download it.")],
        actions: [h("button.btn", { onclick: () => m.close() }, "Close"), h("button.btn.primary", { onclick: () => { m.close(); this.show("history"); } }, "Open History")] });
      return;
    }
    const lines = l.running && l.board_open ? ["Connected. Claude's edits appear in KiCad as they happen, and your selection there is shared."]
      : ["Open the board in KiCad's PCB Editor to see Claude's edits live.",
         "1. Open the board in KiCad.",
         l.enabled ? "2. KiCad's API is enabled. Restart KiCad if it was already open."
                   : "2. Enable KiCad's API (Preferences › Plugins), then restart KiCad.",
         "Without the link, use File › Revert in KiCad to load Claude's changes."];
    const enable = h("button.btn", { onclick: async () => {
      try { const r = await api("/api/kicad/enable-api", { body: {} }); toast(r.already ? "KiCad API already enabled" : "KiCad API enabled. " + r.note, "ok", 8000); m.close(); }
      catch (e) { toast(e.message, "error"); }
    } }, "Enable KiCad API");
    const m = modal({ title: "KiCad live link", icon: "zap", body: [...lines.map((t) => h("p", t)), l.reason ? h("p.small.muted", "Status: " + l.reason) : null],
      actions: [h("button.btn", { onclick: () => m.close() }, "Close"), l.running || l.enabled ? null : enable,
        h("button.btn.primary", { onclick: () => { m.close(); this.openKicad("board"); } }, "Open board in KiCad")] });
  }

  projectMenu(anchor) {
    api("/api/projects").then((list) => {
      const others = list.filter((s) => !s.archived && s.id !== this.pid).slice(0, 8);
      const server = state.info && state.info.server_mode;
      menu(anchor, [
        { label: "Rename…", icon: "pencil", run: () => this.rename() },
        { label: "Design limits…", icon: "ruler", run: () => this.limitsDialog() },
        { label: "Save checkpoint…", icon: "bookmark", kbd: "mod+s", run: () => this.checkpoint() },
        { label: "Download project", icon: "download", run: () => this.download() },
        server ? null : { label: "Show in Finder", icon: "folder", run: () => this.reveal() },
        { label: "Archive project…", icon: "archive", danger: true, run: () => this.archive() },
        "-", { head: "Switch project" }, ...others.map((s) => ({ label: s.name, icon: "circuit-board", hint: fmtTime(s.updated), run: () => go("p/" + enc(s.id)) })),
        { label: "All projects", icon: "layout-grid", kbd: "mod+shift+p", run: () => go("") }]);
    });
  }

  kicadMenu(anchor) {
    menu(anchor.closest(".kicadsplit") || anchor, [
      { head: this.liveText || "KiCad" },
      { label: "Board (PCB Editor)", icon: "circuit-board", kbd: "mod+shift+b", run: () => this.openKicad("board") },
      { label: "Schematic (Schematic Editor)", icon: "waypoints", kbd: "mod+shift+e", run: () => this.openKicad("schematic") },
      { label: "Project (KiCad)", icon: "folder-open", run: () => this.openKicad("project") },
      "-", { label: "About the live link…", icon: "zap", run: () => this.liveHelp() },
    ], { align: "end" });
  }

  async selftest() {
    toast("Running the check self-test…");
    const r = await api(`/api/projects/${enc(this.pid)}/selftest`, { body: {} });
    toast(r.output.trim().split("\n").pop(), r.ok ? "ok" : "error", 8000);
  }

  async updateToolkit() {
    const r = await api(`/api/projects/${enc(this.pid)}/toolkit/update`, { body: {} });
    toast(`Toolkit ${r.toolkit} installed`, "ok");
  }

  async archive() {
    if (!await confirmDialog({ title: `Archive ${this.p.name}?`, text: "You can restore it from the projects page.", ok: "Archive", danger: true })) return;
    await api(`/api/projects/${enc(this.pid)}`, { method: "DELETE" });
    go("");
  }

  async limitsDialog() {
    const body = h("div", h("p.small.muted", { style: { margin: "0 0 10px" } }, "Limits the board must meet. Leave any to Claude; the checks hold the design to the ones you set."));
    const m = modal({ title: "Design limits", icon: "ruler", body, actions: [h("button.btn.primary", { onclick: () => m.close() }, "Done")] });
    try { body.appendChild(await limitsPanel(this.pid, (v) => { this.p.constraints = v; })); }
    catch (e) { body.appendChild(h("div.small.muted", e.message)); }
  }

  // ------------------------------------------------------------------ actions
  async runChecks(only) {
    try { await api(`/api/projects/${enc(this.pid)}/checks/run`, { body: { only } }); this.show("checks"); }
    catch (e) { toast(e.message, "error"); }
  }

  async checkpoint() {
    const msg = await promptDialog({ title: "Save checkpoint", label: "Name", value: "Checkpoint", ok: "Save" });
    if (msg === null) return;
    const r = await api(`/api/projects/${enc(this.pid)}/history/snapshot`, { body: { message: msg || "Checkpoint" } });
    toast(r.hash ? `Checkpoint ${r.hash} saved` : "No changes since the last checkpoint", r.hash ? "ok" : "info");
  }

  download() { native.save(`/api/projects/${enc(this.pid)}/download`); }
  reveal() { if (isNative) native.reveal(this.p.root + "/tracewright.json"); else api(`/api/projects/${enc(this.pid)}/reveal`, { body: { path: "tracewright.json" } }); }

  async openKicad(what) {
    try { await api(`/api/projects/${enc(this.pid)}/kicad/open`, { body: { what } }); toast("Opening in KiCad…", "ok"); }
    catch (e) { toast(e.message, "error"); }
  }

  async rename() {
    const n = await promptDialog({ title: "Rename the project", value: this.p.name, ok: "Rename" });
    if (!n || !n.trim() || n === this.p.name) return;
    this.p = { ...this.p, ...(await api(`/api/projects/${enc(this.pid)}`, { method: "PATCH", body: { name: n.trim() } })) };
    this.nameEl.textContent = this.p.name;
    document.title = `${this.p.name} — Tracewright`;
  }

  // files dropped on the window: attached to the message being written (the chat opens if it was hidden)
  dropped(paths, files) {
    if (this.chatPane.classList.contains("collapsed")) this.toggleChat();
    this.chat.attach({ paths: paths || [], files: files || [] });
  }

  showFile(path) {
    this.show("files");
    this.view("files").open(path);
  }

  dragSplit(el) {
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      el.classList.add("drag");
      const x0 = e.clientX, w0 = this.chatPane.getBoundingClientRect().width;
      const mv = (ev) => { this.chatPane.style.width = Math.max(320, Math.min(innerWidth * 0.62, w0 + ev.clientX - x0)) + "px"; this.viewIf(this.tab, (v) => v.resize && v.resize()); };
      const up = () => { el.classList.remove("drag"); removeEventListener("mousemove", mv); removeEventListener("mouseup", up);
        localStorage.setItem("tw.chatw", parseInt(this.chatPane.style.width)); for (const v of Object.values(this.views)) v.resize && v.resize(); };
      addEventListener("mousemove", mv); addEventListener("mouseup", up);
    });
  }

  // The selection: shared with Claude, and cross-probed -- the parts picked in one view (board, schematic,
  // 3D, BOM) are selected in the others, which bring them into view when they are shown.
  select(items, source) {
    this.chat.setSelection(items, source);
    api(`/api/projects/${enc(this.pid)}/selection`, { body: { items } }).catch(() => {});
    if (this.probing) return;
    const refs = [...new Set((items || []).filter((i) => i.ref).map((i) => i.ref))];
    if (!refs.length && (items || []).some((i) => i.net)) return;          // a net picked on the board: parts stay as they are
    if (source !== "inspector" && localStorage.getItem("tw.inspector") !== "off") {
      if (refs.length === 1) this.inspector.show(refs[0]); else this.inspector.hide();
    }
    this.probed = { refs, source };                                         // for the views opened later
    this.probing = true;
    try { for (const [k, v] of Object.entries(this.views)) if (k !== source && v && v.probe) v.probe(refs, source); }
    finally { this.probing = false; }
  }
  timelapse() { (this.tl = this.tl || new TimelapsePlayer(this)).open(); }

  mission() { (this.mc = this.mc || new MissionControl(this)).open(); }

  // The run monitor opens by itself when Claude works on its own (autonomous, past the intake) for more
  // than a few seconds -- once per run: closing it keeps it closed until the next one.
  watchMission() {
    this.ev.on("agent.status", (e) => {
      if (!e.busy) { clearTimeout(this.mcTimer); this.mcTurn = null; return; }
      if (e.turn === this.mcTurn) return;
      this.mcTurn = e.turn;
      clearTimeout(this.mcTimer);
      if (localStorage.getItem("tw.mission.auto") === "0") return;
      this.mcTimer = setTimeout(async () => {
        if (!this.chat || !this.chat.busy || (this.mc && (this.mc.isOpen || this.mc.dismissed === e.turn))) return;
        const pr = await api(`/api/projects/${enc(this.pid)}`).catch(() => null);
        if (!pr || !pr.unattended || !this.chat.busy) return;
        this.mission();
        this.mc.turn = e.turn;
      }, 6000);
    });
  }

  ask(text) { if (this.chatPane.classList.contains("collapsed")) this.toggleChat(); this.chat.prefill(text); }
  locate(where) {
    if (!where) return;
    if (where.sheet && !where.x && where.x !== 0) { this.show("schematic"); this.view("schematic").highlight({ refs: where.ref ? [where.ref] : [], sheet: where.sheet }); return; }
    if (where.sheet) { this.show("schematic"); this.view("schematic").focus(where); return; }
    this.show("board");
    this.view("board").highlight({ refs: where.ref ? [where.ref] : [], nets: where.net ? [where.net] : [],
      points: (where.x !== undefined && where.x !== null) ? [{ x: where.x, y: where.y }] : [] });
  }
}
