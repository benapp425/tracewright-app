// First run, once per account: what Tracewright is, whether this Mac has what it needs (KiCad, Claude),
// how it should look, how Claude should work, and a first board to open. Every step can be skipped;
// Settings has all of it later.
import { h, clear, api, toast, btn } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";
import { BRAND, mark } from "./brand.js";
import { state, setTheme, setPalette, PALETTES, resolvedTheme, go } from "./app.js";

const STEPS = ["welcome", "setup", "look", "work", "start"];

export class Onboarding {
  constructor(root, user, done) {
    this.root = root; this.user = user; this.done = done; this.at = 0;
    this.prefs = { run_mode: "autonomous", model: null };
    root.appendChild(this.el = h("div.onb", { "data-drag": "" }));
    this.render();
    this.onKey = (e) => { if (e.key === "Enter" && e.target.tagName !== "INPUT" && e.target.tagName !== "BUTTON") this.next(); };
    addEventListener("keydown", this.onKey);
  }

  destroy() { removeEventListener("keydown", this.onKey); }

  async render() {
    const step = STEPS[this.at];
    clear(this.el);
    const card = h("div.onb-card");
    this.el.append(h("div.onb-top", mark(26), h("span.onb-brand", BRAND.name), h("div.grow"),
      h("button.linkbtn.onb-skip", { onclick: () => this.finish(null) }, "Skip setup")), card);
    const dots = h("div.onb-dots", STEPS.map((s, i) => h("i" + (i === this.at ? ".on" : i < this.at ? ".done" : ""))));
    const body = h("div.onb-body");
    const nav = h("div.onb-nav", dots, h("div.grow"),
      this.at ? h("button.btn.ghost", { onclick: () => { this.at--; this.render(); } }, icon("chevron-left", 14), "Back") : null,
      step !== "start" ? h("button.btn.primary", { onclick: () => this.next() }, step === "welcome" ? "Get started" : "Continue", icon("arrow-right", 14)) : null);
    card.append(body, nav);
    await this[step](body);
  }

  next() { if (this.at < STEPS.length - 1) { this.at++; this.render(); } }

  welcome(b) {
    const first = (this.user.name || "").split(" ")[0];
    b.append(h("div.onb-hero", mark(72, true)),
      h("h1", first ? `Welcome, ${first}` : `Welcome to ${BRAND.name}`),
      h("p.onb-lead", BRAND.pitch),
      h("div.onb-cards", [
        ["message-square", "Describe it", "Claude turns your requirements into a schematic and layout."],
        ["circuit-board", "Watch it build", "Placement and routing appear live, in KiCad too."],
        ["list-checks", "Check and order", `${(state.info && state.info.check_count) || 50} design checks, then one click to the fab.`],
      ].map(([ic, t, d], i) => h("div.onb-feat", { style: { animationDelay: `${0.1 + i * 0.08}s` } }, h("span.onb-fi", icon(ic, 18)), h("b", t), h("span", d)))));
  }

  async setup(b) {
    b.append(h("h1", "Check your setup"), h("p.onb-lead", "Tracewright uses KiCad and Claude."));
    const list = h("div.onb-checks", h("div.onb-loading", h("span.spinner"), "Checking…"));
    b.append(list);
    let info = {};
    try { info = await api("/api/info"); state.info = info; } catch (e) { /* shown as missing */ }
    const k = info.kicad || {};
    const rows = [
      [!!k.cli, "KiCad", k.cli ? `Version ${k.version || ""}` : "Not found. Install KiCad 9 or 10.",
        k.cli ? null : btn("external-link", "Download KiCad", { onclick: () => native.openURL("https://www.kicad.org/download/") }, "sm")],
      [!!info.claude_auth, "Claude", info.claude_auth === "api_key" ? "Connected with an API key" : info.claude_auth ? "Connected with Claude Code"
        : "Not connected. Sign in to Claude Code, or add an API key.",
        info.claude_auth ? null : h("div.onb-key", this.keyIn = h("input", { type: "password", placeholder: "Anthropic API key" }),
          btn("check", "Save", { onclick: () => this.saveKey() }, "sm"))],
      [!!info.git, "Git", info.git ? "Project history enabled" : "Not found. Install the Xcode command line tools.", null],
      [!!(info.java || info.freerouting), "Freerouting (optional)", info.java || info.freerouting ? "Available" : "Not installed. The built-in router handles most boards.", null],
    ];
    clear(list).append(...rows.map(([ok, t, d, fix]) => h("div.onb-row" + (ok ? ".ok" : t.includes("optional") ? ".opt" : ".bad"),
      h("span.onb-ri", icon(ok ? "check" : t.includes("optional") ? "minus" : "circle-alert", 13)), h("div.grow", h("b", t), h("span", d)), fix)));
    if (!k.cli || !info.claude_auth) b.appendChild(h("div.onb-hint", icon("info", 13), "You can finish this later in Settings."));
  }

  async saveKey() {
    const v = this.keyIn.value.trim();
    if (!v) return;
    try { await api("/api/settings", { method: "PATCH", body: { anthropic_api_key: v } }); toast("API key saved", "ok"); this.render(); }
    catch (e) { toast(e.message, "error"); }
  }

  look(b) {
    b.append(h("h1", "Appearance"), h("p.onb-lead", "You can change these any time in Settings."));
    const themes = h("div.onb-themes", [["system", "System", "laptop"], ["dark", "Dark", "moon"], ["light", "Light", "sun"]].map(([t, l, ic]) =>
      h("button.onb-theme" + (state.theme === t ? ".on" : ""), { onclick: () => { setTheme(t); this.render(); } },
        h("div.onb-tprev." + (t === "system" ? resolvedTheme() : t), h("i"), h("i"), h("i")), icon(ic, 14), l)));
    const pals = h("div.palettes.onb-pals", Object.entries(PALETTES).map(([k, p]) => h("button.pal" + (state.palette === k ? ".on" : ""),
      { onclick: () => { setPalette(k); this.render(); } }, h("i", { style: { background: p[resolvedTheme()][0] } }), p.name)));
    b.append(h("div.onb-label", "Theme"), themes, h("div.onb-label", "Accent color"), pals);
  }

  work(b) {
    b.append(h("h1", "Workflow"), h("p.onb-lead", "The default for new projects. You can change it per project."));
    const pick = (v) => { this.prefs.run_mode = v; this.render(); };
    b.append(h("div.onb-modes", [
      ["autonomous", "zap", "Autonomous", "Claude asks its questions first, then completes the design and records each decision."],
      ["check_in", "messages-square", "Step by step", "Claude waits for your approval after each stage."],
    ].map(([v, ic, t, d]) => h("button.onb-mode" + (this.prefs.run_mode === v ? ".on" : ""), { onclick: () => pick(v) },
      h("span.onb-mi", icon(ic, 18)), h("div", h("b", t), h("span", d)), h("span.onb-radio")))));
  }

  start(b) {
    b.append(h("h1", "Get started"), h("p.onb-lead", "Choose how to begin."));
    const go_ = (what) => this.finish(what);
    b.append(h("div.onb-starts", [
      ["circuit-board", "Open the demo", "A finished USB-C ATtiny85 board.", "demo"],
      ["folder-input", "Import a project", "Bring in an existing KiCad design.", "import"],
      ["plus", "New project", "Start from a description.", "new"],
      ["sparkles", "Project ideas", "Five questions, three ideas.", "ideas"],
    ].map(([ic, t, d, what], i) => h("button.onb-start", { onclick: () => go_(what), style: { animationDelay: `${i * 0.06}s` } },
      h("span.onb-si", icon(ic, 20)), h("b", t), h("span", d), icon("arrow-right", 15)))));
  }

  async finish(what) {
    try {
      const u = await api("/api/auth/me", { method: "PATCH", body: { onboarded: true, prefs: { run_mode: this.prefs.run_mode, seen_version: (state.info || {}).version } } });
      state.auth.user = u;
    } catch (e) { /* not fatal: it shows again next time */ }
    this.el.classList.add("out");
    setTimeout(() => this.done(what), 250);
  }
}
