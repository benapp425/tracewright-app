"""Will the board go in its box? The board against the enclosure's inside -- either way round, with room at the walls;
the parts' heights (from their 3D models) under the lid and, on the bottom, over the standoffs; the connectors at the
board's edge, each with the opening its wall needs -- and an OpenSCAD enclosure to start from: walls, floor, standoffs at
the mounting holes, the openings, and a lid.

    fit(ctx, box={"w": 60, "l": 45, "h": 25}, standoff=5, gap=1, lid=1) -> {"ok", "lines", "openings", "holes", "heights"}
    scad(ctx, box, ...) -> OpenSCAD text
"""
import re

from . import geom

CONN = re.compile(r"^(J|P|CN|USB|SW|BT|X)\d", re.I)


def _outline(b):
    if not b.outline:
        raise ValueError("the board has no outline")
    return geom.bbox(max(b.outline, key=lambda o: abs(geom.area(o))))


def _heights(ctx):
    from . import models3d
    out = {}
    for fp in ctx.board.fp_list:
        try:
            hgt, src = models3d.height(fp, ctx.p)
        except Exception:
            hgt, src = None, "no model"
        out[fp.ref] = (hgt, fp.side)
    return out


def holes(b):
    """Mounting holes: footprints named H / MH, and plated or bare holes of 2.5 mm and over, as (ref, x, y, d)."""
    out = []
    for fp in b.fp_list:
        big = [p for p in fp.pads if p.drill and min(p.drill_w or p.drill, p.drill_h or p.drill) >= 2.5]
        if re.match(r"^(H|MH)\d", fp.ref) or big:
            p = big[0] if big else (fp.pads[0] if fp.pads else None)
            d = (min(p.drill_w or p.drill, p.drill_h or p.drill) if p and p.drill else 3.2)
            out.append((fp.ref, round(fp.x, 3), round(fp.y, 3), round(d, 2)))
    return out


def openings(b, heights, standoff, thick, clear=0.5):
    """The wall openings the edge connectors need: (ref, wall, from, to along the wall from its first corner, bottom,
    top above the floor)."""
    x0, y0, x1, y1 = _outline(b)
    out = []
    for fp in b.fp_list:
        if not CONN.match(fp.ref):
            continue
        bb = fp.bbox()
        dists = {"left": bb[0] - x0, "right": x1 - bb[2], "top": bb[1] - y0, "bottom": y1 - bb[3]}
        wall, d = min(dists.items(), key=lambda kv: kv[1])
        if d > 1.0:
            continue                                   # not at an edge
        hgt = heights.get(fp.ref, (None, "F"))[0] or 3.0
        z0 = standoff + thick if fp.side == "F" else max(0.0, standoff - hgt)
        z1 = z0 + hgt if fp.side == "F" else standoff
        a, c = ((bb[1] - y0, bb[3] - y0) if wall in ("left", "right") else (bb[0] - x0, bb[2] - x0))
        out.append({"ref": fp.ref, "wall": wall, "from": round(a - clear, 2), "to": round(c + clear, 2),
                    "bottom": round(z0 - clear, 2), "top": round(z1 + clear, 2)})
    return out


def fit(ctx, box, standoff=5.0, gap=1.0, lid=1.0):
    b = ctx.board
    x0, y0, x1, y1 = _outline(b)
    W, H = x1 - x0, y1 - y0
    bw, bl, bh = float(box["w"]), float(box["l"]), float(box["h"])
    thick = b.thickness or 1.6
    lines, ok = [], True
    room = [(bw - W, bl - H, "as drawn"), (bw - H, bl - W, "turned a quarter")]
    best = max(room, key=lambda r: min(r[0], r[1]))
    if min(best[0], best[1]) < 2 * gap:
        ok = False
        lines.append(f"The board ({W:.1f} × {H:.1f} mm) does not fit inside {bw:g} × {bl:g} mm with {gap:g} mm at the walls "
                     f"(short by {max(0.0, 2 * gap - min(best[0], best[1])):.1f} mm)")
    else:
        lines.append(f"The board ({W:.1f} × {H:.1f} mm) fits {best[2]}, with {best[0] / 2:.1f} and {best[1] / 2:.1f} mm to the walls")
    hs = _heights(ctx)
    top = max(((h, r) for r, (h, s) in hs.items() if h is not None and s == "F"), default=(0.0, None))
    bot = max(((h, r) for r, (h, s) in hs.items() if h is not None and s == "B"), default=(0.0, None))
    unknown = sorted(r for r, (h, s) in hs.items() if h is None and not re.match(r"^(H|MH|FID|TP)\d", r))
    need = standoff + thick + top[0] + lid
    if need > bh + 1e-6:
        ok = False
        lines.append(f"Too tall: {standoff:g} mm standoffs + {thick:g} mm board + {top[0]:.1f} mm ({top[1]}) + {lid:g} mm under the lid "
                     f"= {need:.1f} mm, the box has {bh:g} mm")
    elif top[1]:
        lines.append(f"Tallest part {top[1]} ({top[0]:.1f} mm): {bh - need + lid:.1f} mm left under the lid")
    if bot[1] and bot[0] > standoff - 0.5:
        ok = False
        lines.append(f"{bot[1]} on the bottom is {bot[0]:.1f} mm tall: the standoffs ({standoff:g} mm) must be taller")
    if unknown:
        lines.append(f"No 3D model to measure: {', '.join(unknown[:8])}{' ...' if len(unknown) > 8 else ''} (their heights are not counted)")
    ops = openings(b, hs, standoff, thick)
    for o in ops:
        lines.append(f"{o['ref']} needs an opening in the {o['wall']} wall, {o['to'] - o['from']:.1f} × {o['top'] - o['bottom']:.1f} mm, "
                     f"{o['from']:.1f} mm along it and {o['bottom']:.1f} mm above the floor")
    hl = holes(b)
    if not hl:
        lines.append("No mounting holes: the board has nothing to screw it down by")
    return {"ok": ok, "lines": lines, "openings": ops, "holes": hl, "board": [round(W, 2), round(H, 2)], "turned": best[2] != "as drawn",
            "heights": {"top": top, "bottom": bot}, "box": {"w": bw, "l": bl, "h": bh}, "standoff": standoff}


def scad(ctx, box, standoff=5.0, gap=1.0, lid=1.0, wall=2.0, floor=2.0):
    """An OpenSCAD enclosure for the board as it sits (not turned): parameters at the top, the openings and standoffs
    in board coordinates from the outline's corner."""
    r = fit(ctx, box, standoff, gap, lid)
    b = ctx.board
    x0, y0, x1, y1 = _outline(b)
    W, H = x1 - x0, y1 - y0
    bw, bl, bh = r["box"]["w"], r["box"]["l"], r["box"]["h"]
    ox, oy = (bw - W) / 2, (bl - H) / 2                 # the board centred in the box
    s = [f"// {ctx.p.name}: an enclosure to start from (made by Tracewright from the board)",
         f"// inside {bw:g} x {bl:g} x {bh:g} mm; board {W:.2f} x {H:.2f} mm on {standoff:g} mm standoffs",
         f"wall = {wall:g};", f"floor_t = {floor:g};", f"inner = [{bw:g}, {bl:g}, {bh:g}];", f"standoff = {standoff:g};",
         f"board_at = [{ox:.2f}, {oy:.2f}];  // the board's outline corner inside the box", "$fn = 48;", "",
         "module shell() {", "  difference() {", "    translate([-wall, -wall, -floor_t]) cube([inner[0] + 2 * wall, inner[1] + 2 * wall, inner[2] + floor_t]);",
         "    cube([inner[0], inner[1], inner[2] + 1]);"]
    for o in r["openings"]:
        if o["wall"] in ("left", "right"):
            s.append(f"    // {o['ref']}")
            s.append(f"    translate([{(-wall - 1) if o['wall'] == 'left' else bw - 1:.2f}, {oy + o['from']:.2f}, {o['bottom']:.2f}]) "
                     f"cube([wall + 2, {o['to'] - o['from']:.2f}, {o['top'] - o['bottom']:.2f}]);")
        else:
            s.append(f"    // {o['ref']}")
            s.append(f"    translate([{ox + o['from']:.2f}, {(-wall - 1) if o['wall'] == 'top' else bl - 1:.2f}, {o['bottom']:.2f}]) "
                     f"cube([{o['to'] - o['from']:.2f}, wall + 2, {o['top'] - o['bottom']:.2f}]);")
    s += ["  }"]
    for ref, x, y, d in r["holes"]:
        s.append(f"  // {ref}: a standoff with a hole for an M{max(2, round(d - 0.4)):g} screw")
        s.append(f"  translate([board_at[0] + {x - x0:.2f}, board_at[1] + {y - y0:.2f}, 0]) difference() {{ cylinder(d = {d + 3:.1f}, h = standoff); "
                 f"cylinder(d = {max(1.6, d - 0.6):.1f}, h = standoff + 1); }}")
    s += ["}", "", "module lid() {", "  translate([-wall, -wall, 0]) cube([inner[0] + 2 * wall, inner[1] + 2 * wall, wall]);",
          "  translate([0.2, 0.2, -2]) difference() { cube([inner[0] - 0.4, inner[1] - 0.4, 2]); translate([1.2, 1.2, -1]) cube([inner[0] - 2.8, inner[1] - 2.8, 4]); }",
          "}", "", "shell();", "translate([0, inner[1] + 2 * wall + 10, 0]) lid();", ""]
    return "\n".join(s), r
