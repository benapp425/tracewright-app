// Settings (sections: general, Claude, GitHub, KiCad and tools, server, about) and the lessons
// knowledge base.
import { h, clear, api, toast, modal, btn, confirmDialog, fmtTime } from "./util.js";
import { icon } from "./icons.js";
import { topbar, state, setTheme, setPalette, setMotion, PALETTES, resolvedTheme, go, repoUrl } from "./app.js";
import { avatar } from "./brand.js";
import { markdown } from "./markdown.js";
import { native, isNative } from "./native.js";

const MODELS = [["claude-opus-5-5", "Claude Opus 5.5 (recommended)"], ["claude-fable-5-1", "Claude Fable 5.1"], ["claude-sonnet-5", "Claude Sonnet 5 (faster)"],
  ["claude-haiku-4-5-20251001", "Claude Haiku 4.5 (fastest)"], ["default", "Claude Code default"]];
const SECTIONS = [["account", "Account", "user"], ["general", "General", "sliders-horizontal"], ["claude", "Claude", "sparkles"], ["github", "GitHub", "git-branch"],
  ["kicad", "KiCad and tools", "circuit-board"], ["server", "Server", "cloud"], ["about", "About", "info"]];
const SECRET = ["anthropic_api_key", "github_token", "server_password"];

export class SettingsPage {
  constructor(root, section) {
    this.section = SECTIONS.find((s) => s[0] === section) ? section : section === "security" ? "general" : "account";
    root.appendChild(topbar([h("div.crumbs-top", h("span.cs", icon("chevron-right", 14)), h("span.cbtn", { style: { cursor: "default" } }, "Settings"))],
      [btn("book-open", "Lessons", { onclick: () => go("lessons") }, "ghost sm"), btn("layout-grid", "Projects", { onclick: () => go("") }, "ghost sm")]));
    this.page = h("div.page");
    root.appendChild(this.page);
    this.dirtyKeys = new Set();
    this.render();
  }

  async render() {
    const [s, info, live] = await Promise.all([api("/api/settings"), api("/api/info"), api("/api/live").catch(() => ({}))]);
    state.info = info;
    this.s = s; this.info = info; this.live = live;
    const inner = h("div.page-inner.narrow");
    clear(this.page).appendChild(inner);
    inner.appendChild(h("div.page-head", h("div.grow", h("h1", "Settings"))));
    const nav = h("nav.snav", SECTIONS.filter(([k]) => k !== "server" || true).map(([k, t, ic]) =>
      h("a" + (k === this.section ? ".on" : ""), { onclick: () => { this.section = k; history.replaceState(null, "", "#/settings/" + k); this.render(); } }, icon(ic, 15), t)));
    this.body = h("div.sbody");
    inner.appendChild(h("div.settings", nav, this.body));
    this[this.section]();
    this.body.appendChild(this.saveBar = h("div.savebar", { style: { display: "none" } }, btn("check", "Save changes", { onclick: () => this.save() }, "primary"),
      h("button.btn", { onclick: () => this.render() }, "Discard"), h("span.small.muted", "Unsaved changes")));
  }

  // ------------------------------------------------------------------ building blocks
  group(title, sub, rows) {
    this.body.appendChild(h("div.sgroup", h("h2", title), sub ? h("div.gsub", sub) : null, rows));
  }
  row(title, desc, control) { return h("div.srow2", h("div.sl", h("div.st", title), desc ? h("div.sd", desc) : null), h("div.sc", control)); }
  status(title, ok, value, extra) {
    return h("div.srow2", h("div.sl", h("div.st.row", h("span.dot" + (ok === true ? ".ok" : ok === false ? ".err" : "")), title)),
      h("div.sc", h("span.sv" + (/^[\/~]/.test(String(value)) ? ".mono" : ""), value), extra || null));
  }
  mark(key) { this.dirtyKeys.add(key); this.saveBar.style.display = "flex"; }
  sel(key, opts) { const el = h("select", { onchange: () => this.mark(key) }); for (const [v, t] of opts) el.appendChild(h("option", { value: v, selected: this.s[key] === v }, t)); el.dataset.key = key; return el; }
  inp(key, attrs = {}) { const el = h("input", { value: SECRET.includes(key) ? "" : this.s[key] ?? "", oninput: () => this.mark(key), ...attrs }); el.dataset.key = key; return el; }
  sw(key) { const i = h("input", { type: "checkbox", checked: !!this.s[key], onchange: () => { this.mark(key); this.save(true); } }); i.dataset.key = key; return h("label.switch", i, h("span.track")); }

  async save(quiet) {
    const body = {};
    for (const el of this.body.querySelectorAll("[data-key]")) {
      const k = el.dataset.key;
      if (!this.dirtyKeys.has(k)) continue;
      body[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value;
    }
    for (const k of SECRET) if (k in body && !body[k]) delete body[k];
    if (!Object.keys(body).length) { this.saveBar.style.display = "none"; return; }
    try {
      this.s = await api("/api/settings", { method: "PATCH", body });
      state.info = await api("/api/info");
      this.dirtyKeys.clear();
      toast(quiet ? "Saved" : "Settings saved", "ok", 1800);
      if (!quiet || Object.keys(body).some((k) => SECRET.includes(k))) this.render(); else this.saveBar.style.display = "none";
    } catch (e) { toast(e.message, "error"); }
  }

  // ------------------------------------------------------------------ sections
  async account() {
    const auth = state.auth || {};
    const u = auth.user;
    if (!u) {
      this.group("Account", null, [h("div.small.muted", "Accounts are turned off.")]);
      return;
    }
    const name = h("input.acct-name", { value: u.name, maxlength: 80 });
    this.group("Your account", null, [
      h("div.acct-card", avatar(u, 54), h("div.grow", h("b", u.name), h("span", u.email), h("span.acct-role", u.role === "owner" ? "Owner" : "Member"))),
      this.row("Name", null, h("div.row", name, btn("check", "Save", { onclick: async () => {
        try { state.auth.user = await api("/api/auth/me", { method: "PATCH", body: { name: name.value } }); toast("Saved", "ok", 1600); this.render(); } catch (e) { toast(e.message, "error"); }
      } }, "sm"))),
      this.row("Email", null, h("span.sv", u.email)),
      this.row("Google", u.google && u.google_email && u.google_email !== u.email ? u.google_email : null,
        u.google ? h("span.sv", "Linked") : auth.google ? btn("link", "Link Google", { onclick: (e) => this.linkGoogle(e.currentTarget) }, "sm") : h("span.sv", "Not set up"))]);
    const old = h("input", { type: "password", placeholder: u.has_password ? "Current password" : "Not set (Google sign-in)", autocomplete: "current-password" });
    const nw = h("input", { type: "password", placeholder: "New password", autocomplete: "new-password" });
    this.group("Password", null, [this.row("Change password", null,
      h("div.col", { style: { gap: "6px", alignItems: "stretch" } }, u.has_password ? old : null, nw, btn("shield-check", "Change password", { onclick: async () => {
        try { await api("/api/auth/password", { body: { old: old.value, new: nw.value } }); toast("Password changed", "ok"); old.value = nw.value = ""; } catch (e) { toast(e.message, "error"); }
      } }, "sm")))]);
    const sessions = await api("/api/auth/sessions").catch(() => []);
    this.group("Sessions", null, [
      ...sessions.map((x) => h("div.srow2", h("div.sl", h("div.st", x.current ? "This window" : shortAgent(x.agent)), h("div.sd", `Since ${new Date(x.created * 1000).toLocaleDateString()}`)),
        h("div.sc", x.current ? h("span.badge.ok", "Current") : null))),
      sessions.length > 1 ? this.row("Other sessions", null, btn("log-out", "Sign out others", { onclick: async () => { await api("/api/auth/sessions/end-others", { body: {} }); toast("Other sessions signed out", "ok"); this.render(); } }, "sm")) : null,
      this.row("Sign out", null, btn("log-out", "Sign out", { onclick: () => import("./app.js").then((m) => m.runCommand("sign-out")) }, "sm"))]);
    if (u.role !== "owner") return;
    const cfg = await api("/api/auth/state").catch(() => ({}));
    const signups = h("input", { type: "checkbox", checked: !!cfg.signups, onchange: async () => { await api("/api/auth/config", { method: "PATCH", body: { allow_signups: signups.checked } }); toast(signups.checked ? "New accounts allowed" : "New accounts turned off", "ok"); } });
    const cid = h("input", { placeholder: "Client ID", value: this.s.google_client_id || "" });
    const secret = h("input", { type: "password", placeholder: this.s.google_client_secret ? "Saved · type to replace" : "Client secret" });
    const redirect = state.info.server_mode ? `${location.origin}/api/auth/google/callback` : "http://127.0.0.1";
    this.group("Sign-in", null, [
      this.row("Allow new accounts", state.info.server_mode ? "Keep off on public servers." : null, h("label.switch", signups, h("span.track"))),
      this.row("Google sign-in", cfg.google ? "Enabled" : "Requires an OAuth client",
        h("div.col", { style: { gap: "6px", alignItems: "stretch", minWidth: "320px" } }, cid, secret, h("div.row", btn("check", "Save", { onclick: async () => {
          try { const r = await api("/api/auth/config", { method: "PATCH", body: { google_client_id: cid.value.trim(), google_client_secret: secret.value.trim() } });
            toast(r.google ? "Google sign-in enabled" : "Google sign-in disabled", "ok"); this.s = await api("/api/settings"); this.render(); } catch (e) { toast(e.message, "error"); }
        } }, "sm"), btn("book-open", "Setup guide", { onclick: () => googleGuide(redirect) }, "sm ghost")))),
      h("div.acct-list", (await api("/api/auth/users").catch(() => [])).map((x) => h("div.acct-row", avatar(x, 28), h("div.grow", h("b", x.name), h("span", x.email)),
        h("span.badge", x.role === "owner" ? "Owner" : "Member"), x.id === u.id ? null : btn("trash-2", null, { "data-tip": "Remove account", onclick: async () => {
          if (!await confirmDialog({ title: `Remove ${x.name}'s account?`, text: "They'll be signed out. Projects aren't affected.", ok: "Remove", danger: true })) return;
          await api(`/api/auth/users/${x.id}`, { method: "DELETE" }); this.render();
        } }, "sm ghost danger"))))]);
  }

  async linkGoogle(b) {
    b.disabled = true; b.classList.add("busy");
    toast("Continue in your browser", "info", 4000);
    try {
      const { googleFlow } = await import("./auth.js");
      const u = await googleFlow({ link: true });
      if (u) { state.auth = await api("/api/auth/state"); toast("Google linked", "ok"); this.render(); }
    } catch (e) { toast(e.message.replace(/^\w/, (c) => c.toUpperCase()), "error"); }
    finally { b.disabled = false; b.classList.remove("busy"); }
  }

  general() {
    const theme = h("div.seg", [["system", "System", "laptop"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]].map(([t, label, ic]) =>
      h("button" + (state.theme === t ? ".on" : ""), { onclick: () => { setTheme(t); this.render(); } }, icon(ic, 13), label)));
    const pals = h("div.palettes", Object.entries(PALETTES).map(([k, p]) => h("button.pal" + (state.palette === k ? ".on" : ""),
      { onclick: () => { setPalette(k); this.render(); } }, h("i", { style: { background: p[resolvedTheme()][0] } }), p.name)));
    const motion = h("input", { type: "checkbox", checked: state.motion === "reduced", onchange: (e) => setMotion(e.target.checked ? "reduced" : "full") });
    this.group("Appearance", null, [this.row("Theme", null, theme),
      this.row("Accent color", null, pals),
      this.row("Reduce motion", null, h("label.switch", motion, h("span.track")))]);
    if (isNative) {
      const lockSw = h("input", { type: "checkbox", disabled: true });
      const after = h("select", { disabled: true }, [[5, "5 minutes"], [15, "15 minutes"], [30, "30 minutes"], [60, "1 hour"], [-1, "When the Mac sleeps"]].map(([v, t]) => h("option", { value: v }, t)));
      const how = h("span.small.muted");
      const lockNow = btn("shield-check", "Lock now", { disabled: true, onclick: () => native.lockNow() }, "sm");
      this.group("Privacy", null, [
        this.row("Lock with Touch ID", "Require Touch ID or your password to open Tracewright.",
          h("div.row", h("label.switch", lockSw, h("span.track")), how)),
        this.row("Lock after", "Time without activity", h("div.row", after, lockNow))]);
      native.lockState().then((st) => {
        if (!st) return;
        lockSw.checked = !!st.enabled; lockSw.disabled = !st.available; after.disabled = !st.enabled; lockNow.disabled = !st.enabled;
        after.value = String(st.minutes);
        how.textContent = !st.available ? "Not available on this Mac" : st.biometrics ? "" : "Uses your Mac password";
      });
      lockSw.onchange = async () => {
        const r = await native.lockSet({ enabled: lockSw.checked, minutes: Number(after.value) });
        lockSw.checked = !!(r && r.enabled);
        after.disabled = lockNow.disabled = !lockSw.checked;
        toast(lockSw.checked ? "Lock enabled" : "Lock disabled", "ok");
      };
      after.onchange = () => native.lockSet({ minutes: Number(after.value) });
    }
    const ws = this.inp("workspace");
    this.group("Projects", null, [
      this.row("Projects folder", null, h("div.row", ws, isNative ? btn("folder", null, { "data-tip": "Choose…", onclick: async () => {
        const p = await native.pick({ kind: "folder", title: "Choose the projects folder" }); if (p) { ws.value = p; this.mark("workspace"); }
      } }, "sm") : null)),
      this.row("Default fab", "For new projects", this.sel("fab_house", [["jlcpcb", "JLCPCB"], ["pcbway", "PCBWay"], ["oshpark", "OSH Park"]])),
      this.row("Placement speed", "Seconds between parts during live placement", this.inp("live_pace_s", { type: "number", step: "0.05", min: "0", max: "2", style: { width: "90px" } })),
      isNative ? null : this.row("Open browser on start", null, this.sw("open_browser"))]);
  }

  claude() {
    const info = this.info;
    this.group("Model", null, [
      this.row("Model", null, this.sel("model", MODELS)),
      this.row("Effort", "Higher is slower and more thorough", this.sel("effort", [["low", "Low"], ["medium", "Medium"], ["high", "High (default)"], ["xhigh", "Extra high"], ["max", "Max"]]))]);
    this.group("Permissions", "Claude only works inside the project folder.", [
      this.row("Approval mode", null, this.sel("permission_mode", [
        ["acceptEdits", "Ask for risky commands (recommended)"], ["default", "Ask for every change"],
        ["bypassPermissions", "Never ask"], ["plan", "Plan only"]])),
      this.row("Allow safe commands without asking", "Toolkit, KiCad, git and read-only commands", this.sw("auto_allow_bash")),
      this.row("Allow web lookups", "Data sheets and part searches", this.sw("allow_web")),
      this.row("Checkpoint every turn", "Undo any turn from History", this.sw("snapshot_each_turn"))]);
    const auth = { api_key: "API key", subscription_token: "Claude plan token", claude_code: "Claude Code" }[info.claude_auth] || "Not connected";
    this.group("Connection", null, [
      this.status("Claude", !!info.claude_auth, auth),
      this.row("Anthropic API key", "Optional. Claude Code is used when empty.",
        this.inp("anthropic_api_key", { type: "password", placeholder: this.s.anthropic_api_key || "Not set" }))]);
  }

  github() {
    const who = h("span.small.muted");
    this.group("GitHub", "Sync projects to private repositories.", [
      this.row("Access token", "Fine-grained, with Contents and Administration access",
        this.inp("github_token", { type: "password", placeholder: this.s.github_token || "Not set" })),
      this.row("Connection", null, h("div.row", who, btn(null, "Test", { onclick: async () => {
        who.textContent = "Checking…";
        try { const r = await api("/api/github/me"); who.textContent = `Connected as ${r.login}`; } catch (e) { who.textContent = e.message; }
      } }, "sm")))]);
  }

  kicad() {
    const info = this.info, live = this.live;
    this.group("KiCad", null, [
      this.status("KiCad", !!info.kicad.cli, info.kicad.cli ? `${info.kicad.version}` : "Not found"),
      this.status("kicad-cli", !!info.kicad.cli, info.kicad.cli || "Not found"),
      this.status("KiCad Python", !!info.kicad.python, info.kicad.python || "Not found"),
      liveRow(info, live, () => this.render()),
      this.row("kicad-cli location", "Leave empty to detect", this.inp("kicad_cli", { placeholder: info.kicad.cli || "" })),
      this.row("KiCad Python location", "Leave empty to detect", this.inp("kicad_python", { placeholder: info.kicad.python || "" }))]);
    this.group("Tools", null, [
      this.status("Claude Code", !!info.claude_cli, info.claude_cli || "Not found"),
      this.status("Git", info.git, info.git ? "Installed" : "Not found"),
      this.status("Java", info.java ? true : null, info.java || "Optional"),
      this.row("Freerouting", "Optional autorouter (.jar)", this.inp("freerouting_jar", { placeholder: info.freerouting || "freerouting-2.x.jar" }))]);
  }

  server() {
    const info = this.info;
    this.group("Server", null, [
      this.status("Mode", !info.server_mode || !!info.auth, info.server_mode ? `Server${info.auth ? " · sign-in required" : ""}` : "This Mac only"),
      this.row("Server password", "Required when reachable from other machines. See DEPLOY.md.",
        this.inp("server_password", { type: "password", placeholder: this.s.server_password ? "Saved · type to replace" : "Not set" }))]);
  }

  about() {
    const info = this.info;
    const upd = h("span.sv", "Checking…");
    api("/api/update").then((u) => {
      clear(upd);
      if (!u.configured) upd.textContent = u.off ? "Update check off" : "";
      else if (u.error) upd.textContent = "Couldn't reach GitHub";
      else if (u.available) upd.append(h("b", `${u.latest} available `), h("a", { onclick: () => native.openURL(u.url) }, "Download"));
      else upd.textContent = "Up to date";
    }).catch(() => { upd.textContent = ""; });
    this.group("Tracewright", null, [
      this.status("Version", null, info.version, upd),
      this.row("Release notes", null, btn("sparkles", "View", { onclick: () => import("./app.js").then((m) => m.whatsNew(true)) }, "sm")),
      this.row("Check for updates", "Daily, from GitHub", this.sw("update_check")),
      this.status("Data folder", null, info.data_dir, isNative ? btn("folder", null, { "data-tip": "Show in Finder", onclick: () => native.reveal(info.data_dir) }, "sm ghost") : null),
      this.status("Projects folder", null, info.workspace),
      this.row("Keyboard shortcuts", null, btn("keyboard", "View", { onclick: () => import("./app.js").then((m) => m.runCommand("shortcuts")) }, "sm")),
      this.row("Quit Tracewright", null, btn("log-out", "Quit", { onclick: async () => {
        if (!await confirmDialog({ title: "Quit Tracewright?", text: "Claude will stop in every project.", ok: "Quit", danger: true })) return;
        if (isNative) { native.quit(); return; }
        await api("/api/quit", { body: {} }).catch(() => {});
        document.body.innerHTML = '<div style="padding:40px;font:15px -apple-system,sans-serif;color:#8b8d96">Tracewright has quit. Open it again from Applications (or run <code>tracewright</code>).</div>';
      } }, "sm danger"))]);
  }
}

function shortAgent(ua) {
  ua = ua || "";
  if (/Tracewright|AppleWebKit.*Mac OS(?!.*Chrome|.*Firefox)/.test(ua) && !/Safari\/[\d.]+$/.test(ua)) return "Tracewright for Mac";
  if (/Chrome\//.test(ua)) return "Chrome"; if (/Firefox\//.test(ua)) return "Firefox"; if (/Safari\//.test(ua)) return "Safari";
  return ua ? ua.slice(0, 40) : "A browser";
}

// How to make a Google OAuth client for Tracewright's sign-in.
function googleGuide(redirect) {
  const step = (n, t, d) => h("div.gg-step", h("span.gg-n", String(n)), h("div", h("b", t), d ? h("div.small.muted", d) : null));
  modal({ title: "Set up Google sign-in", icon: "shield-check", cls: "wide", sub: "Create a free OAuth client in Google Cloud.",
    body: h("div.gg-steps",
      step(1, "Open the Google Cloud console", h("a", { onclick: () => native.openURL("https://console.cloud.google.com/apis/credentials") }, "APIs & Services › Credentials")),
      step(2, "Configure the consent screen", "App name Tracewright, user type External. Add yourself as a test user."),
      step(3, "Create an OAuth client ID", state.info.server_mode ? "Type Web application, with the redirect URI " + redirect : "Type Desktop app."),
      step(4, "Paste the client ID and secret", "Save. The sign-in screen then shows Continue with Google.")) });
}

// The live link needs kicad-python here and KiCad's own "Enable KiCad API" setting.
function liveRow(info, live, redraw) {
  const row = (ok, v, extra) => h("div.srow2", h("div.sl", h("div.st.row", h("span.dot" + (ok === true ? ".ok" : ok === false ? ".err" : "")), "Live link"),
    h("div.sd", "Shows Claude's edits in KiCad as they happen")), h("div.sc", h("span.sv", v), extra || null));
  if (!info.live_api) return row(false, "kicad-python not installed");
  if (live.running) return row(true, `Connected · KiCad ${String(live.version || "").split(" ")[0]}`.trim());
  if (live.enabled === null || live.enabled === undefined) return row(null, "Open KiCad once, then enable its API");
  if (live.enabled) return row(null, live.process ? "Enabled · restart KiCad" : "Enabled");
  return row(false, "Disabled in KiCad", btn(null, "Enable", { onclick: async () => {
    try { const r = await api("/api/kicad/enable-api", { body: {} }); toast(r.already ? "KiCad API already enabled" : "KiCad API enabled. " + r.note, "ok", 8000); redraw(); }
    catch (e) { toast(e.message, "error"); }
  } }, "sm"));
}

export class LessonsPage {
  constructor(root) {
    root.appendChild(topbar([h("div.crumbs-top", h("span.cs", icon("chevron-right", 14)), h("span.cbtn", { style: { cursor: "default" } }, "Lessons"))],
      [btn("settings", "Settings", { onclick: () => go("settings") }, "ghost sm"), btn("layout-grid", "Projects", { onclick: () => go("") }, "ghost sm")]));
    this.page = h("div.page");
    root.appendChild(this.page);
    this.q = "";
    this.render();
  }

  async render() {
    const list = await api("/api/lessons" + (this.q ? `?q=${encodeURIComponent(this.q)}` : ""));
    const inner = h("div.page-inner.narrow");
    const had = this.search && document.activeElement === this.search;
    this.search = h("input", { placeholder: "Search lessons", value: this.q,
      oninput: (e) => { this.q = e.target.value; clearTimeout(this.t); this.t = setTimeout(() => this.render(), 220); } });
    clear(this.page).appendChild(inner);
    inner.appendChild(h("div.page-head", h("div.grow", h("h1", "Lessons"), h("div.sub", "Design knowledge Claude uses in every project.")),
      btn("plus", "New lesson", { onclick: () => this.edit() }, "primary")));
    inner.appendChild(h("div.toolbar", h("div.input-icon", { style: { width: "320px" } }, icon("search", 14), this.search), h("div.grow"), h("span.small.muted", `${list.length} lesson${list.length === 1 ? "" : "s"}`)));
    if (had) setTimeout(() => { this.search.focus(); this.search.setSelectionRange(this.q.length, this.q.length); }, 0);
    for (const l of list) {
      const el = h("div.lesson",
        h("div.lh", { onclick: () => el.classList.toggle("open") }, h("span.chev", icon("chevron-right", 14)), h("span.lt", l.title),
          h("div.tags", (l.tags || []).slice(0, 4).map((t) => h("span.badge", t))),
          btn("pencil", null, { "data-tip": "Edit", onclick: (e) => { e.stopPropagation(); this.edit(l); } }, "sm ghost"),
          btn("trash-2", null, { "data-tip": "Delete", onclick: async (e) => {
            e.stopPropagation();
            if (await confirmDialog({ title: "Delete this lesson?", text: l.title, ok: "Delete", danger: true })) { await api(`/api/lessons/${l.id}`, { method: "DELETE" }); this.render(); }
          } }, "sm ghost")),
        h("div.lb", l.source ? h("div.small.faint", { style: { margin: "8px 0 4px" } }, l.source) : null, h("div.md", { html: markdown(l.body) })));
      inner.appendChild(el);
    }
    if (!list.length) inner.appendChild(h("div.empty", h("div.eicon", icon("book-open", 20)), h("h3", "No matching lessons")));
  }

  edit(l) {
    const title = h("input", { value: l ? l.title : "", placeholder: "Title" });
    const tags = h("input", { value: l ? (l.tags || []).join(", ") : "", placeholder: "kicad, jlc, footprint" });
    const body = h("textarea", { style: { minHeight: "240px" }, placeholder: "What happened and how to avoid it" });
    body.value = l ? l.body : "";
    const m = modal({ title: l ? "Edit lesson" : "New lesson", icon: "book-open", cls: "wide",
      body: [h("div.field", h("label", "Title"), title), h("div.field", h("label", "Tags"), tags), h("div.field", h("label", "Lesson (Markdown)"), body)],
      actions: [h("button.btn", { onclick: () => m.close() }, "Cancel"), btn("check", "Save", { onclick: async () => {
        if (!title.value.trim()) { toast("Add a title", "warn"); return; }
        await api("/api/lessons", { body: { id: l ? l.id : undefined, title: title.value, body: body.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean), source: l ? l.source : undefined } });
        m.close(); this.render();
      } }, "primary")] });
  }
}
