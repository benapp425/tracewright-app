// The requirements' Advanced limits: preset limits (largest board, tallest part, layers, temperature ...),
// each left to Claude unless you give it a value. Used in the new-project dialog, the guided start's
// requirements card and the project's Design limits dialog.
import { h, clear, api, toast } from "./util.js";

let OPTIONS = null;
export async function limitOptions() {
  if (!OPTIONS) OPTIONS = (await api("/api/constraints")).options;
  return OPTIONS;
}

const num = (v) => (v === "" || v == null ? null : Number(v));

// A form: one row per option. values: the limits set; onChange(key, value|null) for each edit.
export function limitsForm(options, values, onChange) {
  const grid = h("div.lim-grid");
  for (const o of options) {
    const v = values[o.key];
    const set = (val) => { onChange(o.key, val); row.classList.toggle("on", val != null); };
    let input;
    if (o.kind === "choice") {
      input = h("select.lim-in" + (v == null ? ".unset" : ""), { onchange: (e) => { e.target.classList.toggle("unset", e.target.value === "");
        set(e.target.value === "" ? null : (o.choices.find((c) => String(c) === e.target.value) ?? e.target.value)); } },
        h("option", { value: "" }, "Claude decides"),
        ...o.choices.map((c) => h("option", { value: String(c), selected: v != null && String(v) === String(c) }, `${c}${o.unit ? " " + o.unit : ""}`)));
    } else if (o.kind === "size" || o.kind === "range") {
      const a = h("input.lim-in.half", { type: "number", step: "any", placeholder: o.kind === "size" ? "width" : "min", value: v ? v[0] : "" });
      const b = h("input.lim-in.half", { type: "number", step: "any", placeholder: o.kind === "size" ? "length" : "max", value: v ? v[1] : "" });
      const commit = () => {
        const x = num(a.value), y = num(b.value);
        if (x == null && y == null) set(null);
        else if (x != null && y != null) set([x, y]);
      };
      a.addEventListener("change", commit); b.addEventListener("change", commit);
      input = h("span.lim-pair", a, h("span.lim-sep", o.kind === "size" ? "×" : "to"), b);
    } else {
      input = h("input.lim-in", { type: "number", step: "any", min: "0", placeholder: "Claude decides", value: v ?? "",
        onchange: (e) => set(num(e.target.value)) });
    }
    const row = h("label.lim-row" + (v != null ? ".on" : ""), { title: o.hint || "" },
      h("span.lim-l", o.label), input, h("span.lim-u", o.kind === "choice" ? "" : o.unit || ""));
    grid.appendChild(row);
  }
  return grid;
}

// The form bound to a project: loads the options and values, saves each edit.
export async function limitsPanel(pid, onSaved) {
  const box = h("div.lim");
  const [options, cur] = await Promise.all([limitOptions(), api(`/api/projects/${encodeURIComponent(pid)}/constraints`)]);
  const values = { ...(cur.values || {}) };
  const save = async (key, val) => {
    try {
      const s = await api(`/api/projects/${encodeURIComponent(pid)}`, { method: "PATCH", body: { constraints: { [key]: val } } });
      Object.assign(values, s.constraints || {});
      for (const k of Object.keys(values)) if (!(s.constraints || {})[k]) delete values[k];
      onSaved && onSaved(s.constraints || {});
    } catch (e) { toast(e.message, "error"); }
  };
  box.appendChild(limitsForm(options, values, save));
  return box;
}

// "2 set" or "Claude decides", for a collapsed section's heading.
export function limitsSummary(values) {
  const n = Object.keys(values || {}).length;
  return n ? `${n} set` : "Claude decides";
}
