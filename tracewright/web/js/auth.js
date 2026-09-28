// The sign-in screen: create an account or sign in, with an email and a password or with Google. The
// brand on the left (the mark, what Tracewright does, traces drawing themselves across the panel), the
// form on the right. Google sign-in happens in the system browser; this window waits for it.
import { h, clear, api, toast } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";
import { BRAND, mark } from "./brand.js";

const POINTS = [
  ["sparkles", "Live in KiCad", "Claude places and routes while you watch."],
  ["list-checks", "36 design checks", "Each one tested against a known fault."],
  ["shopping-cart", "Ready to order", "Send to JLCPCB or PCBWay in one click."],
];

// Google in the system browser; this window polls until that sign-in finishes. Resolves to the signed-in
// user (null when cancelled: p.cancel()). body.link: link Google to the signed-in account (Settings).
export function googleFlow(body = {}, remember = true) {
  let timer, stop = false;
  const p = (async () => {
    const s = await api("/api/auth/google/start", { body });
    if (isNative) native.openURL(s.url); else window.open(s.url, "_blank", "noopener");
    const t0 = Date.now();
    while (!stop) {
      await new Promise((r) => { timer = setTimeout(r, 1500); });
      if (stop) break;
      if (Date.now() - t0 > 10 * 60 * 1000) throw new Error("Google sign-in timed out. Try again.");
      let d;
      try {
        const r = await fetch("/api/auth/google/poll", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ poll: s.poll, secret: s.secret, remember }) });
        d = await r.json();
      } catch (e) { continue; }                          // the next poll tries again
      if (d.status === "pending") continue;
      if (d.ok && d.user) { native.activate(); return d.user; }
      throw new Error(d.error || "Google sign-in did not finish.");
    }
    return null;
  })();
  p.cancel = () => { stop = true; clearTimeout(timer); };
  return p;
}

// Password strength, 0-4, from length and variety (a nudge, not a gate: the server has the rules).
function strength(pw) {
  let s = 0;
  if (pw.length >= 8) s++;
  if (pw.length >= 12) s++;
  if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++;
  return Math.min(4, s);
}

export class AuthScreen {
  // st: /api/auth/state; done(user): called once signed in
  constructor(root, st, done) {
    this.root = root; this.st = st; this.done = done;
    this.mode = st.accounts === 0 ? "register" : "signin";
    root.appendChild(this.el = h("div.auth", { "data-drag": "" }));
    this.render();
  }

  destroy() { clearInterval(this.pollT); }

  render() {
    const st = this.st, reg = this.mode === "register";
    clear(this.el);
    const traces = h("div.auth-traces", { html: tracesSVG() });
    const brand = h("section.auth-brand", traces,
      h("div.auth-brand-in",
        h("div.auth-logo", mark(56, true), h("div", h("div.auth-name", BRAND.name), h("div.auth-tag", BRAND.tagline))),
        h("div.auth-points", POINTS.map(([ic, t, d], i) => h("div.auth-pt", { style: { animationDelay: `${0.5 + i * 0.12}s` } },
          h("span.auth-pti", icon(ic, 16)), h("div", h("b", t), h("span", d))))),
        h("div.auth-foot", `Version ${st.version || ""}`)));
    this.err = h("div.auth-err");
    const email = h("input", { type: "email", placeholder: "you@example.com", autocomplete: "email", required: true, value: this.email || "" });
    const name = h("input", { type: "text", placeholder: "Your name", autocomplete: "name" });
    const pw = h("input", { type: "password", placeholder: reg ? "At least 8 characters" : "Password", autocomplete: reg ? "new-password" : "current-password", required: true });
    const eye = h("button.auth-eye", { type: "button", "data-tip": "Show the password", onclick: () => { pw.type = pw.type === "password" ? "text" : "password"; clear(eye).appendChild(icon(pw.type === "password" ? "eye" : "eye-off", 15)); } }, icon("eye", 15));
    const meter = h("div.auth-meter", [0, 1, 2, 3].map(() => h("i")));
    const meterText = h("span.auth-mt");
    if (reg) pw.addEventListener("input", () => {
      const s = pw.value ? strength(pw.value) : -1;
      meter.dataset.s = String(s);
      meterText.textContent = s < 0 ? "" : ["Too short", "Weak", "Fair", "Good", "Strong"][s];
    });
    const remember = h("input", { type: "checkbox", checked: true });
    // a new server's first account takes the password the server was started with (its admin knows it)
    const spw = reg && st.claim ? h("input", { type: "password", placeholder: "The password this server was started with", autocomplete: "off", required: true }) : null;
    const go = h("button.btn.primary.auth-go", { type: "submit" }, reg ? "Create account" : "Sign in", icon("arrow-right", 15));
    const google = st.google && !st.claim ? h("button.auth-google", { type: "button", onclick: () => this.google(remember.checked) }, googleG(), "Continue with Google") : null;
    const forgot = reg ? null : h("button.linkbtn.auth-forgot", { type: "button", onclick: () => { this.email = email.value; this.forgot(); } }, "Forgot?");
    const form = h("form.auth-form", { onsubmit: (e) => { e.preventDefault(); this.submit({ email: email.value, name: name.value, password: pw.value, remember: remember.checked, ...(spw ? { server_password: spw.value } : {}) }, go); } },
      h("div.auth-h", h("h1", reg ? (st.accounts === 0 ? `Welcome to ${BRAND.name}` : "Create your account") : "Welcome back"),
        reg ? (st.accounts === 0 ? h("p", `Create the owner account for this ${st.server_mode ? "server" : "Mac"}.`) : null) : h("p", "Sign in to continue.")),
      google, google ? h("div.auth-or", h("span", "or")) : null,
      reg ? h("label.auth-f", h("span", "Name"), name) : null,
      h("label.auth-f", h("span", "Email"), email),
      h("label.auth-f", h("span.auth-fl", "Password", forgot), h("div.auth-pw", pw, eye), reg ? h("div.auth-strength", meter, meterText) : null),
      spw ? h("label.auth-f", h("span", "Server password"), spw,
        h("div.auth-hint", "Needed for the first account only: the password set with TW_PASSWORD or `tracewright password`.")) : null,
      h("label.auth-rem", remember, h("span", "Keep me signed in")),
      this.err, go,
      st.signups || !reg ? h("div.auth-switch", reg ? "Already have an account?" : "New to Tracewright?",
        (reg || st.signups) ? h("button.linkbtn", { type: "button", onclick: () => { this.email = email.value; this.mode = reg ? "signin" : "register"; this.render(); } }, reg ? "Sign in" : "Create an account") : null) : null,
      h("div.auth-privacy", icon("shield-check", 12), st.server_mode ? "Your account is stored on this server." : "Your account is stored on this Mac."));
    this.waitEl = h("div.auth-wait");
    this.el.append(brand, this.main = h("section.auth-main", h("div.auth-card", form, this.waitEl)));
    if (this.notice) { this.err.before(h("div.auth-ok", icon("circle-check", 14), this.notice)); this.notice = null; }
    setTimeout(() => (reg ? name : email.value ? pw : email).focus(), 60);
  }

  // A forgotten password. Tracewright sends no email: the account lives on this Mac (or server), so the
  // one who owns the machine resets it. In the Mac app: Touch ID or the Mac's password. Elsewhere: the
  // `tracewright account reset` command, on the machine itself.
  forgot() {
    const st = this.st;
    const email = h("input", { type: "email", placeholder: "you@example.com", autocomplete: "email", required: true, value: this.email || "" });
    const back = h("button.linkbtn", { type: "button", onclick: () => { this.email = email.value; this.render(); } }, "Back to sign in");
    const err = h("div.auth-err");
    const googleNote = st.google ? h("div.auth-note", icon("info", 12), "Accounts linked to Google can also use Continue with Google.") : null;
    let body;
    if (isNative && !st.server_mode) {
      const pw = h("input", { type: "password", placeholder: "At least 8 characters", autocomplete: "new-password", required: true });
      const go = h("button.btn.primary.auth-go", { type: "submit" }, icon("fingerprint", 16), "Reset with Touch ID");
      body = h("form.auth-form", { onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        go.disabled = true; go.classList.add("busy");
        try {
          const r = await native.resetPassword(email.value.trim(), pw.value);
          if (r && r.ok) { this.email = email.value.trim(); this.notice = "Password updated. Sign in with your new password."; this.mode = "signin"; this.render(); return; }
          err.textContent = !r ? "Couldn't reach the Mac app. Update Tracewright and try again." : r.cancelled ? "Not confirmed."
            : (r.message || "The password could not be reset.").split("\n").pop();
        } finally { go.disabled = false; go.classList.remove("busy"); }
      } },
        h("div.auth-h", h("h1", "Reset password"), h("p", "Confirm with Touch ID or your Mac password to set a new one.")),
        h("label.auth-f", h("span", "Email"), email),
        h("label.auth-f", h("span", "New password"), pw),
        err, go, googleNote, h("div.auth-switch", back));
    } else {
      const cmd = () => `${st.server_mode ? "tracewright" : "~/.local/bin/tracewright"} account reset ${email.value.trim() || "you@example.com"}`;
      const code = h("code.auth-cmd-t", cmd());
      email.addEventListener("input", () => { code.textContent = cmd(); });
      const copy = h("button.btn.sm.ghost", { type: "button", onclick: () => { navigator.clipboard.writeText(cmd()).then(() => toast("Copied", "ok", 1400)).catch(() => {}); } }, icon("copy", 13), "Copy");
      body = h("div.auth-form",
        h("div.auth-h", h("h1", "Reset password"), h("p", st.server_mode
          ? "Ask the server's owner, or run this on the server:"
          : "Run this in Terminal to set a new password:")),
        h("label.auth-f", h("span", "Email"), email),
        h("div.auth-cmd", icon("terminal", 14), code, copy),
        googleNote, h("div.auth-switch", back));
    }
    clear(this.main).appendChild(h("div.auth-card.auth-reset", body));
    setTimeout(() => (email.value ? body.querySelector("input[type=password]") || email : email).focus(), 60);
  }

  async submit(body, btn) {
    this.err.textContent = "";
    btn.disabled = true; btn.classList.add("busy");
    try {
      const r = await api(this.mode === "register" ? "/api/auth/register" : "/api/auth/login", { body });
      this.finish(r.user);
    } catch (e) {
      this.err.textContent = e.message.replace(/^\w/, (c) => c.toUpperCase());
      this.el.querySelector(".auth-card").classList.remove("shake"); void this.el.offsetWidth; this.el.querySelector(".auth-card").classList.add("shake");
    } finally { btn.disabled = false; btn.classList.remove("busy"); }
  }

  // Google: sign in in the system browser; this window waits, then collects its session.
  async google(remember) {
    this.err.textContent = "";
    this.stopWait();
    const flow = this.flow = googleFlow({}, remember);
    clear(this.waitEl).append(h("div.auth-waitin", h("span.spinner"), h("div", h("b", "Continue in your browser"),
      h("span", "This window updates when you're done.")), h("button.btn.sm.ghost", { onclick: () => this.stopWait() }, "Cancel")));
    this.el.classList.add("waiting");
    try {
      const u = await flow;
      if (flow !== this.flow) return;                    // cancelled, or another attempt started
      this.stopWait();
      if (u) this.finish(u);
    } catch (e) {
      if (flow !== this.flow) return;
      this.stopWait();
      this.err.textContent = e.message.replace(/^\w/, (c) => c.toUpperCase());
    }
  }

  stopWait() { if (this.flow) this.flow.cancel(); this.flow = null; clear(this.waitEl); this.el.classList.remove("waiting"); }

  finish(user) {
    this.el.classList.add("out");
    toast(`Signed in as ${user.name || user.email}`, "ok", 2200);
    setTimeout(() => this.done(user), 280);
  }
}

// Google's "G", in its colours (brand guidelines: the mark unchanged, on white).
function googleG() {
  return h("span.gg", { html: `<svg width="18" height="18" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.6-.4-3.9z"/><path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 38.2 44 33 44 24c0-1.3-.1-2.6-.4-3.9z"/></svg>` });
}

// Circuit traces that draw themselves across the brand panel (static when motion is reduced).
function tracesSVG() {
  const paths = [];
  for (let i = 0; i < 14; i++) {
    const y = 40 + i * 58, x0 = -20, dx = 120 + (i * 37) % 160;
    const up = i % 2 ? -1 : 1;
    const d = `M${x0} ${y} H${dx} l${40} ${40 * up} H${dx + 260 + (i * 53) % 200} l${30} ${-30 * up} H${1100}`;
    paths.push(`<path d="${d}" pathLength="1" style="animation-delay:${(i * 0.23).toFixed(2)}s"/>`);
    paths.push(`<circle cx="${dx + 260 + (i * 53) % 200}" cy="${y + 40 * up}" r="5" style="animation-delay:${(i * 0.23 + 1.1).toFixed(2)}s"/>`);
  }
  return `<svg viewBox="0 0 1000 860" preserveAspectRatio="xMidYMid slice">${paths.join("")}</svg>`;
}
