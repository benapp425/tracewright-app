"""Schematics as code (generate) and precise in-place edits (edit.set_fields).

Generate: write design/schematic.py with Design / Builder / Part (see builder.py), run it, then
`finish(project)` upgrades the files to the running KiCad's format, redraws them in the project's
schematic style (hierarchical or flat, style.py), runs ERC and exports the netlist. The agent re-runs the script after every change; once the user edits the schematic by
hand, edit in place instead (or fold their change into the script first).
"""
import os, json, sys, time
from .kisch import Project, Sheet, LibSymbol, make_ic, load_stock, snap, uid, rot_vec
from .builder import Design, Builder, Part, stock, power, KIND, NOTE, TITLE
from . import style
from .. import kicad


def finish(project, erc=True, plot=True):
    """Upgrade every sheet in place, redraw it in the project's schematic style when the script drew
    the other one (tracewright.json schematic.style: "hierarchical" or "flat"; see style.py), export
    the netlist, run ERC and read the sheets as KiCad plots them (plot: text that overlaps text, wires,
    symbols or block borders, from the strokes themselves -- the sch.render check); returns a short summary."""
    t0 = time.time()
    out = {"sheets": [], "erc": None}
    for f in project.sheets():
        kicad.sch_upgrade(f)
        out["sheets"].append(os.path.basename(f))
    progress(f"finish: {len(out['sheets'])} sheets upgraded")
    want = ((getattr(project, "cfg", None) or {}).get("schematic") or {}).get("style")
    if want in style.STYLES:
        from . import conventions
        bundles = conventions.get(getattr(project, "cfg", None) or {}).get("bundles", "bus")
        r = style.convert(project.sch, want, bundle_min=4 if bundles == "bus" else 0)
        out["style"] = {"ok": r["ok"], "message": r["message"]}
        progress(f"finish: {want} style: {r['message'][:120]}")
    net = os.path.join(project.build, f"{project.stem}.net")
    kicad.netlist(project.sch, net)
    want = os.path.join(project.hw, ".tracewright-nets.json")
    if os.path.exists(want):                        # a page laid out by rule: does KiCad see what was asked for?
        from . import auto
        from ..netlist import Netlist
        d = json.load(open(want))
        out["connections"] = auto.check_netlist(Netlist.load(net), d.get("nets") or {}) or "as asked"
        if d.get("crowded"):
            out["crowded"] = d["crowded"]
        if d.get("notes"):                              # notes to rewrite: long, or not beside a part
            out["notes"] = d["notes"]
    if erc:
        d = kicad.erc(project.sch, os.path.join(project.build, "erc.json"))
        v = [x for s in d.get("sheets", []) for x in s.get("violations", [])]
        by = {}
        for x in v:
            by[x["type"]] = by.get(x["type"], 0) + 1
        out["erc"] = {"violations": len(v), "by_type": by}
        progress(f"finish: ERC {len(v)} violations")
    if plot:
        out["plot"] = plot_check(project)
        out["critic"] = critic(project)
    out["seconds"] = round(time.time() - t0, 1)
    try:
        os.makedirs(project.build, exist_ok=True)
        with open(os.path.join(project.build, "schematic-report.json"), "w") as f:
            json.dump(out, f, indent=1)
    except OSError:
        pass
    progress(summary(out))
    return out


def progress(line):
    """One line of what the schematic run is doing, at once (stderr, flushed): a run watched from another process sees
    each sheet as it is laid out and the line that says it is done."""
    print(line, file=sys.stderr, flush=True)


def summary(r):
    """finish()'s report in one line, the last thing a run prints: "Schematic done: ..." (the full report is in
    build/schematic-report.json)."""
    con = r.get("connections")
    erc = (r.get("erc") or {}).get("violations")
    parts = [f"{len(r.get('sheets') or [])} sheets",
             "KiCad's netlist as asked" if con == "as asked" else f"{len(con or [])} connection problems",
             f"ERC {erc}" if erc is not None else None,
             f"{(r.get('plot') or {}).get('count', 0)} overlaps on the plot" if r.get("plot") else None,
             f"{len(r.get('crowded') or [])} crowded spots" if r.get("crowded") else None,
             None if (r.get("style") or {}).get("ok", True) else "style not applied"]
    return "Schematic done: " + ", ".join(p for p in parts if p) + " (build/schematic-report.json)"


def critic(project):
    """The sheets read as a person would: notes short and beside their parts, support parts at the pin they serve,
    connector pins named, titles filled (the sch.notes, sch.support and sch.labels checks). {count, items}."""
    try:
        from ..checks import load_all
        from ..checks.context import Context
        ctx = Context(project, offline=True)
        items = []
        for c in load_all():
            if c.id in ("sch.notes", "sch.support", "sch.labels"):
                try:
                    items += [f"{f.message}" for f in c.fn(ctx) if f.severity in ("error", "warning")]
                except Exception as e:                  # NotApplicable and the like: nothing to say
                    if type(e).__name__ != "NotApplicable":
                        items.append(f"{c.id}: {type(e).__name__}: {e}"[:160])
        return {"count": len(items), "items": items[:15]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def plot_check(project):
    """The sch.render check on the sheets as KiCad plots them now: {sheets, overlaps: [where: what], count}."""
    try:
        from ..checks import load_all
        from ..checks.context import Context
        chk = next(c for c in load_all() if c.id == "sch.render")
        ctx = Context(project, offline=True)
        found = [f for f in chk.fn(ctx) if f.severity in ("error", "warning")]
        return {"sheets": len(ctx.svgs), "count": len(found),
                "overlaps": [f"{(f.where or {}).get('sheet', '')} ({(f.where or {}).get('x')}, {(f.where or {}).get('y')}): {f.message}"
                             for f in found[:12]]}
    except Exception as e:                          # no kicad-cli to plot with: say so, do not fail the run
        return {"error": f"{type(e).__name__}: {e}"[:200]}
