// Files, history (with GitHub), outputs and the brief / docs.
import { h, clear, api, toast, fmtTime, fmtSize, lightbox, upload, btn, confirmDialog } from "./util.js";
import { icon } from "./icons.js";
import { markdown } from "./markdown.js";
import { native, isNative } from "./native.js";
import { state } from "./app.js";
import { OrderPanel } from "./order.js";
import { Compare } from "./compare.js";
import { BringUpView } from "./bringup.js";

const enc = encodeURIComponent;
const IMG = /\.(png|jpe?g|gif|svg|webp)$/i;

function fileUrl(pid, path, raw = true) { return `/api/projects/${enc(pid)}/file?path=${enc(path)}${raw ? "&raw=1" : ""}`; }

export function fileIcon(name, dir) {
  if (dir) return "folder";
  const n = name.toLowerCase();
  if (n.endsWith(".kicad_pcb")) return "circuit-board";
  if (n.endsWith(".kicad_sch")) return "waypoints";
  if (n.endsWith(".kicad_pro")) return "folder-open";
  if (IMG.test(n)) return "image";
  if (/\.(md|txt|rpt|log)$/.test(n)) return "file-text";
  if (/\.(py|json|js|sh|toml|ya?ml|kicad_dru|kicad_sym|kicad_mod|net|csv)$/.test(n)) return "file-code";
  if (/\.(zip|gz|tgz)$/.test(n)) return "package";
  if (/\.(step|stp|wrl|glb)$/.test(n)) return "box";
  if (/\.(gbr|gtl|gbl|gto|gbo|gts|gbs|gko|drl)$/.test(n)) return "layers";
  if (n.endsWith(".pdf")) return "file";
  return "file";
}

function openFile(ws, path) {
  if (isNative) native.openPath(ws.p.root + "/" + path);
  else window.open(fileUrl(ws.pid, path), "_blank", "noopener");
}

function revealFile(ws, path) {
  if (isNative) native.reveal(ws.p.root + "/" + path);
  else api(`/api/projects/${enc(ws.pid)}/reveal`, { body: { path } });
}

async function viewFile(ws, path, into) {
  const pid = ws.pid;
  clear(into);
  const name = path.split("/").pop();
  const server = state.info && state.info.server_mode;
  into.appendChild(h("div.fhead", icon(fileIcon(name), 15), h("b.ellipsis", name), h("span.small.faint.ellipsis", path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""), h("div.grow"),
    server ? null : btn("external-link", "Open", { onclick: () => openFile(ws, path), "data-tip": isNative ? "Open in default app" : "Open in new tab" }, "sm ghost"),
    server ? null : btn("folder", null, { onclick: () => revealFile(ws, path), "data-tip": "Show in Finder" }, "sm ghost"),
    btn("download", null, { onclick: () => native.save(fileUrl(pid, path) + "&download=1"), "data-tip": "Download" }, "sm ghost")));
  const body = h("div");
  into.appendChild(body);
  if (IMG.test(path)) { body.appendChild(h("img.preview", { src: fileUrl(pid, path) + "&t=" + Date.now(), onclick: (e) => lightbox(e.target.src) })); return; }
  if (/\.pdf$/i.test(path)) { into.appendChild(h("iframe", { src: fileUrl(pid, path), style: { top: "42px", height: "calc(100% - 42px)" } })); return; }
  if (/\.(zip|step|stp|wrl|glb|drl)$/i.test(path)) {
    body.appendChild(h("div.empty.plain", { style: { marginTop: "40px" } }, h("div.eicon", icon(fileIcon(name), 22)), h("h3", name), h("p", "No preview available.")));
    return;
  }
  try {
    const f = await api(`/api/projects/${enc(pid)}/file?path=${enc(path)}`);
    if (typeof f === "string") { body.appendChild(h("pre.code", f)); return; }
    if (/\.md$/i.test(path)) body.appendChild(h("div.md.doc", { html: markdown(f.text) }));
    else body.appendChild(h("pre.code", f.text + (f.truncated ? "\n\n… (truncated)" : "")));
  } catch (e) { body.appendChild(h("div.empty.plain", h("p", e.message))); }
}

export class FilesPanel {
  constructor(el, ws) {
    this.pid = ws.pid; this.ws = ws; this.path = "";
    this.list = h("div.flist"); this.view = h("div.fview");
    el.appendChild(h("div.split2", this.list, this.view));
    this.load("");
  }
  shown() { if (!this.opening) this.load(this.path); }

  // a file: its folder listed, the file shown
  async open(path) {
    this.opening = true;
    const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    await this.load(dir);
    this.opening = false;
    const it = [...this.list.querySelectorAll(".fitem")].find((x) => x.dataset.path === path);
    this.list.querySelectorAll(".fitem").forEach((x) => x.classList.toggle("on", x === it));
    if (it) it.scrollIntoView({ block: "nearest" });
    viewFile(this.ws, path, this.view);
  }

  async load(path) {
    this.path = path;
    let d;
    try { d = await api(`/api/projects/${enc(this.pid)}/files?path=${enc(path)}`); } catch (e) { toast(e.message, "error"); return; }
    clear(this.list);
    const crumbs = h("div.crumbs", h("a", { onclick: () => this.load("") }, "project"));
    let acc = "";
    for (const part of (d.path || "").split("/").filter(Boolean)) { acc += (acc ? "/" : "") + part; const p = acc; crumbs.append(h("span.cs", "/"), h("a", { onclick: () => this.load(p) }, part)); }
    const picker = h("input", { type: "file", multiple: true, style: { display: "none" }, onchange: async () => {
      if (!picker.files.length) return;
      try {
        const r = await upload(`/api/projects/${enc(this.pid)}/upload`, [...picker.files], { dir: d.path || "uploads" });
        toast(`Uploaded ${r.saved.length} file${r.saved.length === 1 ? "" : "s"}`, "ok"); this.load(this.path);
      } catch (e) { toast(e.message, "error"); }
    } });
    this.list.appendChild(h("div.listhead", h("div.grow", crumbs), picker,
      btn("upload", null, { "data-tip": "Upload files", onclick: () => picker.click() }, "sm ghost")));
    if (d.path) this.list.appendChild(h("div.fitem.dir", { onclick: () => this.load(d.path.split("/").slice(0, -1).join("/")) }, icon("arrow-left", 14), h("span.fn", "..")));
    for (const e of d.entries) {
      const it = h("div.fitem" + (e.dir ? ".dir" : ""), { "data-path": e.path, onclick: () => {
        if (e.dir) this.load(e.path);
        else { this.list.querySelectorAll(".fitem").forEach((x) => x.classList.remove("on")); it.classList.add("on"); viewFile(this.ws, e.path, this.view); }
      }, ondblclick: () => { if (!e.dir && !(state.info && state.info.server_mode)) openFile(this.ws, e.path); } },
        icon(fileIcon(e.name, e.dir), 15), h("span.fn", e.name), e.dir ? icon("chevron-right", 13) : h("span.sz", fmtSize(e.size)));
      this.list.appendChild(it);
    }
    if (!d.entries.length) this.list.appendChild(h("div.small.muted", { style: { padding: "14px" } }, "Empty folder"));
    if (!this.view.firstChild) this.view.appendChild(h("div.empty.plain", { style: { marginTop: "70px" } }, h("div.eicon", icon("folder-open", 22)), h("h3", "Project files"),
      h("p", "Select a file to preview it. Files dropped on the window are attached to your next message.")));
  }
}

export class HistoryPanel {
  constructor(el, ws) {
    this.pid = ws.pid; this.ws = ws;
    this.gh = h("div.ghcard");
    this.list = h("div.hscroll");
    this.view = h("div.fview");
    el.appendChild(h("div.split2", h("div.histcol", this.gh, h("div.listhead", h("b", "History"), h("div.grow"), h("button.btn.sm", { onclick: () => ws.timelapse(), "data-tip": "Play back every change" }, icon("play", 13), "Timelapse")), this.list), this.view));
    ws.ev.on("github.status", (e) => this.renderGh(e));
    this.reload();
  }
  shown() { this.reload(); }

  async github(action, body) {
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/github/${action}`, { body: body || {} });
      this.renderGh(r);
      if (action === "pull") toast(r.pulled ? `Pulled ${r.pulled} commit${r.pulled === 1 ? "" : "s"} from GitHub` : "Already up to date", "ok");
      if (action === "push") toast("Pushed to GitHub", "ok");
      this.reload(false);
    } catch (e) { toast(e.message, "error"); this.reload(); }
  }

  renderGh(g) {
    const b = clear(this.gh);
    if (!g) return;
    if (!g.connected) {
      const repo = h("input", { placeholder: "New repository, owner/name or URL" });
      b.append(h("div.row", icon("git-branch", 15), h("b", "GitHub"), h("span.grow"), g.token ? null : h("a.small", { onclick: () => location.hash = "#/settings/github" }, "Add token")),
        h("div.small.muted", "Sync this project with a private repository."),
        h("div.row", h("div.grow", repo), btn("link", "Connect", { onclick: (ev) => { ev.currentTarget.disabled = true; this.github("connect", { repo: repo.value, private: true }); } }, "sm")));
      return;
    }
    const auto = h("input", { type: "checkbox", checked: g.auto_push !== false, onchange: () => this.github("settings", { auto_push: auto.checked }) });
    const sync = g.error ? h("span.small.err-t", g.error) : h("span.small.muted", g.ahead || g.behind ? `${g.ahead} to push · ${g.behind} to pull` : "In sync");
    b.append(h("div.row", icon("git-branch", 15), h("b", "GitHub"), h("a.small.ellipsis", { onclick: () => native.openURL(g.url) }, g.repo || ""), h("span.grow"), sync),
      h("div.row", btn("cloud-upload", "Push", { onclick: () => this.github("push") }, "sm"), btn("cloud-download", "Pull", { onclick: () => this.github("pull") }, "sm"),
        h("span.grow"), h("label.row.small", { style: { cursor: "pointer" } }, h("label.switch", auto, h("span.track")), "Push after each turn")));
  }

  async reload(gh = true) {
    if (gh) api(`/api/projects/${enc(this.pid)}/github?fetch=1`).then((g) => this.renderGh(g)).catch(() => clear(this.gh));
    let d;
    try { d = await api(`/api/projects/${enc(this.pid)}/history`); } catch (e) { toast(e.message, "error"); return; }
    clear(this.list);
    if (!d.length) this.list.appendChild(h("div.small.muted", { style: { padding: "14px" } }, "No history yet"));
    for (const c of d) {
      const who = /^(Claude|Before):/.test(c.message) ? "claude" : /^(Imported|Restored)/.test(c.message) ? "" : "user";
      const stat = (c.stat || "").replace(/ files? changed/, " files").replace(/ insertions?\(\+\)/, " +").replace(/ deletions?\(-\)/, " −");
      const it = h("div.hitem", { onclick: () => { this.list.querySelectorAll(".hitem").forEach((x) => x.classList.remove("on")); it.classList.add("on"); this.show(c); } },
        h("span.hdot." + who), h("div.grow", h("div.hm", c.message), h("div.hd", h("span.mono", c.hash), h("span", fmtTime(c.date)), stat ? h("span", stat) : null)));
      this.list.appendChild(it);
    }
    if (!this.view.firstChild) this.view.appendChild(h("div.empty.plain", { style: { marginTop: "70px" } }, h("div.eicon", icon("history", 22)), h("h3", "History"),
      h("p", "Select a checkpoint to see its changes or restore it.")));
  }

  async show(c) {
    clear(this.view);
    const files = await api(`/api/projects/${enc(this.pid)}/history/${c.hash}`).catch(() => []);
    this.view.appendChild(h("div", { style: { padding: "22px 26px" } },
      h("h3", { style: { margin: "0 0 4px", fontSize: "16px" } }, c.message), h("div.small.muted", `${c.hash} · ${new Date(c.date).toLocaleString()} · ${c.author}`),
      h("div.row", { style: { margin: "16px 0 18px" } }, this.ws.p.has_pcb ? btn("git-branch", "Compare with now", { onclick: () => new Compare(this.ws, c).open() }, "primary") : null,
        btn("rotate-ccw", "Restore", { onclick: async () => {
        if (!await confirmDialog({ title: "Restore this version?", text: `Files return to ${c.hash}. The current state is saved first, so you can undo this.`, ok: "Restore" })) return;
        const r = await api(`/api/projects/${enc(this.pid)}/history/restore`, { body: { rev: c.hash } });
        toast(`Restored (checkpoint ${r.hash})`, "ok"); this.reload();
      } })),
      h("div.label", { style: { marginBottom: "6px" } }, `${files.length} file${files.length === 1 ? "" : "s"} changed`),
      h("div", files.map(([st, f]) => h("div.row.small", { style: { padding: "4px 0", borderTop: "1px solid var(--line)" } },
        h("span.badge." + (st === "A" ? "ok" : st === "D" ? "err" : "warn"), { style: { minWidth: "22px", justifyContent: "center" } }, st), icon(fileIcon(f), 13), h("span.mono", f))))));
  }
}

export class OutputsPanel {
  constructor(el, ws) {
    this.pid = ws.pid; this.ws = ws;
    this.order = new OrderPanel(ws);
    this.box = h("div.panel");
    el.appendChild(this.box);
    this.log = [];
    ws.ev.on("outputs.start", () => { this.busy = true; this.log = []; this.render(); });
    ws.ev.on("outputs.progress", (e) => { this.log.push(e.text); this.render(); });
    ws.ev.on("outputs.done", (e) => { this.busy = false; if (e.error) toast(e.error, "error"); else toast("Outputs ready: " + e.release, "ok"); this.load(); });
    this.load();
  }
  shown() { this.load(); this.order.load(); }
  async load() { this.data = await api(`/api/projects/${enc(this.pid)}/outputs`).catch(() => ({})); this.render(); }
  generate(renders) { api(`/api/projects/${enc(this.pid)}/outputs/run`, { body: { renders } }).catch((e) => toast(e.message, "error")); }

  render() {
    const b = clear(this.box);
    const inner = h("div.panel-inner");
    b.appendChild(inner);
    inner.appendChild(this.order.el);
    const cs = this.ws.p.checks;
    const ready = cs && cs.counts && !cs.counts.error;
    inner.appendChild(h("div.checks-head",
      h("div.verdict", h("div.vic" + (ready ? ".ok" : ""), this.busy ? h("span.spinner.lg") : icon("package", 22)),
        h("div", h("div.vt", this.busy ? "Generating outputs…" : "Fab outputs"),
          h("div.vs", "Gerbers, drill, BOM, CPL, PDFs, STEP and renders"))),
      h("div.grow"),
      btn(null, "Without renders", { disabled: this.busy, onclick: () => this.generate(false) }),
      btn("package", "Generate outputs", { disabled: this.busy, onclick: () => this.generate(true) }, "primary")));
    if (cs && cs.counts && cs.counts.error && !this.busy)
      inner.appendChild(h("div.notice.err", icon("circle-alert", 15), h("div.nb", h("b", `${cs.counts.error} check error${cs.counts.error === 1 ? "" : "s"}. `), "Fix them before ordering."),
        btn(null, "View checks", { onclick: () => this.ws.show("checks") }, "sm")));
    if (this.log.length) inner.appendChild(h("div.log", this.log.join("\n")));
    const d = this.data || {};
    const names = { release: ["Release", "package"], fab: ["Fabrication", "layers"], docs: ["Documents", "file-text"], images: ["Images", "image"] };
    let any = false;
    const server = state.info && state.info.server_mode;
    for (const sec of ["release", "fab", "docs", "images"]) {
      if (!d[sec] || !d[sec].length) continue;
      any = true;
      inner.appendChild(h("div.outsec", h("div.label", { style: { marginBottom: "8px" } }, names[sec][0]), d[sec].map((f) => h("div.outfile",
        h("div.oic", icon(fileIcon(f.name), 15)), h("span.n", f.name), h("span.small.faint.tnum", `${fmtSize(f.size)} · ${fmtTime(f.mtime)}`),
        IMG.test(f.name) ? btn("eye", null, { "data-tip": "Preview", onclick: () => lightbox(fileUrl(this.pid, f.path) + "&t=" + f.mtime) }, "sm ghost") : null,
        server ? null : btn("external-link", null, { "data-tip": "Open", onclick: () => openFile(this.ws, f.path) }, "sm ghost"),
        server ? null : btn("folder", null, { "data-tip": "Show in Finder", onclick: () => revealFile(this.ws, f.path) }, "sm ghost"),
        btn("download", null, { "data-tip": "Download", onclick: () => native.save(fileUrl(this.pid, f.path)) }, "sm ghost")))));
    }
    if (!any && !this.busy) inner.appendChild(h("div.empty", h("div.eicon", icon("package", 22)), h("h3", "No outputs yet")));
  }
}

export class DocsPanel {
  constructor(el, ws) {
    this.pid = ws.pid; this.ws = ws;
    this.list = h("div.flist", { style: { width: "250px" } }); this.view = h("div.fview");
    el.appendChild(h("div.split2", this.list, this.view));
    this.load();
  }
  shown() { this.load(); }

  async load() {
    const ls = (path) => api(`/api/projects/${enc(this.pid)}/files?path=${path}`).then((d) => d.entries).catch(() => []);
    const [top, docsDir, build] = await Promise.all([ls(""), ls("docs"), ls("build")]);
    const md = (e) => !e.dir && /\.md$/i.test(e.name);
    const docs = [...top.filter((e) => md(e) && /^(BRIEF|README)\.md$/i.test(e.name)).sort((a, b) => (a.name < b.name ? -1 : 1)),
      ...docsDir.filter(md), ...build.filter((e) => e.name === "readiness.md").map((e) => ({ ...e, name: "readiness (last check)" }))];
    clear(this.list);
    this.list.appendChild(h("div.listhead", h("b", "Brief & docs")));
    if (!docs.length) this.list.appendChild(h("div.small.muted", { style: { padding: "12px 14px" } }, "No documents yet."));
    const title = (n) => n.replace(/\.md$/, "").replace(/^(\w)/, (c) => c.toUpperCase()).replace(/-/g, " ");
    const open = (path) => path === "docs/bring-up.md" ? new BringUpView(this.ws, clear(this.view)) : viewFile(this.ws, path, this.view);
    const bu = docs.find((e) => e.path === "docs/bring-up.md");
    if (bu) { docs.splice(docs.indexOf(bu), 1); docs.unshift({ ...bu, name: "Bring-up checklist", special: true }); }
    for (const e of docs) {
      const it = h("div.fitem" + (e.special ? ".special" : ""), { onclick: () => { this.list.querySelectorAll(".fitem").forEach((x) => x.classList.remove("on")); it.classList.add("on"); this.current = e.path; open(e.path); } },
        icon(e.special ? "list-checks" : "file-text", 14), h("span.fn", e.special ? e.name : title(e.name)), h("span.sz", fmtTime(e.mtime)));
      if (e.path === this.current) it.classList.add("on");
      this.list.appendChild(it);
    }
    // the data sheet library: each part's data sheet, and whether its pin table was read from it
    const lib = (await api(`/api/projects/${enc(this.pid)}/datasheets`).catch(() => ({ items: [] }))).items || [];
    if (lib.length) {
      this.list.appendChild(h("div.listhead", h("b", "Data sheets")));
      for (const d of lib) {
        const path = d.pdf || d.pins_file;
        const it = h("div.fitem", { "data-tip": d.pins ? `Pin table: ${d.pins} pins, from ${d.source}` : "No pin table saved", onclick: () => {
          this.list.querySelectorAll(".fitem").forEach((x) => x.classList.remove("on")); it.classList.add("on"); this.current = path; viewFile(this.ws, path, this.view); } },
          icon(d.pdf ? "file-text" : "table-2", 14), h("span.fn", d.name), d.pins ? h("span.sz", `${d.pins} pins`) : h("span.sz", d.size ? fmtSize(d.size) : ""));
        if (path === this.current) it.classList.add("on");
        this.list.appendChild(it);
      }
    }
    if (!this.current && docs.length) { this.current = docs[0].path; this.list.querySelector(".fitem").classList.add("on"); open(docs[0].path); }
  }
}
