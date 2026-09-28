// The command palette (⌘K): every command, every project, and the parts and nets of the open board.
import { h, clear, api, keys } from "./util.js";
import { icon } from "./icons.js";
import { allCommands, runCommand, state, go } from "./app.js";

let open = null;

function score(q, text) {
  if (!q) return 1;
  text = text.toLowerCase();
  const i = text.indexOf(q);
  if (i === 0) return 100 - text.length / 100;
  if (i > 0) return (text[i - 1] === " " ? 70 : 50) - i / 100;
  let t = 0, last = -1;                          // letters in order
  for (const ch of q) { const j = text.indexOf(ch, last + 1); if (j < 0) return 0; t += j - last === 1 ? 3 : 1; last = j; }
  return t;
}

export async function openPalette(initial = "") {
  if (open) { open.close(); return; }
  const input = h("input", { placeholder: "Search commands, projects and parts", value: initial, spellcheck: false });
  const list = h("div.plist");
  const bg = h("div.palette-bg", { onmousedown: (e) => { if (e.target === bg) close(); } },
    h("div.palette", h("div.pinput", icon("search", 17), input, h("span.kbd", "esc")), list));
  document.body.appendChild(bg);
  const close = () => { bg.remove(); open = null; };
  open = { close };

  let projects = [];
  api("/api/projects").then((ps) => { projects = ps.filter((p) => !p.archived); render(); }).catch(() => {});
  const ws = state.current && state.current.p ? state.current : null;
  const parts = ws && ws.findItems ? ws.findItems() : [];

  let items = [], sel = 0;
  const render = () => {
    const q = input.value.trim().toLowerCase();
    const groups = [];
    const cmds = allCommands().filter((c) => !c.hidden).map((c) => ({ kind: "cmd", title: c.title, icon: c.icon || "command", kbd: c.kbd, group: c.group || "Commands",
      run: () => runCommand(c.name), s: score(q, c.title + " " + (c.group || "")) }));
    const prj = projects.map((p) => ({ kind: "project", title: p.name, icon: "circuit-board", desc: p.description ? p.description.slice(0, 60) : "",
      group: "Projects", run: () => go("p/" + encodeURIComponent(p.id)), s: score(q, p.name) * 1.05 }));
    const its = q ? parts.map((it) => ({ kind: "item", title: it.label, icon: it.icon, desc: it.desc, group: ws ? `In ${ws.p.name}` : "Items",
      run: it.run, s: score(q, it.label) * 1.1 })) : [];
    const all = [...cmds, ...prj, ...its].filter((x) => x.s > 0);
    if (q) all.sort((a, b) => b.s - a.s);
    items = all.slice(0, q ? 40 : 60);
    if (!q) {                                               // grouped when not searching
      const by = {};
      for (const it of items) (by[it.group] = by[it.group] || []).push(it);
      const order = ["Project", "Review", "View", "Projects", "General", "Appearance", "Help"];
      items = Object.keys(by).sort((a, b) => ((order.indexOf(a) + 1) || 99) - ((order.indexOf(b) + 1) || 99)).flatMap((g) => by[g]);
    }
    sel = Math.min(sel, Math.max(0, items.length - 1));
    clear(list);
    if (!items.length) { list.appendChild(h("div.pempty", "Nothing matches.")); return; }
    let lastGroup = null;
    items.forEach((it, i) => {
      if (!q && it.group !== lastGroup) { list.appendChild(h("div.pgroup", it.group)); lastGroup = it.group; }
      list.appendChild(h("div.pi" + (i === sel ? ".on" : ""), { onmousemove: () => { if (sel !== i) { sel = i; mark(); } }, onclick: () => pick(i) },
        icon(it.icon || "command", 16), h("span.ellipsis", it.title), it.desc ? h("span.pd.ellipsis", it.desc) : null,
        it.kbd ? h("span.pk", keys(it.kbd).map((k) => h("span.kbd", k))) : null));
    });
  };
  const mark = () => {
    const rows = list.querySelectorAll(".pi");
    rows.forEach((r, i) => r.classList.toggle("on", i === sel));
    const r = rows[sel]; if (r) r.scrollIntoView({ block: "nearest" });
  };
  const pick = (i) => { const it = items[i]; if (!it) return; close(); setTimeout(() => it.run(), 10); };
  input.addEventListener("input", () => { sel = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); mark(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); mark(); }
    else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); pick(sel); }
  });
  render();
  setTimeout(() => input.focus(), 10);
}
