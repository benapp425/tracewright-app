// Editing the schematic in the app (schedit.py): a part's fields and flags from its card, a net's name from one of
// its labels. Each edit is one undo step (Undo in the toast, or ⌘Z in the schematic view). Edits wait while Claude
// works on the design, and while KiCad has the schematic open (it would save its own copy over them).
import { h, api, toast, modal, popover } from "./util.js";
import { icon } from "./icons.js";

const enc = encodeURIComponent;
const FIELDS = [["Value", "value", "10k, 100n, AMS1117-3.3"], ["Footprint", "footprint", "Resistor_SMD:R_0402_1005Metric"],
  ["MPN", "mpn", "Manufacturer's part number"], ["Manufacturer", "manufacturer", ""], ["LCSC", "lcsc", "C25744"],
  ["Datasheet", "datasheet", "https://…"], ["Description", "description", ""]];
const LABEL = { MPN: "Part number", LCSC: "LCSC code", Datasheet: "Data sheet" };

export async function schEdit(ws, ops, label) {
  const r = await api(`/api/projects/${enc(ws.pid)}/schematic/edit`, { body: { ops, label } });
  ws.inspector && ws.inspector.invalidate();
  const sch = ws.views && ws.views.schematic;
  if (sch) { sch.history = r.history; sch.reload && sch.reload(); }
  if (!(r.changes || []).length) { toast("Nothing to change", "info", 2500); return r; }
  toast(label, "ok", 6000, { label: "Undo", run: () => schStep(ws, true) });
  if (r.board_out_of_date) setTimeout(() => toast("The board has the old footprint or net names", "info", 9000,
    { label: "Update the board", run: () => updateBoard(ws) }), 400);
  return r;
}

export async function schStep(ws, back) {
  try {
    const r = await api(`/api/projects/${enc(ws.pid)}/schematic/${back ? "undo" : "redo"}`, { body: {} });
    ws.inspector && ws.inspector.invalidate();
    const sch = ws.views && ws.views.schematic;
    if (sch) { sch.history = r.history; sch.reload && sch.reload(); }
    toast(`${back ? "Undid" : "Redid"}: ${r.label}`, "ok", 3000);
  } catch (e) { toast(e.message, "error"); }
}

export async function updateBoard(ws) {
  try {
    await api(`/api/projects/${enc(ws.pid)}/board/sync`, { body: {} });
    toast("The board is up to date with the schematic", "ok", 4000, { label: "Show", run: () => ws.show("board") });
  } catch (e) { toast(e.message, "error"); }
}

// a part's fields and flags, as a form
export function editPart(ws, info) {
  const inputs = {};
  const rows = FIELDS.map(([k, key, ph]) => {
    const i = h("input", { value: info[key] || "", placeholder: ph, spellcheck: false, autocomplete: "off" });
    inputs[k] = [i, info[key] || ""];
    return h("div.field.pe-f" + (k === "Description" || k === "Datasheet" ? ".wide" : ""), h("label", LABEL[k] || k), i);
  });
  const flag = (text, sub, on) => {
    const i = h("input", { type: "checkbox", checked: !!on });
    return [i, h("label.pe-flag", h("span.switch", i, h("span.track")), h("span", h("b", text), h("span.pe-sub", sub)))];
  };
  const [dnp, dnpRow] = flag("Not fitted (DNP)", "Kept on the board and in the BOM as not fitted", info.dnp);
  const [errBox] = [h("div.pe-err", { style: { display: "none" } })];
  const save = h("button.btn.primary", { onclick: async () => {
    const fields = {};
    for (const [k, [i, was]] of Object.entries(inputs)) if (i.value.trim() !== was) fields[k] = i.value.trim();
    const ops = [];
    if (Object.keys(fields).length) ops.push({ op: "fields", ref: info.ref, fields });
    if (dnp.checked !== !!info.dnp) ops.push({ op: "flags", ref: info.ref, dnp: dnp.checked });
    if (!ops.length) { m.close(); return; }
    save.disabled = true; errBox.style.display = "none";
    const what = Object.keys(fields).map((k) => (LABEL[k] || k).toLowerCase()).concat(ops.some((o) => o.op === "flags") ? [dnp.checked ? "not fitted" : "fitted"] : []);
    try { await schEdit(ws, ops, `${info.ref}: ${what.join(", ")}`); m.close(); }
    catch (e) { errBox.textContent = e.message; errBox.style.display = ""; save.disabled = false; }
  } }, "Save");
  const m = modal({ title: `Edit ${info.ref}`, sub: "Written into the schematic; one undo step", icon: "pencil", cls: "pe",
    body: [h("div.pe-grid", rows), dnpRow, errBox],
    actions: [h("button.btn", { onclick: () => m.close() }, "Cancel"), save] });
  m.box.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing && e.target.tagName === "INPUT" && e.target.type !== "checkbox") { e.preventDefault(); save.click(); } });
  return m;
}

// a net's name, from one of its labels on the sheet: every label in the same scope follows, and KiCad's netlist
// proves the connections hold before anything is written
export function renameLabel(ws, anchor, lab, sheet) {
  const scope = { global_label: "every global label of this name, on every sheet", label: "every label of this name on this sheet",
    hierarchical_label: "this sheet's hierarchical label and the sheet pins that meet it" }[lab.kind] || "";
  const input = h("input", { value: lab.text, spellcheck: false, autocomplete: "off" });
  const errBox = h("div.pe-err", { style: { display: "none" } });
  const go = async () => {
    const to = input.value.trim();
    if (!to || to === lab.text) { pop.close(); return; }
    ok.disabled = true; errBox.style.display = "none";
    try { await schEdit(ws, [{ op: "rename", from: lab.text, to, kind: lab.kind, sheet }], `Renamed ${lab.text} to ${to}`); pop.close(); }
    catch (e) { errBox.textContent = e.message; errBox.style.display = ""; ok.disabled = false; }
  };
  const ok = h("button.btn.sm.primary", { onclick: go }, "Rename");
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); go(); } });
  const pop = popover(anchor, h("div.rn", h("div.rn-h", icon("pencil", 13), h("b", "Rename the net")), input,
    h("div.rn-sub", `Renames ${scope}.`), errBox, h("div.rn-acts", h("button.btn.sm", { onclick: () => pop.close() }, "Cancel"), ok)), { cls: "rn-pop" });
  setTimeout(() => { input.focus(); input.select(); }, 30);
  return pop;
}
