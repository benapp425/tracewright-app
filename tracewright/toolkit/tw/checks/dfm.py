"""Manufacturability against the fab profile (tw/dfm.py): the rules KiCad enforces, and the copper."""
from . import check, Finding
from .. import geom


def _lim(fab, k):
    return float(fab.get(k, 0) or 0)


@check("dfm.rules", "Board rules are within the fab's limits", "Manufacturing", needs=("pcb",))
def dfm_rules(ctx):
    """KiCad's DRC only enforces the rules in the project; this checks those rules (and every net
    class) against what the fab can make, so a clean DRC means a buildable board."""
    fab, pro = ctx.fab, ctx.pro
    out = []
    src = f"{fab['_house']} {fab['_layers']}-layer ({fab['_source']}, {fab['_date']})"
    r = pro.rules

    def cmp(name, val, lo_key, rec_key=None, what=""):
        if val is None:
            return
        lo = _lim(fab, lo_key)
        if val + 1e-6 < lo:
            out.append(Finding("dfm.rules", "error", f"{what or name} {val:.3f} mm is below the {src} minimum {lo:.3f} mm",
                               key=f"dfm.rules:{name}"))
        elif rec_key and val + 1e-6 < _lim(fab, rec_key):
            out.append(Finding("dfm.rules", "info", f"{what or name} {val:.3f} mm is below the recommended "
                               f"{_lim(fab, rec_key):.3f} mm (fine, but tighter than needed?)", key=f"dfm.rules:rec:{name}"))
    cmp("min_track_width", r.get("min_track_width"), "min_track", None, "board minimum track width")
    cmp("min_clearance", r.get("min_clearance"), "min_space", None, "board minimum clearance")
    cmp("min_through_hole_diameter", r.get("min_through_hole_diameter"), "min_via_drill", None, "board minimum drill")
    cmp("min_via_annular_width", r.get("min_via_annular_width"), "min_annular", None, "board minimum via annular ring")
    cmp("min_copper_edge_clearance", r.get("min_copper_edge_clearance"), "min_edge_clearance", None, "copper-to-edge rule")
    for name, c in pro.classes.items():
        cmp(f"class:{name}:track", c.get("track_width"), "min_track", "rec_track", f"net class {name} track")
        cmp(f"class:{name}:clearance", c.get("clearance"), "min_space", "rec_space", f"net class {name} clearance")
        vd, vr = c.get("via_drill"), c.get("via_diameter")
        cmp(f"class:{name}:via_drill", vd, "min_via_drill", None, f"net class {name} via drill")
        if vd and vr:
            cmp(f"class:{name}:annular", (vr - vd) / 2, "min_annular", "rec_annular", f"net class {name} via annular ring")
    return out


@check("dfm.copper", "Copper, drills and outline the fab can make", "Manufacturing", needs=("pcb",))
def dfm_copper(ctx):
    """The board as drawn: narrowest track, smallest via and hole, thinnest annular ring, board size
    and thickness, and a single closed outline -- against the fab profile."""
    fab, b = ctx.fab, ctx.board
    out = []
    if b.tracks:
        t = min(b.tracks, key=lambda t: t.w)
        if t.w + 1e-6 < _lim(fab, "min_track"):
            out.append(Finding("dfm.copper", "error", f"track {t.w:.3f} mm on {t.net or 'no net'} is below the fab minimum "
                               f"{_lim(fab, 'min_track'):.3f} mm", {"net": t.net, "x": t.a[0], "y": t.a[1], "layer": t.layer},
                               key="dfm.copper:track"))
    for v in b.vias:
        if v.drill + 1e-6 < _lim(fab, "min_via_drill"):
            out.append(Finding("dfm.copper", "error", f"via drill {v.drill:.2f} mm is below {_lim(fab, 'min_via_drill'):.2f} mm",
                               {"net": v.net, "x": v.x, "y": v.y}, key="dfm.copper:viadrill"))
            break
    for v in b.vias:
        ring = (v.d - v.drill) / 2
        if ring + 1e-6 < _lim(fab, "min_annular"):
            out.append(Finding("dfm.copper", "error", f"via annular ring {ring:.3f} mm ({v.d:.2f}/{v.drill:.2f}) is below "
                               f"{_lim(fab, 'min_annular'):.3f} mm", {"net": v.net, "x": v.x, "y": v.y},
                               key="dfm.copper:annular"))
            break
    for fp in b.fp_list:
        for p in fp.pads:
            if not p.drill:
                continue
            d = min(p.drill_w, p.drill_h)
            lim = _lim(fab, "min_npth_drill") if p.kind == "np_thru_hole" else _lim(fab, "min_pth_drill")
            if d + 1e-6 < lim:
                out.append(Finding("dfm.copper", "error", f"{fp.ref}.{p.num}: {d:.2f} mm hole is below the fab minimum {lim:.2f} mm",
                                   {"ref": fp.ref, "x": p.x, "y": p.y}, key=f"dfm.copper:hole:{fp.ref}:{p.num}"))
            if p.kind == "thru_hole":
                ring = (min(p.w, p.h) - d) / 2
                if ring + 1e-6 < _lim(fab, "min_annular"):
                    out.append(Finding("dfm.copper", "warning", f"{fp.ref}.{p.num}: annular ring {ring:.3f} mm is below "
                                       f"{_lim(fab, 'min_annular'):.3f} mm", {"ref": fp.ref, "x": p.x, "y": p.y},
                                       key=f"dfm.copper:ring:{fp.ref}:{p.num}"))
    if not b.outline:
        out.append(Finding("dfm.copper", "error", "no closed board outline on Edge.Cuts", key="dfm.copper:outline"))
    elif b.outline_open:
        out.append(Finding("dfm.copper", "error", f"{len(b.outline_open)} Edge.Cuts pieces do not close into the outline",
                           {"x": b.outline_open[0][0][0], "y": b.outline_open[0][0][1]}, key="dfm.copper:open"))
    else:
        w, h = b.size()
        mx = fab.get("max_size_mm", [500, 400])
        if max(w, h) > max(mx) or min(w, h) > min(mx):
            out.append(Finding("dfm.copper", "error", f"board {w:.1f} x {h:.1f} mm is larger than the fab's {mx[0]} x {mx[1]} mm",
                               key="dfm.copper:size"))
        if min(w, h) < 10:
            out.append(Finding("dfm.copper", "info", f"board is only {w:.1f} x {h:.1f} mm: assembly may need a panel/rails",
                               key="dfm.copper:small"))
    th = b.thickness
    if fab.get("thicknesses") and all(abs(th - t) > 0.01 for t in fab["thicknesses"]):
        out.append(Finding("dfm.copper", "warning", f"board thickness {th:g} mm is not a standard option "
                           f"({', '.join(str(t) for t in fab['thicknesses'])})", key="dfm.copper:thick"))
    fl = ctx.setting("fab.layers")
    if fl and int(fl) != len(b.copper):
        out.append(Finding("dfm.copper", "warning", f"tracewright.json says {fl} layers but the board has {len(b.copper)}",
                           key="dfm.copper:layers"))
    return out
