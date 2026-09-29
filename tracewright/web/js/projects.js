// The projects page: every board, a new project from a written brief, an import of an existing design.
import { h, clear, api, toast, fmtTime, modal, upload, menu, btn, confirmDialog, promptDialog, kbd } from "./util.js";
import { icon } from "./icons.js";
import { go, topbar, state, command, runCommand } from "./app.js";
import { native, isNative } from "./native.js";
import { openPalette } from "./palette.js";
import { IdeasPanel } from "./ideas.js";
import { limitsForm, limitOptions, limitsSummary } from "./limits.js";

const enc = encodeURIComponent;

export class ProjectsPage {
  constructor(root) {
    this.root = root;
    this.q = ""; this.sort = localStorage.getItem("tw.sort") || "recent"; this.showArchived = false;
    const info = state.info || {};
    root.appendChild(topbar([], [
      h("button.btn.ghost.sm", { onclick: () => openPalette(), "data-tip": "Search and commands", "data-kbd": "mod+k" },
        icon("search", 14), h("span", "Search"), h("span.kbd", "⌘K")),
      h("div.sep-v"),
      btn("calculator", null, { onclick: () => runCommand("calculators"), "data-tip": "Calculators", "data-kbd": "mod+shift+c" }, "ghost sm"),
      btn("book-open", "Lessons", { onclick: () => go("lessons"), "data-tip": "Design lessons" }, "ghost sm"),
      btn("settings", null, { onclick: () => go("settings"), "data-tip": "Settings", "data-kbd": "mod+," }, "ghost sm"),
      info.auth ? btn("log-out", null, { "data-tip": "Sign out", onclick: async () => { await api("/api/logout", { body: {} }); location.href = "/login"; } }, "ghost sm") : null]));
    this.page = h("div.page");
    root.appendChild(this.page);
    this.unreg = [
      command("new-project", { title: "New project", icon: "plus", kbd: "mod+n", group: "Projects", run: () => this.newDialog() }),
      command("import", { title: "Import a KiCad project", icon: "folder-input", kbd: "mod+o", group: "Projects", run: (p) => this.importDialog(p) }),
      command("demo", { title: "Open the demo board", icon: "circuit-board", group: "Projects", run: () => this.demo() }),
      command("find", { title: "Find a project", icon: "search", group: "Projects", run: () => this.search && this.search.focus() }),
    ];
    this.render();
  }

  destroy() { this.unreg.forEach((f) => f()); }

  command(name, arg) {
    if (name === "new-project") this.newDialog();
    else if (name === "import") this.importDialog(arg);
    else if (name === "ideas") this.openIdeas();
  }

  openIdeas() {
    const go = () => { if (!this.ideasPanel) return setTimeout(go, 120); this.ideasPanel.state = "ask"; this.ideasPanel.at = 0; this.ideasPanel.render();
      this.ideasPanel.el.scrollIntoView({ behavior: "smooth", block: "start" }); };
    go();
  }

  dropped(paths, files) {
    if (paths && paths.length) this.importDialog(paths[0]);
    else if (files && files.length) this.importDialog(null, files[0]);
  }

  async demo() {
    try { const s = await api("/api/projects/demo", { body: {} }); go("p/" + enc(s.id)); } catch (e) { toast(e.message, "error"); }
  }

  async render() {
    const info = state.info;
    let list = [];
    try { list = await api("/api/projects"); } catch (e) { toast(e.message, "error"); }
    this.all = list;
    const inner = h("div.page-inner");
    clear(this.page).appendChild(inner);
    inner.appendChild(h("div.page-head.home-head",
      h("div.grow", h("h1", "Projects")),
      btn("sparkles", "Ideas", { onclick: () => this.openIdeas(), "data-tip": "Project ideas" }, "ghost"),
      btn("folder-input", "Import", { onclick: () => this.importDialog(), "data-tip": "Import project", "data-kbd": "mod+o" }),
      btn("plus", "New project", { onclick: () => this.newDialog(), "data-tip": "New project", "data-kbd": "mod+n" }, "primary")));
    const setup = this.setupCard(info);
    if (setup) inner.appendChild(setup);
    const live = list.filter((s) => !s.archived && !s.error);
    const archived = list.filter((s) => s.archived);
    this.ideasPanel = this.ideasPanel || new IdeasPanel(this);
    inner.appendChild(this.ideasPanel.el);
    if (!live.length) { inner.appendChild(this.welcome()); this.footer(inner, archived); return; }
    this.search = h("input", { placeholder: "Filter", value: this.q, oninput: (e) => { this.q = e.target.value; this.drawGrid(); } });
    const sort = h("div.seg", [["recent", "Recent"], ["name", "Name"]].map(([v, t]) => h("button" + (this.sort === v ? ".on" : ""),
      { onclick: () => { this.sort = v; localStorage.setItem("tw.sort", v); this.render(); } }, t)));
    inner.appendChild(h("div.toolbar", h("div.input-icon", { style: { width: "240px" } }, icon("search", 14), this.search), h("div.grow"),
      h("span.small.muted", `${live.length} project${live.length === 1 ? "" : "s"}`), sort));
    this.grid = h("div.cards");
    inner.appendChild(this.grid);
    this.drawGrid();
    this.footer(inner, archived);
  }

  drawGrid() {
    const q = this.q.trim().toLowerCase();
    let list = (this.all || []).filter((s) => !s.archived && !s.error && (!q || (s.name + " " + (s.description || "")).toLowerCase().includes(q)));
    if (this.sort === "name") list = list.slice().sort((a, b) => a.name.localeCompare(b.name));
    clear(this.grid);
    for (const s of list) this.grid.appendChild(this.card(s));
    if (q && !list.length) this.grid.appendChild(h("div.small.muted", "No matching projects"));
  }

  footer(inner, archived) {
    const f = h("div.row.small.faint", { style: { marginTop: "26px", flexWrap: "wrap" } }, icon("folder", 13), h("span", state.info.workspace),
      isNative ? h("a", { onclick: () => native.reveal(state.info.workspace) }, "Show in Finder") : null, h("div.grow"),
      archived.length ? h("a", { onclick: () => { this.showArchived = !this.showArchived; this.render(); } },
        this.showArchived ? "Hide archived" : `Archived (${archived.length})`) : null);
    inner.appendChild(f);
    if (this.showArchived && archived.length) {
      inner.appendChild(h("div.col", { style: { marginTop: "12px" } }, archived.map((s) => h("div.outfile",
        h("div.oic", icon("archive", 15)), h("span.n", s.name), h("span.small.faint", fmtTime(s.updated)),
        h("button.btn.sm", { onclick: async () => { await api(`/api/projects/${enc(s.id)}`, { method: "PATCH", body: { archived: false } }); toast(`Restored ${s.name}`, "ok"); this.render(); } }, "Restore")))));
    }
  }

  setupCard(info) {
    const rows = [];
    if (!info.kicad.cli) rows.push(["KiCad not found", "Install KiCad 9 or 10, or set its location in Settings.", "Settings", () => go("settings/kicad")]);
    if (!info.claude_auth) rows.push(["Claude not connected", info.server_mode ? "Set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY on the server."
      : "Sign in to Claude Code, or add an API key in Settings.", "Settings", () => go("settings/claude")]);
    for (const pr of info.problems || []) rows.push(["A required package failed to load", pr, null, null]);
    if (!rows.length) return null;
    return h("div.setup", h("div.row", { style: { marginBottom: "6px" } }, icon("triangle-alert", 16, "warn-t"), h("b", "Finish setup")),
      rows.map(([t, d, label, run]) => h("div.srow", h("div.sic.bad", icon("circle-alert", 13)), h("div.grow", h("div", { style: { fontWeight: 500 } }, t), h("div.small.muted", d)),
        label ? h("button.btn.sm", { onclick: run }, label) : null)));
  }

  welcome() {
    return h("div.empty", { style: { padding: "56px 30px" } }, h("div.eicon.accent", icon("circuit-board", 24)), h("h3", "No projects yet"),
      h("p", "Start a board from a description, or import an existing design."),
      h("div.row", btn("plus", "New project", { onclick: () => this.newDialog() }, "primary"), btn("folder-input", "Import", { onclick: () => this.importDialog() }),
        btn("play", "Open the demo", { onclick: () => this.demo() }, "ghost")));
  }

  card(s) {
    const cs = s.checks;
    const verdict = cs ? h("span.badge." + (cs.verdict === "checks pass" ? "ok" : cs.verdict === "not ready" ? "err" : "warn"),
      icon(cs.verdict === "checks pass" ? "circle-check" : cs.verdict === "not ready" ? "circle-x" : "triangle-alert", 12), cs.verdict) : h("span.badge", "not checked");
    const thumb = h("div.pthumb", s.has_pcb ? null : h("div.ph", icon(s.has_sch ? "file-code" : "pencil", 22), s.has_sch ? "No board yet" : "Not started"));
    if (s.has_pcb) thumb.style.backgroundImage = `url(/api/projects/${enc(s.id)}/thumb?t=${Date.parse(s.updated) || 0})`;
    if (s.kind === "imported" || s.kind === "in_place" || s.kind === "demo")
      thumb.appendChild(h("span.badge.kind", s.kind === "in_place" ? "in place" : s.kind));
    const stages = s.stages || [];
    const active = stages.find((x) => x.status === "active");
    const more = btn("ellipsis", null, { "data-tip": "More", onclick: (e) => { e.stopPropagation(); this.cardMenu(e.currentTarget, s); } }, "sm pmore");
    return h("div.pcard", { onclick: () => go("p/" + enc(s.id)), oncontextmenu: (e) => { e.preventDefault(); this.cardMenu({ left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY }, s); } },
      thumb, more,
      h("div.pbody",
        h("div.pname.ellipsis", s.name),
        h("div.pdesc", s.description || (s.kind === "imported" ? "Imported project" : s.kind === "in_place" ? "Opened in place" : "No description")),
        h("div.minibar", { "data-tip": active ? `Now: ${active.title}` : "Stages" }, stages.map((x) => h("i." + x.status))),
        h("div.pfoot", verdict, active ? h("span.muted.ellipsis", active.title) : null, h("span.grow"), h("span.faint", fmtTime(s.updated)))));
  }

  cardMenu(anchor, s) {
    const run = (what) => api(`/api/projects/${enc(s.id)}/kicad/open`, { body: { what } }).then(() => toast("Opening in KiCad…", "ok")).catch((e) => toast(e.message, "error"));
    menu(anchor, [
      { label: "Open", icon: "folder-open", run: () => go("p/" + enc(s.id)) },
      state.info.server_mode ? null : { label: "Open in KiCad", icon: "circuit-board", run: () => run("board") },
      state.info.server_mode ? null : { label: isNative ? "Show in Finder" : "Show folder", icon: "folder", run: () => isNative ? native.reveal(s.root) : api(`/api/projects/${enc(s.id)}/reveal`, { body: { path: "tracewright.json" } }) },
      { label: "Rename…", icon: "pencil", run: async () => {
        const n = await promptDialog({ title: "Rename project", value: s.name, ok: "Rename" });
        if (n && n.trim() && n !== s.name) { await api(`/api/projects/${enc(s.id)}`, { method: "PATCH", body: { name: n.trim() } }); this.render(); }
      } },
      "-",
      { label: s.kind === "in_place" ? "Remove from Tracewright" : "Archive", icon: "archive", danger: true, run: async () => {
        const ok = await confirmDialog({ title: s.kind === "in_place" ? `Remove ${s.name}?` : `Archive ${s.name}?`, ok: s.kind === "in_place" ? "Remove" : "Archive", danger: true,
          text: s.kind === "in_place" ? "Its folder and files stay where they are." : "You can restore it from Archived at the bottom of this page." });
        if (!ok) return;
        await api(`/api/projects/${enc(s.id)}`, { method: "DELETE" });
        toast(s.kind === "in_place" ? `${s.name} removed` : `${s.name} archived`, "ok");
        this.render();
      } },
    ]);
  }

  // ------------------------------------------------------------------ a new project
  // pre: {name, brief, layers, assembly} from a design idea
  newDialog(pre = {}) {
    const name = h("input", { placeholder: "Untitled board", autofocus: true, value: pre.name || "" });
    const brief = h("textarea", { placeholder: "What it does, power, interfaces, key parts, size and quantity", style: { minHeight: "170px" }, value: pre.brief || "" });
    const prefs = (state.auth && state.auth.user && state.auth.user.prefs) || {};
    let layers = pre.layers || 2, fab = (state.info.settings && state.info.settings.fab_house) || "jlcpcb", mode = prefs.run_mode || "autonomous";
    let flow = prefs.workflow || localStorage.getItem("tw.workflow") || "guided";
    const seg = (vals, get, set) => {
      const el = h("div.seg");
      const draw = () => { clear(el); for (const [v, label] of vals) el.appendChild(h("button" + (get() === v ? ".on" : ""), { type: "button", onclick: () => { set(v); draw(); } }, label)); };
      draw();
      return el;
    };
    const sw = (checked) => { const i = h("input", { type: "checkbox", checked }); return [i, h("label.switch", i, h("span.track"))]; };
    const [assembly, asw] = sw(pre.assembly !== false), [start, ssw] = sw(true);
    const modes = h("div.col", { style: { gap: "6px" } });
    const drawModes = () => {
      clear(modes);
      for (const [v, t, d, ic] of [["autonomous", "Autonomous", "Questions first, then Claude completes the design.", "zap"],
                                   ["check_in", "Step by step", "Claude waits for your approval after each stage.", "messages-square"]])
        modes.appendChild(h("div.choice" + (mode === v ? ".on" : ""), { onclick: () => { mode = v; drawModes(); } },
          h("input", { type: "radio", checked: mode === v, name: "mode" }), h("div.grow", h("div.ct.row", icon(ic, 14), t), h("div.cd", d))));
    };
    drawModes();
    const flowHint = h("span");
    const flowSeg = seg([["guided", "Guided"], ["classic", "Classic"]], () => flow, (v) => { flow = v; paintFlow(); });
    const paintFlow = () => { flowHint.textContent = flow === "guided" ? "Chat first, with a live canvas and a Start button" : "Opens the full workspace";
      flowSeg.style.opacity = start.checked ? "" : ".45"; flowSeg.style.pointerEvents = start.checked ? "" : "none"; };
    start.addEventListener("change", paintFlow);
    paintFlow();
    const create = async () => {
      if (!brief.value.trim() && !name.value.trim()) { toast("Add a name or a description", "warn"); return; }
      okb.disabled = true; okb.lastChild.textContent = "Creating…";
      try {
        const lim = Object.fromEntries(Object.entries(limits).filter(([, v]) => v != null));
        const s = await api("/api/projects", { body: { name: name.value || firstLine(brief.value), brief: brief.value, layers, fab_house: fab,
          ...(Object.keys(lim).length ? { constraints: lim } : {}),
          assembly: assembly.checked, start: start.checked, run_mode: mode, workflow: flow } });
        m.close();
        go("p/" + enc(s.id));
      } catch (e) { toast(e.message, "error"); okb.disabled = false; okb.lastChild.textContent = "Create project"; }
    };
    const okb = btn("plus", "Create project", { onclick: create }, "primary");
    // Advanced: preset limits, each Claude's to decide until you give it a value
    const limits = {};
    const limSum = h("span.small.muted", limitsSummary(limits));
    const adv = h("details.np-adv", { ontoggle: async (e) => {
      if (!e.target.open || advBody.firstChild) return;
      try {
        advBody.appendChild(limitsForm((await limitOptions()).filter((o) => o.key !== "layers"), limits, (k, v) => {   // layers: above
          if (v == null) delete limits[k]; else limits[k] = v;
          limSum.textContent = limitsSummary(limits);
        }));
      } catch (er) { advBody.appendChild(h("div.small.muted", er.message)); }
    } }, h("summary", h("span", "Advanced"), limSum), h("div.np-advb"));
    const advBody = adv.lastChild;
    const m = modal({ title: "New project", icon: "circuit-board", cls: "wide",
      body: [
        h("div.field", h("label", "Name"), name),
        h("div.field", h("label", "Description"), brief, h("div.hint", "Claude asks about anything that's missing."), adv),
        h("div.opts", { style: { marginBottom: "16px" } },
          h("span.small.muted", "Layers"), seg([[2, "2"], [4, "4"], [6, "6"]], () => layers, (v) => layers = v),
          h("span.small.muted", { style: { marginLeft: "6px" } }, "Fab"), seg([["jlcpcb", "JLCPCB"], ["pcbway", "PCBWay"], ["oshpark", "OSH Park"]], () => fab, (v) => fab = v),
          h("label.row.small", { style: { marginLeft: "6px", cursor: "pointer" } }, asw, "Assembly")),
        h("div.field", h("label", "Workflow"), modes),
        h("div.row", { style: { margin: "4px 0 6px", gap: "12px" } }, h("label.row", { style: { cursor: "pointer" } }, ssw, h("span", "Start designing now")),
          flowSeg, h("span.small.muted", { style: { minWidth: 0 } }, flowHint))],
      actions: [h("span.small.faint.grow.row", kbd("mod+enter"), "to create"), h("button.btn", { onclick: () => m.close() }, "Cancel"), okb] });
    m.box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); create(); } });
  }

  // ------------------------------------------------------------------ an import
  importDialog(prefill, file) {
    const server = !!(state.info && state.info.server_mode);
    let src = file ? "upload" : server ? "github" : prefill && /^(https?:|git@)/.test(prefill) ? "github" : "mac";
    const path = h("input", { placeholder: "Folder, .kicad_pro, .zip or board file", value: prefill && !/^(https?:|git@)/.test(prefill) ? prefill : "" });
    const url = h("input", { placeholder: "https://github.com/owner/repository", value: prefill && /^(https?:|git@)/.test(prefill) ? prefill : "" });
    const fileIn = h("input", { type: "file", accept: ".zip,.kicad_pcb,.brd,.pcbdoc,.PcbDoc,.asc,.cpa,.fpx" });
    let picked = file || null;
    const fileLabel = h("span.small.muted", picked ? picked.name : "No file chosen");
    fileIn.addEventListener("change", () => { picked = fileIn.files[0] || null; fileLabel.textContent = picked ? picked.name : "No file chosen"; });
    const name = h("input", { placeholder: "Optional" });
    let mode = "copy";
    const modes = h("div.col", { style: { gap: "6px" } });
    const drawModes = () => {
      clear(modes);
      for (const [v, t, d] of [["copy", "Make a copy", "The original stays untouched."],
                               ...(server ? [] : [["in_place", "Open in place", "Tracewright adds its files to the project folder."]])])
        modes.appendChild(h("div.choice" + (mode === v ? ".on" : ""), { onclick: () => { mode = v; drawModes(); } },
          h("input", { type: "radio", checked: mode === v, name: "imode" }), h("div.grow", h("div.ct", t), h("div.cd", d))));
    };
    drawModes();
    const revIn = h("input", { type: "checkbox", checked: true });
    const pick = async (kind) => {
      if (isNative) {
        const r = await native.pick({ kind, title: kind === "folder" ? "Choose a KiCad project folder" : "Choose a KiCad project, board or .zip",
          types: kind === "folder" ? [] : ["kicad_pro", "kicad_pcb", "zip", "brd", "pcbdoc", "PcbDoc"] });
        if (r) path.value = r;
        return;
      }
      try { const r = await api("/api/pick", { body: { kind } }); if (r.path) path.value = r.path; else toast("Type or paste the path", "info"); }
      catch (e) { toast(e.message, "error"); }
    };
    const panes = {
      mac: h("div.field", h("label", "Location"), h("div.row", h("div.grow", path), btn("folder", "Folder…", { onclick: () => pick("folder") }), btn("file", "File…", { onclick: () => pick("file") }))),
      github: h("div.field", h("label", "Repository"), url, h("div.hint", "Private repositories need a GitHub token in Settings.")),
      upload: h("div.field", h("label", "Project .zip or board file"), h("div.row", h("label.btn", { style: { position: "relative", overflow: "hidden" } }, icon("upload", 14), "Choose…",
        Object.assign(fileIn, { style: "position:absolute;inset:0;opacity:0;cursor:pointer" })), fileLabel)),
    };
    const where = h("div");
    const tabs = h("div.seg", { style: { marginBottom: "14px" } });
    const drawSrc = () => {
      clear(tabs);
      for (const [v, t, ic] of [...(server ? [] : [["mac", "This Mac", "laptop"]]), ["github", "GitHub", "git-branch"], ["upload", "Upload", "upload"]])
        tabs.appendChild(h("button" + (src === v ? ".on" : ""), { type: "button", onclick: () => { src = v; drawSrc(); } }, icon(ic, 13), t));
      clear(where).appendChild(panes[src]);
      modeField.style.display = src === "mac" ? "" : "none";
    };
    const modeField = h("div.field", h("label", "Import as"), modes);
    const go_ = async () => {
      const busy = (t) => { okb.disabled = !!t; okb.lastChild.textContent = t || "Import"; };
      try {
        let s;
        if (src === "upload") {
          if (!picked) { toast("Choose a file first", "warn"); return; }
          busy("Uploading…");
          s = await upload("/api/projects/import/upload", [picked], { name: name.value.trim(), review: revIn.checked ? "1" : "" });
        } else {
          const p = (src === "github" ? url : path).value.trim();
          if (!p) { toast(src === "github" ? "Enter a repository URL" : "Choose a project", "warn"); return; }
          busy("Importing…");
          s = await api("/api/projects/import", { body: { path: p, mode: src === "mac" ? mode : "copy", name: name.value.trim(), review: revIn.checked } });
        }
        m.close(); go("p/" + enc(s.id));
      } catch (e) { toast(e.message, "error"); busy(""); }
    };
    const okb = btn("folder-input", "Import", { onclick: go_ }, "primary");
    const m = modal({ title: "Import project", icon: "folder-input", cls: "wide",
      sub: "KiCad 6–10, Altium, Eagle, PADS, CADSTAR, Fabmaster or P-CAD",
      body: [tabs, where, h("div.field", h("label", "Name"), name), modeField,
        h("label.row", { style: { cursor: "pointer", margin: "4px 0 6px" } }, h("label.switch", revIn, h("span.track")), "Review with Claude after import")],
      actions: [h("button.btn", { onclick: () => m.close() }, "Cancel"), okb] });
    drawSrc();
  }
}

function firstLine(s) { return (s.trim().split("\n")[0] || "New board").slice(0, 60); }
