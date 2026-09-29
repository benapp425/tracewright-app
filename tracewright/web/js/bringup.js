// Bring-up at the bench: the plan Claude wrote (docs/bring-up.md) as a checklist for the board in front
// of you. Tick each step; where the plan names a value, type what you measured and it says whether it
// is in range. Each board you build keeps its own results (Board 1, 2...); export them as a table.
import { h, clear, api, toast, btn, promptDialog } from "./util.js";
import { icon } from "./icons.js";
import { native } from "./native.js";

const enc = encodeURIComponent;
const PREFIX = { p: 1e-12, n: 1e-9, u: 1e-6, "µ": 1e-6, m: 1e-3, k: 1e3, M: 1e6, G: 1e9 };
const BASES = ["V", "A", "Ω", "Hz", "W", "s", "F", "°C", "%"];

// "mV" -> ["V", 1e-3], a bare prefix "k" -> [null, 1e3]; null when it is not a unit (as bringup.py reads it)
function unitOf(u) {
  u = u.replace(/ohms?$/i, "Ω");
  if (BASES.includes(u)) return [u, 1];
  if (u.length > 1 && PREFIX[u[0]] && BASES.includes(u.slice(1))) return [u.slice(1), PREFIX[u[0]]];
  if (PREFIX[u]) return [null, PREFIX[u]];
  return null;
}

// a typed reading in the step's base unit: "3.29 V", "148mA", "4.7k" (the step's unit), or a bare "148" (the
// unit the plan wrote); null when it does not parse or is in another unit
export function readingOf(text, e) {
  const m = String(text || "").trim().match(/^(-?\d+(?:\.\d+)?)\s*([a-zA-Zµ°Ω%]*)$/);
  if (!m) return null;
  const x = parseFloat(m[1]);
  if (!m[2]) return x * ((e.shown && unitOf(e.shown)) || [null, 1])[1];
  const u = unitOf(m[2]);
  if (!u || (u[0] !== null && u[0] !== e.unit)) return null;
  return x * u[1];
}

export function verdictOf(text, e) {
  if (!e || !String(text || "").trim()) return null;
  const x = readingOf(text, e);
  if (x === null) return "unread";
  return (e.lo === null || x >= e.lo - 1e-12) && (e.hi === null || x <= e.hi + 1e-12) ? "pass" : "fail";
}

export class BringUpView {
  constructor(ws, el) { this.ws = ws; this.pid = ws.pid; this.el = el; this.load(); }

  async load() {
    try { this.d = await api(`/api/projects/${enc(this.pid)}/bringup`); } catch (e) { clear(this.el).appendChild(h("div.small.muted", e.message)); return; }
    this.render();
  }

  render() {
    const d = this.d, el = clear(this.el);
    if (!d.exists || !d.total) {
      el.appendChild(h("div.empty", h("div.eicon", icon("list-checks", 22)), h("h3", d.exists ? "The bring-up plan has no steps" : "No bring-up plan yet"),
        h("p", "A checklist for testing the built board, with expected values."),
        btn("file-text", "Write the bring-up plan", { onclick: () => this.ws.ask("Write docs/bring-up.md: what only the built board can show, in the order to test it, as '- [ ]' steps grouped in '## ' sections, each with the value to expect (for example 'TP3 3V3 = 3.30 V ± 2 %', 'idle current < 50 mA').") }, "primary")));
      return;
    }
    const res = d.results || {};
    const pct = d.total ? Math.round(100 * d.done / d.total) : 0;
    const boards = h("div.seg.bu-boards", d.boards.map((b) => h("button" + (b === d.current ? ".on" : ""), { onclick: () => this.select(b) }, `Board ${b}`)),
      h("button", { onclick: () => this.addBoard(), "data-tip": "Add board" }, icon("plus", 12)));
    el.appendChild(h("div.bu-head",
      h("div.grow", h("div.bu-title", icon("list-checks", 16), h("b", "Bring-up"), h("span.muted", "docs/bring-up.md")),
        h("div.bu-prog", h("div.bu-bar", h("i", { style: { width: pct + "%" } })), h("span", `${d.done} of ${d.total} steps on board ${d.current}`),
          d.fails ? h("span.bu-fail", icon("circle-alert", 12), `${d.fails} out of range`) : null)),
      boards,
      btn("download", "Results", { onclick: () => native.save(`/api/projects/${enc(this.pid)}/bringup/report`), "data-tip": "Export results (Markdown)" }, "sm")));
    for (const s of d.sections) {
      const n = s.items.filter((it) => (res[it.key] || {}).done).length;
      const sec = h("section.bu-sec" + (n === s.items.length ? ".done" : ""), h("div.bu-st", h("span", s.title), h("span.bu-n", `${n}/${s.items.length}`)));
      for (const it of s.items) sec.appendChild(this.row(it, res[it.key] || {}));
      el.appendChild(sec);
    }
  }

  row(it, r) {
    const e = it.expects[0];
    const cb = h("input", { type: "checkbox", checked: !!r.done, onchange: () => this.save(it.key, { done: cb.checked }, row) });
    const val = e ? h("input.bu-val", { placeholder: e.shown || e.unit, value: r.value || "", "data-tip": `Expect ${e.label}`,
      onchange: () => { this.save(it.key, { value: val.value, ...(val.value && !cb.checked ? { done: true } : {}) }, row); if (val.value) cb.checked = true; paint(); },
      oninput: () => paint() }) : null;
    const tag = h("span.bu-v");
    const paint = () => {
      const v = verdictOf(val ? val.value : null, e);
      tag.className = "bu-v" + (v === "pass" || v === "fail" ? " " + v : "");
      const want = e ? `expect ${e.label.replace(/^[=<>≈~]\s*/, (x) => x.trim() + " ")}` : "";
      clear(tag).append(v === "pass" ? icon("check", 11) : v === "fail" ? icon("x", 11) : null,
        v === "pass" ? "in range" : v === "fail" ? "out of range" : v === "unread" ? `in ${e.shown || e.unit}? ${want}` : want);
      row.classList.toggle("fail", v === "fail");
    };
    const row = h("label.bu-row" + (r.done ? ".on" : ""), cb, h("div.grow", h("div.bu-text", it.text)), val ? h("div.bu-meas", val, tag) : null);
    if (val) paint();
    return row;
  }

  async save(key, fields, row) {
    try {
      const r = await api(`/api/projects/${enc(this.pid)}/bringup`, { body: { board: this.d.current, key, ...fields } });
      this.d.results[key] = r;
      row.classList.toggle("on", !!r.done);
      this.d.done = Object.values(this.d.results).filter((x) => x.done).length;
      const bar = this.el.querySelector(".bu-bar i"), txt = this.el.querySelector(".bu-prog > span");
      if (bar) bar.style.width = `${this.d.total ? Math.round(100 * this.d.done / this.d.total) : 0}%`;
      if (txt) txt.textContent = `${this.d.done} of ${this.d.total} steps on board ${this.d.current}`;
    } catch (e) { toast(e.message, "error"); }
  }

  async select(b) { this.d = await api(`/api/projects/${enc(this.pid)}/bringup`, { body: { select: b } }); this.render(); }

  async addBoard() {
    const serial = await promptDialog({ title: "Add board", label: "Serial number or name", value: String(this.d.boards.length + 1), ok: "Add" });
    if (!serial) return;
    this.d = await api(`/api/projects/${enc(this.pid)}/bringup`, { body: { add_board: serial } });
    this.render();
  }
}
