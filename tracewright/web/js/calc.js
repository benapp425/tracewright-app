// The calculators: the numbers a board needs, worked out with this board's own stack-up -- trace width
// for a current (IPC-2221), via current, impedance (IPC-2141 microstrip and stripline, single and
// differential), dividers with real resistor values, LED resistors, RC filters, crystal load
// capacitors, regulator heat and battery life. Values accept SI prefixes (4k7, 100n, 2.2u, 3M3).
import { h, clear, api, btn } from "./util.js";
import { icon } from "./icons.js";

const PREFIX = { p: 1e-12, n: 1e-9, u: 1e-6, "µ": 1e-6, m: 1e-3, k: 1e3, K: 1e3, M: 1e6, G: 1e9 };
export function parseSI(s) {
  s = String(s ?? "").trim().replace(/[ΩΩVAFHzW°C\s]/g, "").replace(/ohms?$/i, "");
  let m = s.match(/^([-+]?\d*\.?\d+)([pnuµmkKMG])(\d*)$/);            // 4k7, 2u2, 100n
  if (m) return parseFloat(m[1] + (m[3] ? "." + m[3] : "")) * PREFIX[m[2]];
  m = s.match(/^([-+]?\d*\.?\d+(?:e[-+]?\d+)?)$/i);
  return m ? parseFloat(m[1]) : NaN;
}
export function fmtSI(v, unit = "", digits = 3) {
  if (!isFinite(v)) return "–";
  const a = Math.abs(v);
  const [f, p] = a >= 1e9 ? [1e9, "G"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "k"] : a >= 1 || a === 0 ? [1, ""] : a >= 1e-3 ? [1e-3, "m"] : a >= 1e-6 ? [1e-6, "µ"] : a >= 1e-9 ? [1e-9, "n"] : [1e-12, "p"];
  return `${Number((v / f).toPrecision(digits))} ${p}${unit}`.trim();
}
const E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1];
const E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2];
const E96 = Array.from({ length: 96 }, (_, i) => Math.round(Math.pow(10, i / 96) * 100) / 100);
const series = (E, lo, hi) => { const out = []; for (let d = -12; d <= 9; d++) for (const b of E) { const v = b * Math.pow(10, d); if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v); } return out; };
const nearest = (v, E, up) => { const all = series(E, 1e-13, 1e10); return up ? all.find((x) => x >= v * 0.9999) : all.reduce((a, b) => Math.abs(b - v) < Math.abs(a - v) ? b : a); };
const OZ_MM = 0.035, MIL = 0.0254, RHO = 1.72e-8;

// IPC-2221: I = k dT^0.44 A^0.725 (A in mil²)
const ipcArea = (I, dT, k) => Math.pow(I / (k * Math.pow(dT, 0.44)), 1 / 0.725);
const ipcI = (A, dT, k) => k * Math.pow(dT, 0.44) * Math.pow(A, 0.725);
// IPC-2141
const microstrip = (w, h, t, er) => 87 / Math.sqrt(er + 1.41) * Math.log(5.98 * h / (0.8 * w + t));
const stripline = (w, b, t, er) => 60 / Math.sqrt(er) * Math.log(4 * b / (0.67 * Math.PI * (0.8 * w + t)));
const solve = (f, target, lo = 0.02, hi = 6) => { for (let i = 0; i < 80; i++) { const m = (lo + hi) / 2; if (f(m) > target) lo = m; else hi = m; } return (lo + hi) / 2; };

export const CALCS = [
  { id: "trace", title: "Trace width for a current", icon: "route", note: "IPC-2221. Conservative; IPC-2152 allows more near planes.",
    inputs: (c) => [["i", "Current", "A", "1"], ["dt", "Temperature rise", "°C", "10"], ["oz", "Copper", "oz", String(c.oz || 1)], ["layer", "Layer", "", "outer", ["outer", "inner"]], ["len", "Length (for the drop)", "mm", "50"]],
    run: (v) => {
      const I = parseSI(v.i), dT = parseSI(v.dt), oz = parseSI(v.oz), k = v.layer === "inner" ? 0.024 : 0.048;
      const tMil = oz * OZ_MM / MIL, A = ipcArea(I, dT, k), wMil = A / tMil, w = wMil * MIL;
      const R = RHO * (parseSI(v.len) / 1000) / (w / 1000 * oz * OZ_MM / 1000);
      return [["Width", `${w.toFixed(3)} mm`, `${wMil.toFixed(1)} mil`, true], ["Resistance", fmtSI(R, "Ω"), `over ${v.len} mm`], ["Voltage drop", fmtSI(R * I, "V"), `${fmtSI(R * I * I, "W")} heat`]];
    } },
  { id: "via", title: "Via current", icon: "circle-dot", note: "IPC-2221 on the plated barrel. JLC plates about 18–25 µm.",
    inputs: (c) => [["d", "Drill", "mm", "0.3"], ["plate", "Plating", "µm", "20"], ["dt", "Temperature rise", "°C", "10"], ["len", "Board thickness", "mm", String(c.thickness || 1.6)], ["need", "Current to carry", "A", "2"]],
    run: (v) => {
      const d = parseSI(v.d), t = parseSI(v.plate) / 1000, dT = parseSI(v.dt);
      const Amm2 = Math.PI * (d + t) * t, Amil = Amm2 / (MIL * MIL), I = ipcI(Amil, dT, 0.048);
      const R = RHO * (parseSI(v.len) / 1000) / (Amm2 / 1e6);
      const n = Math.ceil(parseSI(v.need) / I);
      return [["Per via", `${I.toFixed(2)} A`, `at +${dT} °C`, true], ["Vias for " + v.need + " A", String(n), "in parallel, spread along the path"], ["Resistance", fmtSI(R, "Ω"), "per via"]];
    } },
  { id: "z", title: "Impedance", icon: "activity", note: "IPC-2141 approximation, within about 5–10 %. Confirm with the fab for controlled impedance.",
    inputs: (c) => [["type", "Line", "", "microstrip", ["microstrip", "stripline"]], ["mode", "Pair", "", "differential", ["single", "differential"]],
      ["target", "Target", "Ω", "90"], ["w", "Width", "mm", "0.2"], ["s", "Gap", "mm", "0.15"],
      ["h", "Dielectric height", "mm", String(c.h ?? 1.51)], ["er", "εr", "", String(c.er ?? 4.5)], ["t", "Copper", "mm", String(c.cu ?? 0.035)]],
    run: (v) => {
      const w = parseSI(v.w), s = parseSI(v.s), hh = parseSI(v.h), er = parseSI(v.er), t = parseSI(v.t), T = parseSI(v.target);
      const single = v.type === "stripline" ? (x) => stripline(x, 2 * hh + t, t, er) : (x) => microstrip(x, hh, t, er);
      const diff = v.type === "stripline" ? (x) => 2 * single(x) * (1 - 0.347 * Math.exp(-2.9 * s / (2 * hh + t))) : (x) => 2 * single(x) * (1 - 0.48 * Math.exp(-0.96 * s / hh));
      const f = v.mode === "differential" ? diff : single;
      const z = f(w), wt = solve(f, T);
      const ratio = w / hh;
      return [[v.mode === "differential" ? "Zdiff" : "Z0", `${z.toFixed(1)} Ω`, `for ${w} mm`, true], [`Width for ${T} Ω`, `${wt.toFixed(3)} mm`, v.mode === "differential" ? `with a ${s} mm gap` : ""],
        v.type === "microstrip" && (ratio < 0.1 || ratio > 2) ? ["Note", "outside the formula's range", "w/h should be 0.1-2"] : null].filter(Boolean);
    } },
  { id: "div", title: "Voltage divider", icon: "git-branch", note: "E24 values, or E96 when E24 is off by more than 1 %. Vout = Vin·R2 / (R1 + R2).",
    inputs: () => [["vin", "Vin (or Vout for a feedback divider)", "V", "5"], ["vout", "Vout (or the reference)", "V", "3.3"], ["rt", "Total resistance about", "Ω", "100k"]],
    run: (v) => {
      const vin = parseSI(v.vin), vout = parseSI(v.vout), rt = parseSI(v.rt);
      const best = (E) => { let b = null; for (const r2 of series(E, rt / 30, rt * 3)) { const r1 = nearest(r2 * (vin / vout - 1), E); const vo = vin * r2 / (r1 + r2);
        const e = Math.abs(vo - vout) / vout + Math.abs(Math.log((r1 + r2) / rt)) * 0.002; if (!b || e < b.e) b = { r1, r2, vo, e }; } return b; };
      let b = best(E24), s = "E24";
      if (Math.abs(b.vo - vout) / vout > 0.01) { b = best(E96); s = "E96"; }
      return [["R1 (top)", fmtSI(b.r1, "Ω"), s, true], ["R2 (bottom)", fmtSI(b.r2, "Ω"), s, true], ["Vout", `${b.vo.toFixed(4)} V`, `${((b.vo - vout) / vout * 100).toFixed(2)} %`],
        ["Current", fmtSI(vin / (b.r1 + b.r2), "A"), "through the divider"]];
    } },
  { id: "led", title: "LED resistor", icon: "sun", note: "Rounded up to the next E24 value.",
    inputs: () => [["vs", "Supply", "V", "3.3"], ["vf", "LED forward voltage", "V", "2.0"], ["if", "LED current", "A", "2m"]],
    run: (v) => {
      const vs = parseSI(v.vs), vf = parseSI(v.vf), i = parseSI(v.if), r = (vs - vf) / i, re = nearest(r, E24, true);
      return [["Resistor", fmtSI(re, "Ω"), `exact ${fmtSI(r, "Ω")}`, true], ["Current", fmtSI((vs - vf) / re, "A"), ""], ["Power", fmtSI((vs - vf) ** 2 / re, "W"), "in the resistor"]];
    } },
  { id: "rc", title: "RC filter", icon: "audio-waveform", note: "fc = 1 / (2πRC). Capacitors use the nearest E12 value.",
    inputs: () => [["r", "R", "Ω", "10k"], ["c", "C", "F", "100n"], ["fc", "Target corner (for C)", "Hz", "1k"]],
    run: (v) => {
      const r = parseSI(v.r), c = parseSI(v.c), fc = 1 / (2 * Math.PI * r * c), ct = 1 / (2 * Math.PI * r * parseSI(v.fc)), ce = nearest(ct, E12);
      return [["Corner", fmtSI(fc, "Hz"), `τ = ${fmtSI(r * c, "s")}`, true], [`C for ${v.fc} Hz`, fmtSI(ce, "F"), `exact ${fmtSI(ct, "F")}, gives ${fmtSI(1 / (2 * Math.PI * r * ce), "Hz")}`]];
    } },
  { id: "xtal", title: "Crystal load capacitors", icon: "cpu", note: "C1 = C2 = 2(CL − Cstray). Stray is typically 2–5 pF.",
    inputs: () => [["cl", "Crystal load (CL)", "F", "12p"], ["cs", "Stray", "F", "3p"]],
    run: (v) => { const c = 2 * (parseSI(v.cl) - parseSI(v.cs)), e = nearest(c, E12); return [["C1 = C2", fmtSI(e, "F"), `exact ${fmtSI(c, "F")}`, true], ["Load then", fmtSI(e / 2 + parseSI(v.cs), "F"), ""]]; } },
  { id: "ldo", title: "Regulator heat", icon: "zap", note: "P = (Vin − Vout)·I. θJA from the data sheet: SOT-223 ≈ 60 °C/W, SOT-23-5 ≈ 200 °C/W.",
    inputs: () => [["vin", "Vin", "V", "5"], ["vout", "Vout", "V", "3.3"], ["i", "Load current", "A", "300m"], ["tja", "θJA", "°C/W", "60"], ["ta", "Ambient", "°C", "40"]],
    run: (v) => {
      const p = (parseSI(v.vin) - parseSI(v.vout)) * parseSI(v.i), tj = parseSI(v.ta) + p * parseSI(v.tja);
      return [["Dissipation", fmtSI(p, "W"), `${(parseSI(v.vout) / parseSI(v.vin) * 100).toFixed(0)} % efficient`, true], ["Junction", `${tj.toFixed(0)} °C`, tj > 125 ? "too hot: use a buck or a bigger package" : tj > 100 ? "warm: more copper under it" : "fine"]];
    } },
  { id: "batt", title: "Battery life", icon: "clock", note: "Average current from the duty cycle, with 80 % of rated capacity usable.",
    inputs: () => [["cap", "Capacity", "mAh", "2000"], ["on", "Active current", "A", "80m"], ["ton", "Active time per cycle", "s", "2"], ["off", "Sleep current", "A", "15u"], ["period", "Cycle", "s", "600"]],
    run: (v) => {
      const on = parseSI(v.on), ton = parseSI(v.ton), off = parseSI(v.off), per = parseSI(v.period);
      const avg = (on * ton + off * Math.max(0, per - ton)) / per, hours = parseSI(v.cap) / 1000 * 0.8 / avg;
      return [["Runs for", hours > 48 ? `${(hours / 24).toFixed(hours > 480 ? 0 : 1)} days` : `${hours.toFixed(1)} h`, "", true], ["Average current", fmtSI(avg, "A"), `${(on * ton / per / avg * 100).toFixed(0)} % of it while active`]];
    } },
];

export class Calculators {
  constructor() { this.at = localStorage.getItem("tw.calc") || "trace"; this.vals = JSON.parse(localStorage.getItem("tw.calc.v") || "{}"); this.ctx = {}; }

  // ws: the project workspace, for its stack-up (none on the projects page)
  async open(ws) {
    if (this.el) { this.close(); return; }
    this.ws = ws; this.ctx = {};
    this.list = h("div.cl-list");
    this.body = h("div.cl-body");
    this.el = h("div.cl-panel", h("div.cl-head", icon("calculator", 16), h("b", "Calculators"), this.ctxEl = h("span.cl-ctx"), h("div.grow"),
      btn("x", null, { onclick: () => this.close(), "data-tip": "Close" }, "sm ghost")), this.list, this.body);
    document.body.appendChild(this.el);
    requestAnimationFrame(() => this.el.classList.add("on"));
    this.onKey = (e) => { if (e.key === "Escape") this.close(); };
    addEventListener("keydown", this.onKey);
    const st = this.ws ? await api(`/api/projects/${encodeURIComponent(this.ws.pid)}/stackup`).catch(() => null) : null;
    if (st && st.board) {
      const outer = st.copper_mm[st.layers[0]];
      this.ctx = { oz: outer ? Math.round(outer / OZ_MM * 2) / 2 : 1, thickness: st.thickness, cu: outer || 0.035,
        h: st.top_dielectric ? +st.top_dielectric.h.toFixed(4) : undefined, er: st.top_dielectric ? st.top_dielectric.er || 4.5 : undefined };
      this.ctxEl.textContent = `This board: ${st.layers.length} layers, ${st.thickness} mm` + (st.top_dielectric ? `, top dielectric ${st.top_dielectric.h.toFixed(3)} mm` : "");
    }
    this.render();
  }

  close() { if (!this.el) return; removeEventListener("keydown", this.onKey); const el = this.el; this.el = null; el.classList.remove("on"); setTimeout(() => el.remove(), 200); }

  render() {
    clear(this.list);
    for (const c of CALCS) this.list.appendChild(h("button.cl-chip" + (c.id === this.at ? ".on" : ""), { onclick: () => { this.at = c.id; localStorage.setItem("tw.calc", c.id); this.render(); } }, icon(c.icon, 13), c.title));
    const c = CALCS.find((x) => x.id === this.at) || CALCS[0];
    const vals = { ...Object.fromEntries(c.inputs(this.ctx).map(([k, , , d]) => [k, d])), ...(this.vals[c.id] || {}) };
    const out = h("div.cl-out");
    const calc = () => {
      clear(out);
      let res;
      try { res = c.run(vals); } catch (e) { res = [["", "Check the inputs", ""]]; }
      for (const [label, val, sub, big] of res) out.appendChild(h("div.cl-res" + (big ? ".big" : ""), h("span", label), h("b", val), sub ? h("small", sub) : null));
    };
    clear(this.body).append(h("div.cl-title", c.title), h("div.cl-form", c.inputs(this.ctx).map(([k, label, unit, , opts]) => h("label.cl-in", h("span", label),
      opts ? h("div.seg", opts.map((o) => h("button" + (vals[k] === o ? ".on" : ""), { type: "button", onclick: (e) => { vals[k] = o; this.save(c.id, vals); for (const b of e.currentTarget.parentNode.children) b.classList.toggle("on", b === e.currentTarget); calc(); } }, o)))
        : h("div.cl-field", h("input", { value: vals[k], spellcheck: false, oninput: (e) => { vals[k] = e.target.value; this.save(c.id, vals); calc(); } }), unit ? h("em", unit) : null)))),
      out, h("div.cl-note", icon("info", 12), c.note),
      h("button.linkbtn.cl-reset", { onclick: () => { delete this.vals[c.id]; localStorage.setItem("tw.calc.v", JSON.stringify(this.vals)); this.render(); } }, "Reset"));
    calc();
  }

  save(id, vals) { this.vals[id] = { ...vals }; localStorage.setItem("tw.calc.v", JSON.stringify(this.vals)); }
}
