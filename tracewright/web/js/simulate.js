// The Simulate tab: the board as laid out, simulated. The voltage drop and current density in a supply's copper, where
// a signal's return current runs (and where it has to go round a gap), the board's heat, a net's impedance and edge at
// the far end, a rail's impedance against its target, and circuit blocks from the schematic in ngspice.
import { h, clear, api, toast } from "./util.js";
import { icon } from "./icons.js";
import { SimView, simRuns } from "./sims.js";

const enc = encodeURIComponent;
const TABS = [["drop", "Voltage drop", "zap"], ["return", "Return paths", "route"], ["heat", "Heat", "sun"],
  ["signal", "Signals", "activity"], ["pdn", "Power delivery", "gauge"], ["circuit", "Circuits", "cpu"]];
const RAMP = { warm: [[0, [40, 70, 160]], [0.35, [40, 170, 170]], [0.65, [240, 200, 70]], [1, [235, 70, 55]]],
  drop: [[0, [45, 150, 95]], [0.5, [235, 200, 70]], [1, [235, 70, 55]]] };
const num = (v) => v === "" || v == null ? null : +v;

function ramp(stops, t) {
  t = Math.max(0, Math.min(1, t));
  for (let k = 1; k < stops.length; k++) {
    const [a, ca] = stops[k - 1], [b, cb] = stops[k];
    if (t <= b) { const f = (t - a) / (b - a || 1); return ca.map((v, i) => Math.round(v + (cb[i] - v) * f)); }
  }
  return stops[stops.length - 1][1];
}

// the board drawn plainly, a field over it, and what the cursor is on
class BoardMap {
  constructor(el, board) {
    this.board = board;
    this.canvas = h("canvas.sm-canvas");
    this.tip = h("div.sm-tip", { style: { display: "none" } });
    this.legend = h("div.sm-legend");
    this.el = h("div.sm-map", this.canvas, this.tip, this.legend);
    el.appendChild(this.el);
    this.canvas.addEventListener("mousemove", (e) => this.hover(e));
    this.canvas.addEventListener("mouseleave", () => { this.tip.style.display = "none"; });
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(this.el);
  }

  set({ field = null, frame = null, ramp: r = "warm", unit = "", lo = null, hi = null, label = "", overlay = null, value = null } = {}) {
    Object.assign(this, { field, frame, rampName: r, unit, lo, hi, label, overlay, valueAt: value });
    this.img = null;
    if (field && frame) {
      let a = Infinity, b = -Infinity;
      for (const row of field) for (const v of row) if (v != null) { if (v < a) a = v; if (v > b) b = v; }
      this.lo = lo ?? a; this.hi = hi ?? b;
      const off = document.createElement("canvas");
      off.width = frame.nx; off.height = frame.ny;
      const c = off.getContext("2d"), im = c.createImageData(frame.nx, frame.ny);
      const span = (this.hi - this.lo) || 1;
      for (let j = 0; j < frame.ny; j++) for (let i = 0; i < frame.nx; i++) {
        const v = field[j][i], k = (j * frame.nx + i) * 4;
        if (v == null) continue;
        const [R, G, B] = ramp(RAMP[r], (v - this.lo) / span);
        im.data[k] = R; im.data[k + 1] = G; im.data[k + 2] = B; im.data[k + 3] = 215;
      }
      c.putImageData(im, 0, 0);
      this.img = off;
    }
    clear(this.legend);
    if (this.img) {
      const g = RAMP[r].map(([t, c]) => `rgb(${c.join(",")}) ${t * 100}%`).join(", ");
      this.legend.append(h("span", label), h("span.sm-lo", fmt(this.lo, unit)), h("i.sm-bar", { style: { background: `linear-gradient(90deg, ${g})` } }), h("span.sm-hi", fmt(this.hi, unit)));
    }
    this.draw();
  }

  fit() {
    const r = this.el.getBoundingClientRect(), b = this.board.bbox || [0, 0, 100, 100];
    const w = r.width || 600, hgt = r.height || 360, pad = 16;
    const s = Math.min((w - 2 * pad) / (b[2] - b[0] || 1), (hgt - 2 * pad) / (b[3] - b[1] || 1));
    this.S = s; this.ox = (w - (b[2] - b[0]) * s) / 2 - b[0] * s; this.oy = (hgt - (b[3] - b[1]) * s) / 2 - b[1] * s;
    const dpr = devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = hgt * dpr;
    this.canvas.style.width = w + "px"; this.canvas.style.height = hgt + "px";
    return dpr;
  }

  draw() {
    if (!this.board) return;
    const dpr = this.fit(), c = this.canvas.getContext("2d"), S = this.S, d = this.board;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = "#101216"; c.fillRect(0, 0, this.canvas.width, this.canvas.height);
    c.setTransform(S * dpr, 0, 0, S * dpr, this.ox * dpr, this.oy * dpr);
    const px = 1 / S;
    c.fillStyle = "#1a2a20";
    for (const ol of d.outline || []) { c.beginPath(); ol.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.closePath(); c.fill("evenodd"); }
    c.lineCap = "round";
    for (const t of d.tracks || []) { c.strokeStyle = "rgba(200,140,90,.28)"; c.lineWidth = t[4]; c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); }
    c.fillStyle = "rgba(210,180,110,.38)";
    for (const f of d.footprints || []) for (const p of f.pads || []) for (const pl of p.p || []) { c.beginPath(); pl.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.closePath(); c.fill(); }
    if (this.img) {
      const F = this.frame;
      c.imageSmoothingEnabled = true;
      c.drawImage(this.img, F.x0, F.y0, F.nx * F.cell, F.ny * F.cell);
    }
    if (this.overlay) this.overlay(c, px);
    c.strokeStyle = "rgba(230,200,90,.9)"; c.lineWidth = 1.2 * px;
    for (const ol of d.outline || []) { c.beginPath(); ol.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.closePath(); c.stroke(); }
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.font = "600 10px -apple-system, sans-serif"; c.fillStyle = "rgba(230,232,236,.75)"; c.textAlign = "center";
    if (S > 4) for (const f of d.footprints || []) { const [x, y] = [f.x * S + this.ox, f.y * S + this.oy]; c.fillText(f.ref, x, y + 3); }
  }

  hover(e) {
    if (!this.img && !this.valueAt) return;
    const r = this.canvas.getBoundingClientRect(), x = (e.clientX - r.left - this.ox) / this.S, y = (e.clientY - r.top - this.oy) / this.S;
    let text = this.valueAt ? this.valueAt(x, y) : null;
    if (text == null && this.img) {
      const F = this.frame, i = Math.floor((x - F.x0) / F.cell), j = Math.floor((y - F.y0) / F.cell);
      const v = j >= 0 && j < F.ny && i >= 0 && i < F.nx ? this.field[j][i] : null;
      if (v != null) text = fmt(v, this.unit);
    }
    if (text == null) { this.tip.style.display = "none"; return; }
    this.tip.textContent = text;
    this.tip.style.display = "block";
    this.tip.style.left = (e.clientX - r.left + 14) + "px"; this.tip.style.top = (e.clientY - r.top + 12) + "px";
  }
}

function fmt(v, unit) {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v), d = a >= 100 ? 0 : a >= 10 ? 1 : 2;
  return `${v.toFixed(d)} ${unit}`.trim();
}

export class SimulateView {
  constructor(el, ws) {
    this.el = el; this.ws = ws; this.pid = ws.pid;
    this.tab = localStorage.getItem("tw.sim.tab") || "drop";
    this.box = h("div.panel.simview");
    el.appendChild(this.box);
    this.res = {}; this.form = {};
    ws.ev.on("board.changed", () => { this.stale = true; this.board = null; });
    ws.ev.on("schematic.changed", () => { this.stale = true; });
    ws.ev.on("sims.changed", () => { if (this.tab === "circuit") this.render(); });
    this.load();
  }

  shown() { if (this.stale) { this.stale = false; this.load(); } }
  destroy() {}

  async load() {
    clear(this.box).appendChild(h("div.pl-empty", "Reading the board…"));
    try {
      const [info, board] = await Promise.all([api(`/api/projects/${enc(this.pid)}/simulate`), api(`/api/projects/${enc(this.pid)}/board`).catch(() => null)]);
      this.info = info; this.board = board && !board.empty ? board : null;
    } catch (e) { this.info = { error: e.message }; }
    this.render();
  }

  render() {
    const box = clear(this.box), I = this.info || {};
    box.appendChild(h("div.sm-tabs", TABS.map(([id, label, ic]) => h("button" + (id === this.tab ? ".on" : ""), { "data-sim": id,
      onclick: () => { this.tab = id; localStorage.setItem("tw.sim.tab", id); this.render(); } }, icon(ic, 13), label))));
    if (I.error) { box.appendChild(h("div.empty", h("h3", "The board could not be read"), h("p", I.error))); return; }
    const body = h("div.sm-body");
    box.appendChild(body);
    if (this.tab !== "circuit" && !I.board) { body.appendChild(h("div.empty", h("div.eicon", icon("circuit-board", 20)), h("h3", "No board yet"), h("p", "These simulations work on the laid-out board."))); return; }
    if (!I.schematic && this.tab !== "heat") { body.appendChild(h("div.empty", h("h3", "No schematic yet"))); return; }
    this[this.tab + "Tab"](body, I);
  }

  // ---------------------------------------------------------------- shared
  controls(body, items, run, note) {
    const row = h("div.sm-ctl", items, h("button.btn.primary.sm.sm-run", { onclick: (e) => run(e.currentTarget) }, icon("play", 12), "Run"));
    body.appendChild(row);
    if (note) body.appendChild(h("div.sm-note", note));
    return row;
  }

  field(label, input) { return h("label.sm-f", h("span", label), input); }

  select(key, options, value) {
    const s = h("select", options.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));
    s.addEventListener("change", () => { this.form[key] = s.value; });
    this.form[key] = value;
    return s;
  }

  input(key, value, ph = "", w = 70) {
    const i = h("input", { value: this.form[key] ?? value ?? "", placeholder: ph, style: { width: w + "px" } });
    i.addEventListener("input", () => { this.form[key] = i.value; });
    this.form[key] = i.value;
    return i;
  }

  async run(kind, body, b) {
    if (b) { b.disabled = true; b.classList.add("busy"); }
    try { this.res[kind] = await api(`/api/projects/${enc(this.pid)}/simulate/${kind}`, { body }); }
    catch (e) { toast(e.message, "error", 6000); }
    if (b) { b.disabled = false; b.classList.remove("busy"); }
    this.render();
  }

  lines(body, r) { body.appendChild(h("div.sm-lines", (r.lines || []).map((l) => h("div", l[0].toUpperCase() + l.slice(1))))); }

  map(body) {
    if (!this.board) return null;
    return new BoardMap(body, this.board);
  }

  // ---------------------------------------------------------------- voltage drop
  dropTab(body, I) {
    const rails = I.rails || [];
    if (!rails.length) { body.appendChild(h("div.pl-empty", "No supply rails found.")); return; }
    const pick = this.select("drop_net", rails.map((r) => [r.net, `${r.net}${r.volts ? ` (${r.volts} V)` : ""}`]), this.form.drop_net || rails[0].net);
    const rail = () => rails.find((r) => r.net === (this.form.drop_net || rails[0].net)) || {};
    const amps = this.input("drop_amps", rail().amps ?? "", "0.5");
    this.controls(body, [this.field("Rail", pick), this.field("Current (A)", amps)],
      (b) => this.run("drop", { net: this.form.drop_net, amps: num(this.form.drop_amps) }, b),
      "From its source to its loads through the copper as it is: tracks, pours and vias. Each load draws an even share unless Claude sets them.");
    const r = this.res.drop;
    if (!r) return;
    this.lines(body, r);
    const layers = Object.keys(r.layers || {});
    this.dropLayer = layers.includes(this.dropLayer) ? this.dropLayer : layers[0];
    this.dropShow = this.dropShow || "drop";
    const m = this.map(body);
    const bar = h("div.sm-chips", layers.map((l) => h("button" + (l === this.dropLayer ? ".on" : ""), { onclick: () => { this.dropLayer = l; this.render(); } }, l)),
      h("span.grow"), [["drop", "Drop"], ["density", "Current density"]].map(([k, t]) => h("button" + (k === this.dropShow ? ".on" : ""), { onclick: () => { this.dropShow = k; this.render(); } }, t)));
    body.insertBefore(bar, m ? m.el : null);
    if (m) {
      const dens = this.dropShow === "density";
      m.set({ field: (dens ? r.density : r.layers)[this.dropLayer], frame: r.frame, ramp: "drop", unit: dens ? "A/mm" : "mV", lo: 0,
        label: dens ? `current density on ${this.dropLayer}` : `drop from ${r.source} on ${this.dropLayer}`,
        overlay: (c, px) => { for (const hs of r.hot || []) if (hs.layer === this.dropLayer) { c.strokeStyle = "#ff6b5a"; c.lineWidth = 2 * px; c.beginPath(); c.arc(hs.x, hs.y, 1.2, 0, 7); c.stroke(); } } });
    }
    if ((r.loads || []).length) body.appendChild(h("table.sm-table", h("tr", h("th", "Load"), h("th", "Current"), h("th", "Drop"), h("th", "Of the rail")),
      r.loads.map((x) => h("tr", h("td", x.ref), h("td", `${x.amps} A`), h("td", `${x.drop_mv} mV`), h("td", x.pct == null ? "–" : `${x.pct} %`)))));
  }

  // ---------------------------------------------------------------- return paths
  returnTab(body, I) {
    const sig = I.signals || [];
    if (!sig.length) { body.appendChild(h("div.pl-empty", "No routed signals yet.")); return; }
    const pick = this.select("ret_net", sig.map((s) => [s.net, s.kind ? `${s.net} (${s.kind})` : s.net]), this.form.ret_net || sig[0].net);
    this.controls(body, [this.field("Net", pick)], (b) => this.run("return", { net: this.form.ret_net }, b),
      "A fast edge's return current runs in the plane right under its track. Where the plane has a gap, it goes round: the loop it opens radiates and picks up noise.");
    const r = this.res.return;
    if (!r) return;
    this.lines(body, r);
    const m = this.map(body);
    if (!m) return;
    const full = (this.board.nets || []).find((n) => n === r.net || n.split("/").pop() === r.net);
    const COLS = {}, pal = [[90, 170, 255], [120, 210, 140], [230, 160, 90], [200, 130, 230]];
    m.set({ overlay: (c, px) => {
      for (const run of r.runs || []) {
        const col = COLS[run.ref_net] || (COLS[run.ref_net] = pal[Object.keys(COLS).length % pal.length]);
        c.strokeStyle = `rgba(${col.join(",")},.22)`; c.lineWidth = Math.max(6 * run.h, 0.6); c.lineCap = "butt";
        c.beginPath(); c.moveTo(...run.a); c.lineTo(...run.b); c.stroke();
      }
      c.lineCap = "round";
      for (const t of this.board.tracks || []) if (t[6] === full) { c.strokeStyle = "rgba(255,225,140,.95)"; c.lineWidth = Math.max(t[4], 1.5 * px); c.beginPath(); c.moveTo(t[0], t[1]); c.lineTo(t[2], t[3]); c.stroke(); }
      for (const g of r.gaps || []) {
        c.strokeStyle = "#ff5a4a"; c.lineWidth = 3 * px; c.beginPath(); c.moveTo(...g.at); c.lineTo(...g.to); c.stroke();
        if (g.detour) { c.setLineDash([4 * px, 3 * px]); c.lineWidth = 1.6 * px; c.beginPath(); g.detour.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y)); c.stroke(); c.setLineDash([]); }
      }
      for (const v of r.vias || []) { c.strokeStyle = v.return_via == null || v.return_via > 2 ? "#ff9a4a" : "#7fd4a0"; c.lineWidth = 1.5 * px; c.beginPath(); c.arc(v.x, v.y, 0.6, 0, 7); c.stroke(); }
    }, value: (x, y) => {
      const g = (r.gaps || []).find((q) => Math.hypot(q.at[0] - x, q.at[1] - y) < 2 || Math.hypot(q.to[0] - x, q.to[1] - y) < 2);
      if (g) return g.text;
      const v = (r.vias || []).find((q) => Math.hypot(q.x - x, q.y - y) < 1.2);
      return v ? v.text : null;
    } });
    m.legend.append(...Object.entries(COLS).map(([n, col]) => h("span.sm-key", h("i", { style: { background: `rgb(${col.join(",")})` } }), `return in ${n}`)),
      h("span.sm-key", h("i", { style: { background: "#ff5a4a" } }), "gap"));
  }

  // ---------------------------------------------------------------- heat
  heatTab(body, I) {
    this.form.heat_src = this.form.heat_src || (I.heat || []).map((s) => ({ ref: s.ref, watts: s.watts, why: s.why }));
    const rows = h("div.sm-src");
    const draw = () => {
      clear(rows);
      this.form.heat_src.forEach((s, k) => rows.appendChild(h("div.sm-srow",
        h("input", { value: s.ref, style: { width: "70px" }, placeholder: "U3", oninput: (e) => { s.ref = e.target.value.trim(); } }),
        h("input", { value: s.watts, style: { width: "60px" }, oninput: (e) => { s.watts = +e.target.value || 0; } }), h("span", "W"),
        h("span.grow.sm-why", s.why || ""), h("button.tbtn", { onclick: () => { this.form.heat_src.splice(k, 1); draw(); } }, icon("x", 11)))));
      rows.appendChild(h("button.btn.sm", { onclick: () => { this.form.heat_src.push({ ref: "", watts: 0.1, why: "set by you" }); draw(); } }, icon("plus", 12), "A part that gets warm"));
    };
    draw();
    body.appendChild(rows);
    const air = this.select("heat_air", [["still", "Still air"], ["fan", "A small fan"]], this.form.heat_air || "still");
    this.controls(body, [this.field("Air", air), this.field("Air temperature (°C)", this.input("heat_amb", "", "25"))],
      (b) => this.run("heat", { sources: this.form.heat_src.filter((s) => s.ref && s.watts > 0), air: this.form.heat_air, ambient: num(this.form.heat_amb) }, b),
      "Heat spreads through the copper and the board and leaves both faces into the air. Regulators are filled in from their drop and current.");
    const r = this.res.heat;
    if (!r) return;
    this.lines(body, r);
    const m = this.map(body);
    if (m && r.grid) m.set({ field: r.grid, frame: r.frame, ramp: "warm", unit: "°C", label: "board temperature" });
    if ((r.parts || []).length) body.appendChild(h("table.sm-table", h("tr", h("th", "Part"), h("th", "Hottest under it")),
      r.parts.slice(0, 8).map((x) => h("tr", h("td", x.ref), h("td", `${x.max_c} °C`)))));
  }

  // ---------------------------------------------------------------- signals
  signalTab(body, I) {
    const sig = I.signals || [];
    if (!sig.length) { body.appendChild(h("div.pl-empty", "No routed signals yet.")); return; }
    const pick = this.select("sig_net", sig.map((s) => [s.net, s.kind ? `${s.net} (${s.kind})` : s.net]), this.form.sig_net || sig[0].net);
    this.controls(body, [this.field("Net", pick), this.field("Edge (ns)", this.input("sig_rise", "", "auto", 60)),
      this.field("Driver (Ω)", this.input("sig_rs", "25", "", 50)), this.field("Series R (Ω)", this.input("sig_ser", "", "none", 60))],
      (b) => this.run("signal", { net: this.form.sig_net, rise_ns: num(this.form.sig_rise), rs: num(this.form.sig_rs) || 25, series: num(this.form.sig_ser) }, b),
      "The route from the driver to the far end, its impedance piece by piece from the stack-up, and the edge as it arrives (lines simulated in ngspice).");
    const r = this.res.signal;
    if (!r) return;
    this.lines(body, r);
    if (r.svg) body.appendChild(h("div.sm-svg", { html: r.svg }));
    const pieces = [];                               // the route as it reads: alike pieces in a row are one
    for (const p of ((r.route || {}).pieces || []).filter((q) => q.kind === "track")) {
      const last = pieces[pieces.length - 1];
      if (last && last.layer === p.layer && last.w === p.w && last.z0 === p.z0 && last.how === p.how) last.length = +(last.length + p.length).toFixed(2);
      else pieces.push({ ...p, length: +p.length.toFixed(2) });
    }
    if (pieces.length) body.appendChild(h("table.sm-table", h("tr", h("th", "Layer"), h("th", "Length"), h("th", "Width"), h("th", "Impedance"), h("th", "Over")),
      pieces.slice(0, 14).map((p) => h("tr", h("td", p.layer), h("td", `${p.length} mm`), h("td", `${p.w} mm`), h("td", p.z0 ? `${p.z0} Ω` : "–"), h("td.sm-how", p.how)))));
    const xt = (r.crosstalk || {}).neighbours || [];
    if (xt.length) body.appendChild(h("table.sm-table", h("tr", h("th", "Beside it"), h("th", "Coupled"), h("th", "Spacing"), h("th", "Noise (about)")),
      xt.slice(0, 8).map((x) => h("tr", h("td", x.net), h("td", `${x.length} mm`), h("td", `${x.spacing} mm`), h("td", `${x.noise_pct} %`)))));
  }

  // ---------------------------------------------------------------- power delivery
  pdnTab(body, I) {
    const rails = I.rails || [];
    if (!rails.length) { body.appendChild(h("div.pl-empty", "No supply rails found.")); return; }
    const pick = this.select("pdn_net", rails.map((r) => [r.net, r.net]), this.form.pdn_net || rails[0].net);
    this.controls(body, [this.field("Rail", pick), this.field("Ripple (%)", this.input("pdn_rip", "5", "", 50)), this.field("Load step (A)", this.input("pdn_step", "", "auto", 60))],
      (b) => this.run("pdn", { net: this.form.pdn_net, ripple: (num(this.form.pdn_rip) || 5) / 100, step: num(this.form.pdn_step) }, b),
      "The rail's impedance from its capacitors (typical ESR and ESL, plus their vias) and any plane pair, against the target its loads set.");
    const r = this.res.pdn;
    if (!r) return;
    this.lines(body, r);
    if (r.svg) body.appendChild(h("div.sm-svg", { html: r.svg }));
    if ((r.caps || []).length) body.appendChild(h("table.sm-table", h("tr", h("th", "Capacitor"), h("th", "Value"), h("th", "ESL with vias"), h("th", "Resonance")),
      r.caps.map((c) => h("tr", h("td", c.ref), h("td", c.value), h("td", `${c.esl_nh} nH`), h("td", hz(c.f_res))))));
  }

  // ---------------------------------------------------------------- circuits from the schematic
  async circuitTab(body) {
    const sel = this.ws.views && this.ws.views.schematic && this.ws.views.schematic.sel ? [...this.ws.views.schematic.sel] : [];
    const refs = this.input("cir_refs", sel.join(", "), "R8, C3", 220);
    const src = this.input("cir_src", "+3V3", "net", 80), sv = this.input("cir_v", "3.3", "", 50);
    const kind = this.select("cir_kind", [["step", "Step"], ["dc", "DC"], ["pulse", "Square"], ["sine", "Sine"]], this.form.cir_kind || "step");
    const probe = this.input("cir_probe", "", "net to watch", 120), xc = this.input("cir_c", "", "e.g. I2C_SDA 100p", 150);
    const an = this.select("cir_an", [["tran", "Over time"], ["ac", "Frequency sweep"]], this.form.cir_an || "tran");
    const stop = this.input("cir_stop", "1m", "", 60);
    this.controls(body, [this.field("Parts", refs), this.field("Source on", src), this.field("V", sv), this.field("", kind),
      this.field("Extra C", xc), this.field("Watch", probe), this.field("Analysis", an), this.field("Time / to (Hz)", stop)], (b) => {
      const refsL = String(this.form.cir_refs || "").split(/[\s,]+/).filter(Boolean);
      const [cn, cv] = String(this.form.cir_c || "").trim().split(/\s+/);
      const t = parseSI(this.form.cir_stop || "1m");
      this.run("circuit", { name: "block-" + refsL.slice(0, 3).join("-").toLowerCase(), refs: refsL,
        sources: [{ net: this.form.cir_src, kind: this.form.cir_kind, v: +this.form.cir_v || 3.3, freq: 1e3 }],
        extra: cn && cv ? [{ kind: "C", net: cn, value: cv }] : [], probes: this.form.cir_probe ? [this.form.cir_probe] : [],
        analysis: this.form.cir_an === "ac" ? { kind: "ac", from: 10, to: t || 1e7 } : { kind: "tran", stop: t || 1e-3 } }, b);
    }, "Pick parts in the schematic (or type them): resistors, capacitors, inductors and diodes run as drawn; a chip needs its own SPICE model. Give the block its supply or signal and what to watch.");
    const r = this.res.circuit;
    if (r && (r.notes || []).length) body.appendChild(h("div.sm-note.warn", r.notes.join("; ")));
    const list = h("div.sm-runs", h("div.pl-empty", "Reading the simulations…"));
    body.appendChild(list);
    const runs = await simRuns(this.pid);
    clear(list);
    if (!runs.length) list.appendChild(h("div.pl-empty", "No circuit simulations yet."));
    for (const s of runs) { const el = h("div.sim-card"); list.appendChild(el); new SimView(this.ws, el, s, () => this.render()); }
  }
}

function hz(f) { return f >= 1e9 ? `${(f / 1e9).toFixed(2)} GHz` : f >= 1e6 ? `${(f / 1e6).toFixed(1)} MHz` : f >= 1e3 ? `${(f / 1e3).toFixed(1)} kHz` : `${f} Hz`; }
function parseSI(s) {
  const m = String(s).trim().match(/^([\d.]+)\s*([pnuµmkMG]?)/);
  return m ? +m[1] * ({ p: 1e-12, n: 1e-9, u: 1e-6, "µ": 1e-6, m: 1e-3, k: 1e3, M: 1e6, G: 1e9 }[m[2]] || 1) : null;
}
