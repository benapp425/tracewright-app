"""Schematics as code (generate) and precise in-place edits (edit.set_fields).

Generate: write design/schematic.py with Design / Builder / Part (see builder.py), run it, then
`finish(project)` upgrades the files to the running KiCad's format, runs ERC and exports the
netlist. The agent re-runs the script after every change; once the user edits the schematic by
hand, edit in place instead (or fold their change into the script first).
"""
import os
from .kisch import Project, Sheet, LibSymbol, make_ic, load_stock, snap, uid, rot_vec
from .builder import Design, Builder, Part, stock, power, KIND, NOTE, TITLE
from .. import kicad


def finish(project, erc=True):
    """Upgrade every sheet in place, export the netlist, run ERC; returns a short summary."""
    out = {"sheets": [], "erc": None}
    for f in project.sheets():
        kicad.sch_upgrade(f)
        out["sheets"].append(os.path.basename(f))
    kicad.netlist(project.sch, os.path.join(project.build, f"{project.stem}.net"))
    if erc:
        d = kicad.erc(project.sch, os.path.join(project.build, "erc.json"))
        v = [x for s in d.get("sheets", []) for x in s.get("violations", [])]
        by = {}
        for x in v:
            by[x["type"]] = by.get(x["type"], 0) + 1
        out["erc"] = {"violations": len(v), "by_type": by}
    return out
