"""Drawings for the people who build the board, as PDFs (KiCad plots them with its title block):

    fab_drawing(project)   build/docs/<name>-fab-drawing.pdf: the outline with its overall dimensions, the stack-up,
                           the drill table and the fab notes (material, thickness, copper, finish, mask, the smallest
                           track, space and hole, impedance, HDI), then KiCad's drill map
    assembly(project)      build/docs/<name>-assembly-top.pdf and -bottom.pdf (mirrored, when parts sit there): part
                           outlines, references and pad sketches, parts not fitted crossed out

The fab drawing is drawn on a copy of the board (the user's file is never touched).
"""
import os, shutil, uuid

from . import kicad


def fname(project):
    """The name the other outputs carry (the KiCad project's), as a file name: letters, digits, dots, dashes."""
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", getattr(project, "stem", None) or project.name).strip("-") or "board"


def _q(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _text(x, y, s, size=1.5, bold=False):
    return (f'\t(gr_text "{_q(s)}" (at {x:.3f} {y:.3f} 0) (layer "Dwgs.User") (uuid "{uuid.uuid4()}")\n'
            f'\t\t(effects (font (size {size:g} {size:g}) (thickness {size * (0.17 if bold else 0.12):.3f}){" (bold yes)" if bold else ""}) (justify left bottom)))\n')


def _line(a, b, w=0.15):
    return (f'\t(gr_line (start {a[0]:.3f} {a[1]:.3f}) (end {b[0]:.3f} {b[1]:.3f}) (stroke (width {w:g}) (type solid)) '
            f'(layer "Dwgs.User") (uuid "{uuid.uuid4()}"))\n')


def _arrow(tip, back, w=0.15, size=1.2):
    """Two strokes making an arrow head at tip, pointing away from back."""
    import math
    dx, dy = tip[0] - back[0], tip[1] - back[1]
    L = math.hypot(dx, dy) or 1
    ux, uy = dx / L, dy / L
    out = ""
    for s in (1, -1):
        out += _line(tip, (tip[0] - ux * size - uy * size * 0.45 * s, tip[1] - uy * size + ux * size * 0.45 * s), w)
    return out


def _dims(x0, y0, x1, y1):
    """Overall width below the outline, height left of it."""
    out = ""
    yb = y1 + 6
    out += _line((x0, y1 + 1), (x0, yb + 1)) + _line((x1, y1 + 1), (x1, yb + 1)) + _line((x0, yb), (x1, yb))
    out += _arrow((x0, yb), (x1, yb)) + _arrow((x1, yb), (x0, yb))
    out += _text((x0 + x1) / 2 - 6, yb - 0.8, f"{x1 - x0:.2f} mm", 1.6)
    xl = x0 - 6
    out += _line((x0 - 1, y0), (xl - 1, y0)) + _line((x0 - 1, y1), (xl - 1, y1)) + _line((xl, y0), (xl, y1))
    out += _arrow((xl, y0), (xl, y1)) + _arrow((xl, y1), (xl, y0))
    out += _text(xl - 13, (y0 + y1) / 2 + 0.8, f"{y1 - y0:.2f} mm", 1.6)
    return out


def fab_facts(project, b=None):
    """(notes, stack-up rows, drill rows) for the drawing and for the order page."""
    from .board import Board
    from .pro import ProjectSettings
    from . import hdi, stackup, constraints
    b = b or Board.load(project.pcb)
    cfg = getattr(project, "cfg", None) or {}
    pro = ProjectSettings.load(project.pro) if project.pro else ProjectSettings({})
    n = len(b.copper)
    plan = stackup.get(cfg)
    oz = lambda t: f"{round((t or 0.035) / 0.035 * 2) / 2:g} oz"
    finish = (constraints.get(cfg) or {}).get("finish") or ("ENIG" if n > 2 else "HASL (lead free)")
    tracks = [t.w for t in b.tracks]
    holes = [v.drill for v in b.vias] + [p.drill for p in b.pads() if p.drill]
    min_track = min(tracks) if tracks else float(pro.rule("min_track_width", 0.15) or 0.15)
    min_space = float(pro.rule("min_clearance", 0.15) or 0.15)
    notes = ["Material: FR-4, Tg 150 °C or higher.",
             f"Finished thickness: {b.thickness or 1.6:g} mm ± 10 %.",
             f"Copper: {oz(b.copper_mm('F.Cu'))} finished on the outer layers" + (f", {oz(b.copper_mm('In1.Cu'))} inner" if n > 2 else "") + ".",
             f"{n} copper layers" + (f", built as stack-up {plan['preset']} (table)" if plan and n > 2 else "") + ".",
             f"Surface finish: {finish}.",
             "Solder mask green both sides; silkscreen white.",
             f"Smallest track {min_track:.3f} mm, smallest space {min_space:.3f} mm, smallest hole {min(holes):.3f} mm." if holes else
             f"Smallest track {min_track:.3f} mm, smallest space {min_space:.3f} mm.",
             "Plated holes: at least 20 µm of copper in the barrel."]
    zs = _impedances(project)
    notes.append("Controlled impedance ±10 %: " + "; ".join(zs) + "." if zs else "Impedance not controlled.")
    notes += hdi.fab_notes(cfg, plan)
    notes += ["Build to IPC-6012 class 2; inspect to IPC-A-600 class 2.",
              "100 % electrical test against the IPC-D-356 netlist supplied.",
              "Outline as drawn on the board outline layer; all dimensions in mm."]
    rows = []
    for l in b.stackup:
        if l.get("type") == "copper":
            rows.append(f"{l['name']:<8} copper   {l['thickness']:.4f} mm")
        elif l.get("type") in ("core", "prepreg"):
            rows.append(f"{'':<8} {l['type']:<8} {l['thickness']:.4f} mm  εr {l.get('epsilon_r') or 4.5:g}")
    drills = {}
    for v in b.vias:
        k = (round(v.drill, 3), "plated", "via" if v.kind in ("through", "") else f"{v.kind} via")
        drills[k] = drills.get(k, 0) + 1
    for p in b.pads():
        if p.drill:
            k = (round(min(p.drill_w, p.drill_h) if p.drill_w and p.drill_h else p.drill, 3),
                 "non-plated" if p.kind == "np_thru_hole" else "plated", "slot" if p.drill_w and p.drill_h and abs(p.drill_w - p.drill_h) > 1e-3 else "hole")
            drills[k] = drills.get(k, 0) + 1
    drows = [(f"{d:.3f} mm", pl, what, str(n_)) for (d, pl, what), n_ in sorted(drills.items())]
    return notes, rows, drows


def _impedances(project):
    try:
        from .checks.context import Context
        from .netmodel import for_context
        m = for_context(Context(project, offline=True))
        out = {}
        for net, r in m.records.items():
            if r.get("impedance"):
                kind = "differential" if r.get("kind") == "pair" else "single-ended"
                out.setdefault(f"{r['impedance']:g} Ω {kind}", []).append(net.rsplit("/", 1)[-1])
        return [f"{k} ({', '.join(sorted(set(v))[:4])})" for k, v in out.items()]
    except Exception:
        return []


def _copy_board(project, work):
    os.makedirs(work, exist_ok=True)
    stem = os.path.splitext(os.path.basename(project.pcb))[0]
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        src = os.path.join(os.path.dirname(project.pcb), stem + ext)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(work, stem + ext))
    return os.path.join(work, stem + ".kicad_pcb")


def fab_drawing(project, out=None):
    from .board import Board
    from .outputs import _merge_pdfs
    b = Board.load(project.pcb)
    if not b.outline:
        raise ValueError("the board has no outline")
    work = os.path.join(project.build, "tmp-drawing")
    shutil.rmtree(work, ignore_errors=True)
    tmp = _copy_board(project, work)
    x0, y0, x1, y1 = b.size_box() if hasattr(b, "size_box") else _bbox(b)
    notes, rows, drows = fab_facts(project, b)
    add = _dims(x0, y0, x1, y1)
    x, y = x1 + 14, y0 + 2
    add += _text(x, y, "FAB NOTES", 2.0, True)
    y += 4
    for k, nline in enumerate(notes, 1):
        for j, part in enumerate(_wrap(nline, 70)):
            add += _text(x + (0 if j == 0 else 4), y, (f"{k}. " if j == 0 else "") + part, 1.4)
            y += 2.4
    y += 3
    if rows:
        add += _text(x, y, "STACK-UP (top to bottom)", 1.8, True)
        y += 3.4
        for r in rows:
            add += _text(x, y, r, 1.3)
            y += 2.2
        y += 3
    if drows:
        add += _text(x, y, "DRILLS (finished size)", 1.8, True)
        y += 3.4
        for r in drows:                                # columns at fixed places: KiCad's font is not monospaced
            for k, cell in enumerate(r):
                add += _text(x + (0, 15, 31, 46)[k], y, cell, 1.3)
            y += 2.2
    txt = open(tmp, encoding="utf-8").read().rstrip()
    assert txt.endswith(")")
    txt = _title_block(txt, project)
    open(tmp, "w", encoding="utf-8").write(txt[:-1] + add + ")\n")
    docs = os.path.join(project.build, "docs")
    os.makedirs(docs, exist_ok=True)
    name = fname(project)
    out = out or os.path.join(docs, f"{name}-fab-drawing.pdf")
    page = os.path.join(work, "drawing.pdf")
    kicad.cli("pcb", "export", "pdf", "--layers", "Edge.Cuts,Dwgs.User", "--mode-single", "--include-border-title",
              "--scale", "0", "--black-and-white", "-o", page, tmp)
    maps = os.path.join(work, "drill")
    os.makedirs(maps, exist_ok=True)
    pages = [page]
    try:
        kicad.cli("pcb", "export", "drill", "--format", "excellon", "--generate-map", "--map-format", "pdf", "-o", maps + os.sep, tmp)
        pages += sorted(os.path.join(maps, f) for f in os.listdir(maps) if f.endswith(".pdf"))
    except kicad.KiCadError:
        pass
    if len(pages) == 1 or not _merge_pdfs(pages, out):
        shutil.copy2(page, out)
    shutil.rmtree(work, ignore_errors=True)
    return {"pdf": out, "notes": notes, "stackup": rows, "drills": drows, "pages": len(pages)}


def _title_block(txt, project):
    """The copy's title block filled in where the board's is empty: the board's name, today, its revision."""
    import re, time
    cfg = getattr(project, "cfg", None) or {}
    title = f"{project.name}: fab drawing"
    rev = str(cfg.get("rev") or cfg.get("revision") or "")
    block = (f'(title_block (title "{_q(title)}") (date "{time.strftime("%Y-%m-%d")}")' + (f' (rev "{_q(rev)}")' if rev else "") + ")")
    m = re.search(r"\(title_block\b", txt)
    if m is None:
        i = txt.find("(layers")
        return txt[:i] + block + "\n\t" + txt[i:] if i > 0 else txt
    depth, j = 0, m.start()
    while j < len(txt):
        depth += {"(": 1, ")": -1}.get(txt[j], 0)
        if depth == 0:
            break
        j += 1
    old = txt[m.start():j + 1]
    if '(title "' in old and not re.search(r'\(title ""\)', old):
        return txt.replace(old, re.sub(r'\(title "([^"]*)"\)', lambda q: f'(title "{_q(q.group(1))}: fab drawing")', old, 1), 1)
    return txt[:m.start()] + block + txt[j + 1:]


def _bbox(b):
    from . import geom
    outer = max(b.outline, key=lambda o: abs(geom.area(o)))
    return geom.bbox(outer)


def _wrap(s, n):
    words, out, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n and cur:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    return out + ([cur] if cur else [])


def assembly(project):
    """Top and (when parts sit there) bottom assembly drawings."""
    from .board import Board
    b = Board.load(project.pcb)
    docs = os.path.join(project.build, "docs")
    os.makedirs(docs, exist_ok=True)
    name = fname(project)
    out = []
    for side in ("F", "B"):
        if side == "B" and not any(fp.side == "B" for fp in b.fp_list):
            continue
        path = os.path.join(docs, f"{name}-assembly-{'top' if side == 'F' else 'bottom'}.pdf")
        args = ["pcb", "export", "pdf", "--layers", f"{side}.Fab,{side}.Silkscreen,Edge.Cuts", "--mode-single", "--include-border-title",
                "--sketch-pads-on-fab-layers", "--crossout-DNP-footprints-on-fab-layers", "--scale", "0"]
        if side == "B":
            args.append("--mirror")
        kicad.cli(*args, "-o", path, project.pcb)
        out.append(path)
    return out
