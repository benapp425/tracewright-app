// The simulations Claude ran with KiCad's ngspice (docs/sim; tw/sim.py), under Docs > Simulations: each one's plot,
// what it showed against its pass criterion, each probe's values, and the netlist it ran. Run again after editing the
// netlist; the sign-off evidence that cites it follows the new result.
import { h, clear, api, toast, fmtTime, lightbox, btn, confirmDialog } from "./util.js";
import { icon } from "./icons.js";
import { native, isNative } from "./native.js";

const enc = encodeURIComponent;
const fileUrl = (pid, path) => `/api/projects/${enc(pid)}/file?path=${enc(path)}&raw=1`;
const KIND = { time: "Transient", frequency: "AC sweep" };

export async function simRuns(pid) {
  try { return (await api(`/api/projects/${enc(pid)}/sims`)).items || []; } catch { return []; }
}

export function simState(s) {
  if (s.status === "error") return ["fail", "circle-x", "Did not run"];
  if (!s.check) return ["none", "activity", "Ran"];
  return s.status === "fail" ? ["fail", "circle-x", "Fails"] : ["ok", "circle-check", "Passes"];
}

// a value with an SI prefix and its unit: 0.00153 V -> "1.53 mV" (rounded first: 0.99998 is "1", not "1000 m")
function si(v, unit = "") {
  if (v == null || !isFinite(v)) return "–";
  if (v === 0) return `0${unit ? " " + unit : ""}`;
  const P = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"]];
  const r = +v.toPrecision(4), a = Math.abs(r);
  const [m, p] = P.find(([m]) => a >= m) || P[P.length - 1];
  return `${+(r / m).toPrecision(4)} ${p}${unit}`.trim().replace("-", "−");
}
const unitOf = (probe) => /^v\(/i.test(probe) ? "V" : /^i\(/i.test(probe) ? "A" : "";

export class SimView {
  constructor(ws, el, info, onChange) {
    this.ws = ws; this.pid = ws.pid; this.el = el; this.s = info; this.onChange = onChange;
    this.render();
  }

  render() {
    const s = this.s, el = clear(this.el), files = s.files || {};
    const [cls, ic, word] = simState(s);
    const kind = KIND[s.scale] || (s.scale ? "DC sweep" : "Operating point");
    const path = (ext) => `docs/sim/${files[ext]}`;
    el.appendChild(h("div.sim-head",
      h("div.grow", h("div.sim-title", icon("activity", 16), h("b", s.title || s.name)),
        h("div.sim-sub", h("span.sim-tag." + cls, icon(ic, 12), word), h("span", kind), s.at ? h("span", "ran " + fmtTime(s.at)) : null,
          s.requirement ? h("span", { "data-tip": "Recorded as this requirement's evidence on the Sign-off page" }, icon("badge-check", 12),
            s.requirement_text ? `Evidence for “${s.requirement_text}”` : `Evidence for ${s.requirement}`) : null)),
      btn("play", "Run again", { onclick: (e) => this.rerun(e.currentTarget), "data-tip": "Run the netlist as it is now in " + path("cir") }, "sm"),
      files.csv ? btn("download", null, { onclick: () => native.save(fileUrl(this.pid, path("csv")) + "&download=1"), "data-tip": "Waveforms (CSV)" }, "sm ghost") : null,
      isNative && files.cir ? btn("external-link", null, { onclick: () => native.openPath(this.ws.p.root + "/" + path("cir")), "data-tip": "Open the netlist" }, "sm ghost") : null,
      btn("trash-2", null, { onclick: () => this.remove(), "data-tip": "Remove this simulation" }, "sm ghost")));
    if (s.check) el.appendChild(h("div.sim-verdict." + cls, icon(ic, 14), h("span", s.check)));
    if (s.error) {
      el.appendChild(h("div.sim-verdict.fail", icon("circle-x", 14), h("span", s.error)));
      if ((s.log || []).length) el.appendChild(h("pre.code.sim-log", s.log.join("\n")));
    }
    if (files.svg) el.appendChild(h("img.sim-plot", { src: fileUrl(this.pid, path("svg")) + "&t=" + enc(s.at || ""), alt: s.title || s.name,
      onclick: (e) => lightbox(e.target.src) }));
    const probes = (s.probes || []).filter((p) => (s.summary || {})[p]);
    if (probes.length) {
      const ac = s.scale === "frequency";
      el.appendChild(h("table.sim-tab", h("thead", h("tr", h("th", "Probe"), h("th", ac ? "At the end" : "Final"), h("th", "Lowest"), h("th", "Highest"), ac ? h("th", "−3 dB") : null)),
        h("tbody", probes.map((p) => {
          const m = s.summary[p], c = ac ? (s.corners || {})[p] : null, u = unitOf(p);
          return h("tr", h("td.mono", p), h("td.tnum", si(m.final, u)), h("td.tnum", si(m.min, u)), h("td.tnum", si(m.max, u)), ac ? h("td.tnum", c ? si(c, "Hz") : "–") : null);
        }))));
    }
    if (files.cir) {
      const pre = h("pre.code.sim-net", "…");
      el.appendChild(h("div.sim-sec", h("div.label", "Netlist"), pre));
      api(`/api/projects/${enc(this.pid)}/file?path=${enc(path("cir"))}`).then((f) => { pre.textContent = typeof f === "string" ? f : f.text; })
        .catch((e) => { pre.textContent = e.message; });
    }
  }

  async rerun(b) {
    b.disabled = true;
    try {
      const info = await api(`/api/projects/${enc(this.pid)}/sims/${enc(this.s.name)}`, { body: { action: "run" } });
      this.s = { ...info, files: this.s.files };
      const runs = await simRuns(this.pid);
      this.s = runs.find((x) => x.name === this.s.name) || this.s;
      this.render();
      this.onChange && this.onChange();
      toast(info.error ? "The simulation did not run" : info.check || "Simulation ran", info.error || info.status === "fail" ? "error" : "ok");
    } catch (e) { toast(e.message, "error"); b.disabled = false; }
  }

  async remove() {
    const ok = await confirmDialog({ title: "Remove this simulation?", text: `${this.s.title || this.s.name}: its netlist, plot and waveforms in docs/sim, and the evidence that cites it.`,
      ok: "Remove", danger: true });
    if (!ok) return;
    try {
      await api(`/api/projects/${enc(this.pid)}/sims/${enc(this.s.name)}`, { body: { action: "delete" } });
      clear(this.el);
      this.onChange && this.onChange(true);
    } catch (e) { toast(e.message, "error"); }
  }
}
