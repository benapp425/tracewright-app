// Tracewright UI: a hash router over the pages (projects, a project's workspace, settings, lessons),
// the shared top bar, the command registry behind the menus / shortcuts / palette, the theme, and
// files dropped on the window.
import { h, clear, api, toast, initTooltips, modal, kbd, isMac, menu } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative, events as nativeEvents, initNative } from "./native.js";
import { ProjectsPage } from "./projects.js";
import { Workspace } from "./workspace.js";
import { SettingsPage, LessonsPage } from "./settings.js";
import { openPalette } from "./palette.js";
import { Calculators } from "./calc.js";
import { AuthScreen } from "./auth.js";
import { Onboarding } from "./onboarding.js";
import { Tour } from "./tour.js";
import { BRAND, mark, avatar } from "./brand.js";

export const state = { info: null, auth: null, theme: localStorage.getItem("tw.theme") || "system", current: null, pending: null,
  palette: localStorage.getItem("tw.palette") || "ember", motion: localStorage.getItem("tw.motion") || "full" };

// ------------------------------------------------------------------ theme, colour palette, motion
// A palette is the accent for each theme; some also tint the surfaces (bg, then surface 1-4).
export const PALETTES = {
  ember: { name: "Ember", dark: ["#eb8a50", "#f49b66", "#1d1007"], light: ["#c9622c", "#b4551f", "#ffffff"] },
  ocean: { name: "Ocean", dark: ["#5aa2f8", "#78b4fa", "#06111f"], light: ["#2a6fd4", "#1f60bd", "#ffffff"] },
  mask: { name: "Solder mask", dark: ["#3fca84", "#5ad698", "#04140b"], light: ["#1b8f55", "#157a48", "#ffffff"],
    tint: { dark: ["#0c110e", "#090d0b", "#111814", "#161e1a", "#1c251f", "#232e27"], light: ["#f2f6f3", "#e6ede8", "#ffffff", "#f5f9f6", "#ebf2ed", "#dfe9e2"] } },
  violet: { name: "Violet", dark: ["#a48bfa", "#b7a2fb", "#110a22"], light: ["#6e4fe0", "#5f40cf", "#ffffff"] },
  rose: { name: "Rose", dark: ["#f2729d", "#f68cb0", "#22070f"], light: ["#d2457a", "#bd3569", "#ffffff"] },
  gold: { name: "Gold", dark: ["#e8b84a", "#f0c865", "#1c1403"], light: ["#a87a0d", "#94690a", "#ffffff"] },
  midnight: { name: "Midnight", dark: ["#4fc3e8", "#6fd0ee", "#03131a"], light: ["#1b86b0", "#16759b", "#ffffff"],
    tint: { dark: ["#0a0e17", "#070a12", "#0f1420", "#141a28", "#1a2131", "#212a3c"], light: ["#f1f4f9", "#e4e9f2", "#ffffff", "#f5f7fb", "#eaeef6", "#dde4ef"] } },
  graphite: { name: "Graphite", dark: ["#d9dbe1", "#f2f3f6", "#101114"], light: ["#2a2c33", "#16171b", "#ffffff"] },
};
const TINTED = ["--bg", "--bg-inset", "--surface", "--surface-2", "--surface-3", "--surface-4", "--menu"];
const media = matchMedia("(prefers-color-scheme: light)");
export function resolvedTheme() { return state.theme === "system" ? (media.matches ? "light" : "dark") : state.theme; }
const alpha = (hex, a) => { const v = parseInt(hex.slice(1), 16); return `rgba(${v >> 16 & 255}, ${v >> 8 & 255}, ${v & 255}, ${a})`; };
function applyTheme() {
  const t = resolvedTheme(), root = document.documentElement;
  root.dataset.theme = t;
  const pal = PALETTES[state.palette] || PALETTES.ember;
  const [acc, hov, txt] = pal[t];
  root.style.setProperty("--accent", acc); root.style.setProperty("--accent-hover", hov); root.style.setProperty("--accent-text", txt);
  root.style.setProperty("--accent-soft", alpha(acc, t === "light" ? 0.09 : 0.13)); root.style.setProperty("--accent-line", alpha(acc, t === "light" ? 0.38 : 0.42));
  const tint = pal.tint && pal.tint[t];
  TINTED.forEach((k, i) => tint ? root.style.setProperty(k, k === "--menu" ? tint[3] : tint[i]) : root.style.removeProperty(k));
  root.dataset.palette = state.palette;
  root.dataset.motion = state.motion;
  native.theme(state.theme, tint ? tint[0] : t === "light" ? "#f5f5f7" : "#0f1012");
}
export function setTheme(t) { state.theme = t; localStorage.setItem("tw.theme", t); applyTheme(); }
export function setPalette(p) { if (!PALETTES[p]) return; state.palette = p; localStorage.setItem("tw.palette", p); applyTheme(); }
export function setMotion(m) { state.motion = m === "reduced" ? "reduced" : "full"; localStorage.setItem("tw.motion", state.motion); applyTheme(); }
media.addEventListener("change", () => { if (state.theme === "system") applyTheme(); });
applyTheme();

// ------------------------------------------------------------------ commands (menus, shortcuts, the palette)
const commands = new Map();
// spec: {title, icon, kbd, group, run(arg), when()}; returns an unregister function
export function command(name, spec) { commands.set(name, spec); return () => { if (commands.get(name) === spec) commands.delete(name); }; }
export function allCommands() { return [...commands.entries()].filter(([, c]) => !c.when || c.when()).map(([name, c]) => ({ name, ...c })); }

export function runCommand(name, arg) {
  const c = commands.get(name);
  if (c && (!c.when || c.when())) { c.run(arg); return true; }
  if (["new-project", "import"].includes(name)) { state.pending = { name, arg }; go(""); return true; }    // needs the projects page
  return false;
}

function registerGlobal() {
  command("palette", { title: "Command palette", icon: "command", kbd: "mod+k", group: "General", run: () => openPalette() });
  command("home", { title: "Go to projects", icon: "house", kbd: "mod+shift+p", group: "General", run: () => go("") });
  command("settings", { title: "Settings", icon: "settings", kbd: "mod+,", group: "General", run: (arg) => go("settings" + (typeof arg === "string" && arg ? "/" + arg : "")) });
  command("lessons", { title: "Lessons", icon: "book-open", kbd: "mod+shift+l", group: "General", run: () => go("lessons") });
  command("shortcuts", { title: "Keyboard shortcuts", icon: "keyboard", kbd: "mod+/", group: "Help", run: () => shortcutsHelp() });
  let calcs = null;
  command("calculators", { title: "Calculators", icon: "calculator", kbd: "mod+shift+c", group: "General",
    run: () => (calcs = calcs || new Calculators()).open(state.current && state.current.pid ? state.current : null) });
  command("help", { title: "About Tracewright", icon: "info", group: "Help", run: () => aboutDialog() });
  command("tour", { title: "Take the tour", icon: "map-pin", group: "Help", run: () => startTour(true) });
  command("whats-new", { title: "Release notes", icon: "sparkles", group: "Help", run: () => whatsNew(true) });
  command("sign-out", { title: "Sign out", icon: "log-out", group: "Account", when: () => !!(state.auth && state.auth.user), run: () => signOut() });
  command("account", { title: "Your account", icon: "user", group: "Account", when: () => !!(state.auth && state.auth.user), run: () => go("settings/account") });
  command("theme-system", { title: "Theme: system", icon: "laptop", group: "Appearance", run: () => setTheme("system") });
  command("theme-dark", { title: "Theme: dark", icon: "moon", group: "Appearance", run: () => setTheme("dark") });
  command("theme-light", { title: "Theme: light", icon: "sun", group: "Appearance", run: () => setTheme("light") });
  for (const [k, p] of Object.entries(PALETTES)) command("palette-" + k, { title: `Accent color: ${p.name}`, icon: "palette", group: "Appearance", run: () => setPalette(k) });
  command("motion", { title: "Toggle reduced motion", icon: "zap", group: "Appearance", run: () => setMotion(state.motion === "reduced" ? "full" : "reduced") });
  command("open-project", { title: "Open a project", icon: "folder-open", group: "Projects", hidden: true, run: (pid) => pid && go("p/" + encodeURIComponent(pid)) });
}

// Shortcuts in a browser (in the Mac app the menus own every ⌘ combination).
const KEYMAP = [["mod+k", "palette"], ["mod+,", "settings"], ["mod+/", "shortcuts"], ["mod+shift+k", "run-checks"], ["mod+shift+f", "flag"],
  ["mod+shift+r", "review"], ["mod+shift+enter", "send-flags"], ["mod+.", "stop"], ["mod+\\", "toggle-chat"], ["mod+shift+n", "new-chat"],
  ["mod+shift+p", "home"], ["mod+f", "find"], ["mod+s", "checkpoint"], ["mod+shift+c", "calculators"], ["mod+shift+m", "mission"], ["mod+shift+t", "timelapse"],
  ...["overview", "board", "schematic", "3d", "bom", "checks", "outputs", "rules", "docs", "files"].map((t, i) => [i < 9 ? `mod+${i + 1}` : "mod+0", "tab:" + t])];

function comboOf(e) {
  const k = e.key === "Enter" ? "enter" : e.key.toLowerCase();
  return [(isMac ? e.metaKey : e.ctrlKey) ? "mod" : "", e.shiftKey ? "shift" : "", e.altKey ? "alt" : "", k].filter(Boolean).join("+");
}

document.addEventListener("keydown", (e) => {
  if (isNative && (e.metaKey || e.ctrlKey)) return;            // the Mac app's menus handle these
  if (!(isMac ? e.metaKey : e.ctrlKey)) return;
  const c = comboOf(e);
  const hit = KEYMAP.find(([k]) => k === c);
  if (hit && commands.has(hit[1])) { e.preventDefault(); runCommand(hit[1]); }
});

export function aboutDialog() {
  const v = (state.info && state.info.version) || "";
  const m = modal({ cls: "about", body: h("div.about-in", mark(64), h("div.about-name", BRAND.name), h("div.about-v", `Version ${v}`),
    h("div.about-tag", BRAND.tagline),
    h("div.about-links", h("button.linkbtn", { onclick: () => { m.close(); whatsNew(true); } }, "Release notes"),
      repoUrl() ? h("button.linkbtn", { onclick: () => native.openURL(repoUrl()) }, "GitHub") : null),
    h("div.about-legal", "MIT License · Uses KiCad and Claude")),
    actions: [h("button.btn.primary", { onclick: () => m.close() }, "Done")] });
}

function shortcutsHelp(intro) {
  const groups = [
    ["General", [["mod+k", "Command palette"], ["mod+,", "Settings"], ["mod+n", "New project"], ["mod+o", "Import project"], ["mod+shift+p", "Projects"], ["mod+/", "Keyboard shortcuts"]]],
    ["Project", [["mod+1 … 0", "Switch views"], ["mod+shift+k", "Run all checks"], ["mod+s", "Save checkpoint"],
      ["mod+\\", "Toggle chat"], ["mod+shift+n", "New conversation"], ["mod+.", "Stop Claude"]]],
    ["Review", [["c", "Flag tool"], ["mod+shift+f", "Flag an issue"], ["mod+shift+r", "Review panel"],
      ["mod+shift+enter", "Send flags to Claude"], ["esc", "Exit flag tool"]]],
    ["Board and schematic", [["f", "Fit to view"], ["b", "Flip board"], ["m", "Measure"], ["mod+f", "Find a part or net"],
      ["shift+click", "Add to selection"], ["dblclick", "Zoom to part"]]],
    ["Chat", [["enter", "Send"], ["shift+enter", "New line"], ["mod+shift+m", "Run monitor"]]],
    ["Tools", [["mod+shift+c", "Calculators"], ["mod+shift+t", "Timelapse"]]],
  ];
  const keysOf = (k) => k.includes("…") ? h("span.kbd", k.replace("mod+", isMac ? "⌘" : "Ctrl+")) : k === "dblclick" ? h("span.kbd", "double-click")
    : k === "shift+click" ? [h("span.kbd", "⇧"), h("span.kbd", "click")] : kbd(k);
  const row = ([k, t]) => h("div.row", { style: { justifyContent: "space-between", padding: "6px 0", borderTop: "1px solid var(--line)" } },
    h("span", t), h("span.row", { style: { gap: "3px", flex: "none" } }, keysOf(k)));
  modal({ title: intro ? "Tracewright" : "Keyboard shortcuts", icon: intro ? "info" : "keyboard", cls: "wide",
    sub: intro ? `Version ${(state.info && state.info.version) || ""}` : null,
    body: h("div", { style: { columns: "2", columnGap: "28px" } }, groups.map(([g, rows]) =>
      h("div", { style: { breakInside: "avoid", marginBottom: "16px" } }, h("div.label", { style: { marginBottom: "4px" } }, g), rows.map(row)))) });
}

// ------------------------------------------------------------------ the top bar
// topbar(left nodes, right nodes): the brand, then yours; its empty space drags the Mac window.
export function topbar(left = [], right = []) {
  const u = state.auth && state.auth.user;
  return h("div.topbar", { "data-drag": "" },
    h("a.brand", { href: "#/", "data-tip": "Projects" }, mark(22), left.length ? null : h("span.brand-name", BRAND.name)),
    ...left, h("div.grow"), ...right,
    h("button.btn.ghost.sm.icon.helpbtn", { "data-tip": "Help", onclick: (e) => helpMenu(e.currentTarget) }, icon("info", 15)),
    u ? h("button.acctbtn", { "data-tip": u.email, onclick: (e) => accountMenu(e.currentTarget) }, avatar(u, 26)) : null);
}

function helpMenu(anchor) {
  menu(anchor, [
    { head: BRAND.name + (state.info ? " " + state.info.version : "") },
    { label: "Keyboard shortcuts", icon: "keyboard", kbd: "mod+/", run: () => shortcutsHelp() },
    { label: "Take the tour", icon: "map-pin", run: () => startTour(true) },
    { label: "Release notes", icon: "sparkles", run: () => whatsNew(true) },
    "-",
    ...(repoUrl() ? [{ label: "Documentation", icon: "book-open", run: () => native.openURL(repoUrl("#readme")) },
      { label: "Report a problem", icon: "bug", run: () => native.openURL(repoUrl("/issues/new")) }] : []),
    { label: "About Tracewright", icon: "info", run: () => aboutDialog() },
  ]);
}

function accountMenu(anchor) {
  const u = state.auth.user;
  menu(anchor, [
    { custom: h("div.acct-head", avatar(u, 34), h("div", h("b", u.name), h("span", u.email), h("span.acct-role", u.role === "owner" ? "Owner" : "Member"))) },
    "-",
    { label: "Your account", icon: "user", run: () => go("settings/account") },
    { label: "Settings", icon: "settings", kbd: "mod+,", run: () => go("settings") },
    "-",
    { label: "Sign out", icon: "log-out", run: () => signOut() },
  ]);
}

// Tracewright's repository on GitHub (null in a build that has none: its links are left out)
export function repoUrl(path = "") {
  const r = (state.info && state.info.repo) || BRAND.repo;
  return r ? r.replace(/\/$/, "") + path : null;
}

async function signOut() {
  await api("/api/auth/logout", { body: {} }).catch(() => {});
  state.auth = null;
  toast("Signed out", "ok", 1800);
  route();
}

// The tour of the workspace: the first project someone opens, and from Help. True when it started.
export function startTour(force) {
  const u = state.auth && state.auth.user;
  const seen = localStorage.getItem("tw.toured." + (u ? u.id : "local"));
  if (!force && seen) return false;
  if (!(state.current instanceof Workspace)) {
    if (force) toast("Open a project to take the tour", "info");
    return false;
  }
  localStorage.setItem("tw.toured." + (u ? u.id : "local"), "1");
  new Tour().start();
  return true;
}

// ------------------------------------------------------------------ release notes
// CHANGELOG.md, shown once after each update (remembered per account, so a new port or browser does not
// show it again) and from Help. Each release: an intro line, then ### Added / Changed / Fixed sections of
// "- **Title.** text" items.
const RN_SECTIONS = { added: ["New", "sparkles"], changed: ["Improved", "arrow-up"], fixed: ["Fixed", "wrench"],
  security: ["Security", "shield-check"], removed: ["Removed", "circle-minus"], deprecated: ["Deprecated", "circle-minus"] };

export function parseRelease(body) {
  const out = { intro: "", sections: [] };
  let cur = null, item = null;
  for (const line of String(body || "").split("\n")) {
    const hm = line.match(/^###\s+(.+)/);
    if (hm) { cur = { kind: hm[1].trim().toLowerCase(), items: [] }; out.sections.push(cur); item = null; continue; }
    const bm = line.match(/^[-*]\s+(.*)/);
    if (bm && cur) { item = { raw: bm[1] }; cur.items.push(item); continue; }
    if (item && /^\s+\S/.test(line)) { item.raw += " " + line.trim(); continue; }
    if (!cur && line.trim()) out.intro += (out.intro ? " " : "") + line.trim();
  }
  for (const sec of out.sections) for (const it of sec.items) {
    const m = it.raw.match(/^\*\*(.+?)\*\*\s*(.*)$/);
    it.title = m ? m[1].replace(/[.:]\s*$/, "") : null;
    it.text = m ? m[2] : it.raw;
  }
  return out;
}

const inlineMd = (t) => String(t).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c])
  .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
const longDate = (d) => { const t = new Date(String(d) + "T12:00:00"); return isNaN(t) ? String(d || "") : t.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }); };

function releaseView(r, current) {
  const p = parseRelease(r.body);
  return h("section.rn-rel" + (current ? ".cur" : ""),
    current ? null : h("div.rn-rh", h("b", `Version ${r.version}`), r.date ? h("span", longDate(r.date)) : null),
    p.intro ? h("p.rn-intro", p.intro) : null,
    p.sections.filter((sec) => sec.items.length).map((sec) => {
      const [label, ic] = RN_SECTIONS[sec.kind] || [sec.kind, "circle-dot"];
      return h("div.rn-sec", h("div.rn-sh." + sec.kind, icon(ic, 12), label),
        h("ul.rn-list", sec.items.map((it) => h("li", it.title ? h("b", it.title) : null, it.text ? h("span", { html: inlineMd(it.text) }) : null))));
    }));
}

function markSeen(v) {
  localStorage.setItem("tw.seenVersion", v);
  const u = state.auth && state.auth.user;
  if (u && (u.prefs || {}).seen_version !== v)
    api("/api/auth/me", { method: "PATCH", body: { prefs: { seen_version: v } } }).then((x) => { if (x && x.id) state.auth.user = x; }).catch(() => {});
}

// What changed in this version: once after each update, and from Help. True when it opened.
export async function whatsNew(force) {
  const v = state.info && state.info.version;
  const u = state.auth && state.auth.user;
  const seen = (u && (u.prefs || {}).seen_version) || localStorage.getItem("tw.seenVersion");
  if (!force && (!v || seen === v)) return false;
  let d;
  try { d = await api("/api/changelog"); } catch (e) { if (force) toast(e.message, "error"); return false; }
  markSeen(v);
  const rel = d.releases || [];
  if (!rel.length) return false;
  const [cur, ...older] = rel;
  const body = h("div.rn", releaseView(cur, true));
  if (older.length) {
    const box = h("div.rn-older", older.map((r) => releaseView(r, false)));
    const more = h("button.linkbtn.rn-more", { onclick: () => { box.classList.toggle("open"); more.textContent = box.classList.contains("open") ? "Hide previous versions" : "Previous versions"; } }, "Previous versions");
    body.append(more, box);
  }
  const m = modal({ title: "What's new", icon: "sparkles", cls: "wide whatsnew", sub: `Version ${cur.version}${cur.date ? " · " + longDate(cur.date) : ""}`,
    body, actions: [h("button.btn.primary", { onclick: () => m.close() }, "Continue")] });
  return true;
}

// A newer release on GitHub: a notice once per version, with its notes and the download.
async function updateNotice() {
  if (updateNotice.done) return;
  updateNotice.done = true;
  const u = await api("/api/update").catch(() => null);
  if (!u || !u.available || localStorage.getItem("tw.updateSeen") === u.latest) return;
  localStorage.setItem("tw.updateSeen", u.latest);
  toast(`Tracewright ${u.latest} is available`, "info", 12000, { label: "View", run: () => updateDialog(u) });
}

export function updateDialog(u) {
  const m = modal({ title: `Tracewright ${u.latest}`, icon: "download", cls: "wide whatsnew", sub: `You have version ${u.current}`,
    body: h("div.rn", releaseView({ version: u.latest, body: u.notes || "" }, true)),
    actions: [h("button.btn", { onclick: () => m.close() }, "Later"),
      h("button.btn.primary", { onclick: () => { native.openURL(u.url); m.close(); } }, icon("download", 14), "Download")] });
}

export function go(path) { location.hash = "#/" + path; }

// ------------------------------------------------------------------ routing
async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
  if (state.current && state.current.destroy) state.current.destroy();
  state.current = null;
  document.querySelectorAll(".popover, .menu").forEach((p) => p._close ? p._close() : p.remove());
  const root = clear(document.getElementById("app"));
  try {
    if (!state.auth) state.auth = await api("/api/auth/state");
    if (state.auth.required && !state.auth.user) {
      document.documentElement.classList.add("signedout");
      state.current = new AuthScreen(root, state.auth, (user) => { state.auth.user = user; state.info = null; route(); });
      return;
    }
    document.documentElement.classList.remove("signedout");
    if (!state.info) state.info = await api("/api/info");
    BRAND.repo = state.info.repo || BRAND.repo;
  } catch (e) {
    root.appendChild(h("div.page", h("div.page-inner", h("div.empty", h("div.eicon", icon("plug", 22)), h("h3", "Tracewright is not reachable"), h("p", String(e.message)),
      h("div.row", h("button.btn.primary", { onclick: () => location.reload() }, "Try again"))))));
    return;
  }
  const u = state.auth && state.auth.user;
  if (u && !u.onboarded) {                                       // first run for this account
    state.current = new Onboarding(root, u, (what) => {
      localStorage.setItem("tw.seenVersion", state.info.version);  // a new account needs no "what's new"
      if (what === "demo") api("/api/projects/demo", { body: {} }).then((s) => go("p/" + encodeURIComponent(s.id))).catch((e) => { toast(e.message, "error"); go(""); });
      else { if (what === "import" || what === "new") state.pending = { name: what === "new" ? "new-project" : "import" };
        if (what === "ideas") state.pending = { name: "ideas" };
        location.hash === "#/" ? route() : go(""); }
    });
    return;
  }
  localStorage.setItem("tw.route", hash);
  if (parts[0] === "p" && parts[1]) state.current = new Workspace(root, parts[1], parts[2]);
  else if (parts[0] === "settings") state.current = new SettingsPage(root, parts[1]);
  else if (parts[0] === "lessons") state.current = new LessonsPage(root);
  else state.current = new ProjectsPage(root);
  native.context(parts[0] === "p" ? parts[1] : "", "");
  if (state.pending && state.current.command) { const p = state.pending; state.pending = null; setTimeout(() => state.current.command(p.name, p.arg), 60); }
  if (state.current instanceof Workspace) setTimeout(() => { if (!startTour(false)) whatsNew(false); }, 1400);
  else setTimeout(() => whatsNew(false), 900);
  setTimeout(() => updateNotice(), 5000);
}

// ------------------------------------------------------------------ files dropped on the window
let dropEl = null;
function dropzone(on) {
  if (on && !dropEl) {
    const inProject = state.current instanceof Workspace;
    dropEl = h("div.dropzone", icon(inProject ? "paperclip" : "folder-input", 34),
      h("div", inProject ? "Attach to your message" : "Import a project"),
      h("div.small.muted", inProject ? "Pictures go to Claude. Data sheets, libraries and 3D models go into the project." : "Folder, .kicad_pro, .zip or board file"));
    document.body.appendChild(dropEl);
  } else if (!on && dropEl) { dropEl.remove(); dropEl = null; }
}

function dropped(paths, files) {
  dropzone(false);
  const cur = state.current;
  if (cur && cur.dropped) cur.dropped(paths, files);
}

nativeEvents.on("files", (m) => {
  if (m.phase === "enter") dropzone(true);
  else if (m.phase === "exit") dropzone(false);
  else if (m.phase === "drop") dropped(m.paths || [], null);
});
nativeEvents.on("open", (m) => {                                // dropped on the Dock icon / Open With
  const p = (m.paths || [])[0];
  if (!p) return;
  if (state.current instanceof ProjectsPage) state.current.importDialog(p);
  else { state.pending = { name: "import", arg: p }; go(""); }
});
nativeEvents.on("command", (m) => { if (!runCommand(m.name, m.arg) && m.name.startsWith("tab:")) toast("Open a project first", "info"); });
nativeEvents.on("downloaded", (m) => toast(`Saved ${m.name}`, "ok", 5000, { label: "Show in Finder", run: () => native.reveal(m.path) }));
nativeEvents.on("toast", (m) => toast(m.text, m.level || "info"));
nativeEvents.on("focus", (m) => { if (m.active) native.badge(""); });

if (!isNative) {                                                // a browser: HTML5 drops (files only, no paths)
  let depth = 0;
  const hasFiles = (e) => [...((e.dataTransfer && e.dataTransfer.types) || [])].includes("Files");
  addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; dropzone(true); } });
  addEventListener("dragleave", (e) => { if (hasFiles(e) && --depth <= 0) { depth = 0; dropzone(false); } });
  addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener("drop", (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault(); depth = 0;
    dropped([], [...e.dataTransfer.files]);
  });
}

// ------------------------------------------------------------------ start
window.twApp = { command: runCommand };
addEventListener("tw:signedout", () => { state.auth = null; route(); });     // an API answer said: sign in first
registerGlobal();
// The boot screen in a browser, once a session (the Mac app plays the same one while its engine starts):
// the logo draws itself -- pads land, the traces run between them -- then the name.
function bootSplash() {
  if (isNative || sessionStorage.getItem("tw.booted") || state.motion === "reduced" || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  sessionStorage.setItem("tw.booted", "1");
  const el = h("div.boot", { html: `<div class="boot-box"><svg class="boot-logo" width="104" height="104" viewBox="100 100 824 824"><defs>
    <linearGradient id="bbg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a3530"/><stop offset="1" stop-color="#121815"/></linearGradient>
    <linearGradient id="bcu" x1="230" y1="270" x2="700" y2="840" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#ffc590"/><stop offset=".55" stop-color="#e8864a"/><stop offset="1" stop-color="#b95a26"/></linearGradient></defs>
    <rect class="b-tile" x="100" y="100" width="824" height="824" rx="190" fill="url(#bbg)"/>
    <g fill="none" stroke="url(#bcu)" stroke-linecap="round" stroke-linejoin="round" stroke-width="84"><path class="b-tr b-t1" pathLength="1" d="M300 352H724"/><path class="b-tr b-t2" pathLength="1" d="M512 352V560L600 648V716"/></g>
    <g fill="url(#bcu)"><circle class="b-pad b-p1" cx="276" cy="352" r="78"/><circle class="b-pad b-p2" cx="748" cy="352" r="78"/><circle class="b-pad b-p3" cx="600" cy="742" r="86"/></g>
    <circle class="b-pad b-p3" cx="600" cy="742" r="36" fill="#121815"/></svg>
    <div class="boot-word">Tracewright</div><div class="boot-tag">KiCad boards, designed with Claude</div></div>` });
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("out"), 1750);
  setTimeout(() => el.remove(), 2150);
}

initTooltips();
initNative();
bootSplash();
addEventListener("hashchange", route);
addEventListener("unhandledrejection", (e) => { if (e.reason && e.reason.message && e.reason.message !== "sign in first") toast(e.reason.message, "error"); });
if (!location.hash && isNative) {
  const last = localStorage.getItem("tw.route");
  if (last) history.replaceState(null, "", "#/" + last);
}
route();
