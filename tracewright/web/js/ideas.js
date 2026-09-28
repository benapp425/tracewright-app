// Design ideas, on the home screen: five quick questions (what it should do, how ambitious, how it is
// built, the budget, anything you want in it), then Claude pitches three boards to mock up. Each idea
// can become a project (its brief prefilled), or seed three variations.
import { h, clear, api, toast, btn, copyText } from "./util.js";
import { icon } from "./icons.js";


export class IdeasPanel {
  constructor(page) {
    this.page = page;
    this.answers = JSON.parse(localStorage.getItem("tw.ideas.answers") || "{}");
    this.el = h("section.ideas");
    this.state = "closed"; this.at = 0; this.ideas = null; this.recent = [];
    this.load();
  }

  async load() {
    try {
      const r = await api("/api/ideas");
      this.questions = r.questions || []; this.recent = r.recent || [];
    } catch (e) { this.questions = []; }
    this.render();
  }

  save() { localStorage.setItem("tw.ideas.answers", JSON.stringify(this.answers)); }

  render() {
    const el = clear(this.el);
    el.hidden = this.state === "closed";
    if (el.hidden) return;
    el.appendChild(h("div.id-head", h("div.id-ic", icon("sparkles", 18)), h("div.grow", h("h2", "Project ideas"),
      h("div.sub", "Five questions, three board ideas.")),
      btn("x", null, { onclick: () => { this.state = "closed"; this.render(); }, "data-tip": "Close" }, "sm ghost")));
    if (this.state === "ask") el.appendChild(this.askView());
    else if (this.state === "busy") el.appendChild(this.busyView());
    else if (this.state === "ideas") el.appendChild(this.ideasView(this.ideas));
    if (this.state !== "busy" && this.recent.length && this.state !== "ideas")
      el.appendChild(h("div.id-recent", h("div.label", "Recent ideas"), h("div.id-strip", this.recent.slice(0, 8).map((i) => this.mini(i)))));
  }

  // ------------------------------------------------------------------ the questions
  askView() {
    const qs = this.questions, q = qs[this.at];
    if (!q) return h("div.small.muted", "Ideas are not available right now.");
    const a = this.answers;
    const body = h("div.id-q");
    body.appendChild(h("div.id-qt", h("span.id-qn", `${this.at + 1} of ${qs.length}`), q.question));
    if (q.kind === "multi") {
      const sel = new Set(a[q.id] || []);
      body.appendChild(h("div.id-chips", q.options.map(([v, label, ic]) => h("button.id-chip" + (sel.has(v) ? ".on" : ""), {
        onclick: (e) => { sel.has(v) ? sel.delete(v) : sel.add(v); a[q.id] = [...sel]; this.save(); e.currentTarget.classList.toggle("on", sel.has(v)); } },
        icon(ic || "circle-dot", 14), label))));
    } else if (q.kind === "single") {
      body.appendChild(h("div.id-opts", q.options.map(([v, label, d]) => h("button.id-opt" + (a[q.id] === v ? ".on" : ""), {
        onclick: () => { a[q.id] = v; this.save(); this.next(); } }, h("b", label), d ? h("span", d) : null))));
    } else {
      const t = h("input.id-text", { placeholder: q.placeholder || "", value: a[q.id] || "",
        oninput: (e) => { a[q.id] = e.target.value; this.save(); }, onkeydown: (e) => { if (e.key === "Enter") this.next(); } });
      body.appendChild(t);
      setTimeout(() => t.focus(), 30);
    }
    const last = this.at === qs.length - 1;
    body.appendChild(h("div.id-nav", h("div.id-dots", qs.map((_, i) => h("i" + (i === this.at ? ".on" : i < this.at ? ".done" : "")))), h("div.grow"),
      this.at ? h("button.btn.sm.ghost", { onclick: () => { this.at--; this.render(); } }, icon("chevron-left", 13), "Back") : null,
      h("button.btn.sm" + (last ? ".primary" : ""), { onclick: () => this.next() }, last ? "Generate ideas" : "Next", icon(last ? "sparkles" : "arrow-right", 13))));
    return body;
  }

  next() {
    if (this.at < this.questions.length - 1) { this.at++; this.render(); return; }
    this.generate();
  }

  async generate(seed) {
    this.state = "busy"; this.render();
    try {
      const r = await api("/api/ideas", { body: { answers: this.answers, seed } });
      this.ideas = r.ideas || [];
      this.recent = [...this.ideas, ...this.recent].slice(0, 12);
      this.state = "ideas";
    } catch (e) { toast(e.message, "error"); this.state = "ask"; }
    this.render();
    this.el.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  busyView() {
    return h("div.id-busy", h("div.id-think", h("span.spinner"), h("span", "Generating ideas…")), h("div.id-grid", [0, 1, 2].map((k) => h("div.id-card.skel", { style: { animationDelay: `${k * 0.12}s` } },
      h("i.l1"), h("i.l2"), h("i.l3"), h("i.l4")))));
  }

  // ------------------------------------------------------------------ the ideas
  ideasView(ideas) {
    return h("div.id-results", h("div.id-grid", ideas.map((it, k) => this.card(it, k))),
      h("div.id-more", btn("refresh-cw", "More ideas", { onclick: () => this.generate() }, "sm"),
        btn("sliders-horizontal", "Edit answers", { onclick: () => { this.state = "ask"; this.at = 0; this.render(); } }, "sm ghost")));
  }

  card(it, k = 0) {
    const dots = h("span.id-diff", { "data-tip": `Difficulty ${it.difficulty}/5` }, [1, 2, 3, 4, 5].map((n) => h("i" + (n <= it.difficulty ? ".on" : ""))));
    return h("div.id-card", { style: { animationDelay: `${k * 0.08}s` } },
      h("div.id-ct", h("b", it.title), h("span.id-src." + it.sourcing, it.sourcing === "self" ? "Hand-built" : "JLC turnkey")),
      h("div.id-pitch", it.pitch),
      it.wow ? h("div.id-wow", icon("zap", 12), it.wow) : null,
      h("div.id-meta", dots, it.size ? h("span", icon("ruler", 11), it.size) : null, h("span", icon("layers", 11), `${it.layers} layers`),
        it.cost ? h("span", icon("shopping-cart", 11), `≈ $${Math.round(it.cost)} for 5`) : null),
      it.parts.length ? h("div.id-parts", it.parts.map((p) => h("span.id-part", { "data-tip": p.role }, p.name))) : null,
      it.features.length ? h("ul.id-feat", it.features.map((f) => h("li", f))) : null,
      it.skills.length ? h("div.id-skills", h("span.muted", "Skills: "), it.skills.join(" · ")) : null,
      h("div.id-act", btn("circuit-board", "Create project", { onclick: () => this.page.newDialog({ name: it.title, brief: it.brief, layers: it.layers, assembly: it.sourcing !== "self" }) }, "primary sm"),
        btn("git-branch", "Variations", { onclick: () => this.generate({ title: it.title, pitch: it.pitch }), "data-tip": "Similar ideas" }, "sm"),
        btn("copy", null, { onclick: () => { copyText(it.brief); toast("Brief copied", "ok"); }, "data-tip": "Copy brief" }, "sm ghost")));
  }

  mini(it) {
    return h("div.id-mini", { onclick: () => { this.ideas = [it]; this.state = "ideas"; this.render(); } },
      h("b", it.title), h("span", it.pitch), h("span.id-src." + (it.sourcing || "jlc"), it.sourcing === "self" ? "Hand-built" : "JLC turnkey"));
  }
}
