"""Panels: copies of a rectangular board side by side, V-scored apart, with rails that carry tooling holes and
fiducials -- built as its own board file (build/panel/), with Gerbers and a picture.

    make(project, nx=2, ny=2, rail=5.0, rails="tb", fiducials=True, tooling=True) -> {"pcb", "zip", "svg", "size", "lines"}
    shape(board) -> (ok, note): whether the outline can be V-scored (straight edges, square corners)

A board that is not a rectangle cannot be V-scored: say so and let the fab panelize it with tabs.
"""
import os, shutil, zipfile

from . import kicad, geom


def shape(b):
    if not b.outline:
        return False, "the board has no outline"
    if len(b.outline) > 1:
        return False, "the board has cut-outs inside it: a V-score panel works, but check the cut-outs reach no edge"
    outer = b.outline[0]
    x0, y0, x1, y1 = geom.bbox(outer)
    ratio = abs(geom.area(outer)) / max(1e-9, (x1 - x0) * (y1 - y0))
    if ratio < 0.97:
        return False, "the outline is not a rectangle: a V-score only cuts straight across; ask the fab to panelize it with tabs"
    if ratio < 0.9995:
        return True, "its rounded corners come out square (a V-score cuts straight across); round them by hand or ask the fab"
    return True, ""


def overhangs(b, tol=0.05):
    """[(ref, edge, mm)] parts whose body or courtyard reaches past the board's outline (a USB receptacle's lip)."""
    if not b.outline:
        return []
    x0, y0, x1, y1 = geom.bbox(max(b.outline, key=lambda o: abs(geom.area(o))))
    out = []
    for fp in b.fp_list:
        bb = fp.bbox()
        for edge, d in (("left", x0 - bb[0]), ("right", bb[2] - x1), ("top", y0 - bb[1]), ("bottom", bb[3] - y1)):
            if d > tol:
                out.append((fp.ref, edge, d))
    return out


def make(project, nx=2, ny=2, rail=5.0, rails="tb", fiducials=True, tooling=True):
    from .board import Board
    b = Board.load(project.pcb)
    ok, note = shape(b)
    if not ok:
        raise ValueError(note)
    nx, ny = int(nx), int(ny)
    if not (1 <= nx <= 10 and 1 <= ny <= 10) or nx * ny < 2:
        raise ValueError("a panel of 2 to 100 boards, at most 10 a side")
    if rails not in ("tb", "lr", "all", "none"):
        raise ValueError("rails: tb, lr, all or none")
    out_dir = os.path.join(project.build, "panel")
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    stem = os.path.splitext(os.path.basename(project.pcb))[0] + "-panel"
    pcb = os.path.join(out_dir, stem + ".kicad_pcb")
    for ext in (".kicad_pro", ".kicad_dru"):
        src = os.path.splitext(project.pcb)[0] + ext
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, stem + ext))
    cfg = getattr(project, "cfg", None) or {}
    label = f"{project.name} rev {cfg.get('rev')}" if cfg.get("rev") else project.name
    rc, out, err = kicad.kpy("panel_kpy.py", input_json={"board": project.pcb, "out": pcb, "nx": nx, "ny": ny, "rail": float(rail),
                                                         "rails": rails, "fiducials": bool(fiducials), "tooling": bool(tooling),
                                                         "label": label}, check=False)
    res = kicad.last_json(out) or {"ok": False, "error": (err or out)[-600:]}
    if not res.get("ok"):
        raise ValueError("the panel could not be made: " + str(res.get("error"))[:400])
    gdir = os.path.join(out_dir, "gerbers")
    os.makedirs(gdir)
    n = len(b.copper)
    layers = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu", "F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen",
                                                                   "F.Mask", "B.Mask", "Edge.Cuts", "Cmts.User"]
    kicad.cli("pcb", "export", "gerbers", "--layers", ",".join(layers), "--subtract-soldermask", "-o", gdir + os.sep, pcb)
    kicad.cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th", "-o", gdir + os.sep, pcb)
    zpath = os.path.join(out_dir, f"{stem}-gerbers.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(gdir)):
            z.write(os.path.join(gdir, f), f)
    svg = os.path.join(out_dir, stem + ".svg")
    try:
        kicad.cli("pcb", "export", "svg", "--layers", "Edge.Cuts,Cmts.User,F.Cu,F.Silkscreen,F.Mask", "--mode-single",
                  "--exclude-drawing-sheet", "--fit-page-to-board", "-o", svg, pcb)
    except kicad.KiCadError:
        svg = None
    w, h = res["size"]
    lines = [f"{nx * ny} boards ({nx} × {ny}), panel {w:.1f} × {h:.1f} mm"
             + (f", {rail:g} mm rails ({'top and bottom' if rails == 'tb' else 'left and right' if rails == 'lr' else 'all round'})" if rails != "none" else ""),
             "V-scored between the boards: tell the fab to V-score along the lines on the Comments layer (marked V-CUT)"]
    if note:
        lines.append(note)
    for ref, side_, by in overhangs(b):                # a part past the edge meets the next board in a V-score panel
        facing = (side_ in ("left", "right") and nx > 1) or (side_ in ("top", "bottom") and ny > 1)
        if facing:
            lines.append(f"{ref} reaches {by:.1f} mm past the {side_} edge, into the next board: turn the copies so it faces a rail, "
                         f"or have the fab route the panel with tabs and a gap")
    if max(w, h) > 250:
        lines.append(f"{max(w, h):.0f} mm across: past 250 mm most fabs' assembly lines want a smaller panel")
    if min(res["board"]) < 20 and rails == "none":
        lines.append("small boards with no rails: the assembly line needs rails to grip; add them")
    return {"pcb": pcb, "zip": zpath, "svg": svg, "size": [w, h], "boards": nx * ny, "cuts": res.get("cuts"), "lines": lines}
