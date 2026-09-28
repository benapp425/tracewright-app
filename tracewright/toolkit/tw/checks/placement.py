"""Placement: every part is on the board, nothing collides, polarity is marked, silk is readable."""
import math, collections
from . import check, Finding, NotApplicable
from .. import geom

POLARISED_PREFIX = ("D", "LED", "BT", "CR", "ZD")


def _is_polarized(fp):
    ref = fp.ref.rstrip("0123456789")
    lib = fp.lib_id.lower()
    if ref in ("LED",) or ref.startswith(("D", "BT")) and not ref.startswith(("DS", "DZ")):
        return True
    if ref == "C" and any(k in lib for k in ("cp_", "elec", "tantalum", "polarized", "cpol")):
        return True
    return False


@check("pcb.placement", "Parts on the board, clear of each other", "Placement", needs=("pcb",))
def pcb_placement(ctx):
    """Footprints outside the outline (or parked off the board), courtyards that overlap on the same
    side, THT parts mixed on both sides, and bottom-side parts (extra assembly cost)."""
    b = ctx.board
    out = []
    if not b.outline:
        return [Finding("pcb.placement", "error", "the board has no closed Edge.Cuts outline",
                        hint="Draw the outline on Edge.Cuts as one closed shape.", key="pcb.placement:outline")]
    ol = b.outline[0]
    for fp in b.fp_list:
        if not fp.pads and not fp.shapes:
            continue
        pts = [q for p in fp.pads for q in p.poly] or [(fp.x, fp.y)]
        outside = [q for q in pts if not geom.inside(q, ol)]
        if len(outside) == len(pts):
            out.append(Finding("pcb.placement", "error", f"{fp.ref} is off the board (not placed yet?)",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"pcb.placement:off:{fp.ref}"))
        elif outside and not fp.ref.startswith(("H", "MH", "J", "P", "SW", "U.FL")):
            out.append(Finding("pcb.placement", "warning", f"{fp.ref} has pads outside the board outline",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"pcb.placement:edge:{fp.ref}"))
    cys = []
    for fp in b.fp_list:
        for loop in fp.courtyard():
            cys.append((fp, loop, geom.bbox(loop)))
    missing_cy = [fp.ref for fp in b.fp_list if fp.pads and not fp.courtyard() and not fp.ref.startswith(("#", "H", "FID", "TP", "MH"))]
    if missing_cy:
        out.append(Finding("pcb.placement", "info", f"{len(missing_cy)} footprints have no courtyard: " + ", ".join(missing_cy[:12]),
                           key="pcb.placement:nocy"))
    cys.sort(key=lambda c: c[2][0])
    seen = set()
    for i, (fa, la, ba) in enumerate(cys):
        for fb, lb, bb in cys[i + 1:]:
            if bb[0] > ba[2]:
                break
            if fa is fb or fa.side != fb.side or not geom.bbox_overlap(ba, bb):
                continue
            key = tuple(sorted((fa.ref, fb.ref)))
            if key in seen:
                continue
            if _polys_overlap(la, lb):
                seen.add(key)
                out.append(Finding("pcb.placement", "warning", f"courtyards of {key[0]} and {key[1]} overlap",
                                   {"ref": key[0], "x": (fa.x + fb.x) / 2, "y": (fa.y + fb.y) / 2},
                                   key=f"pcb.placement:cy:{key[0]}:{key[1]}"))
    sides = {fp.side for fp in b.fp_list if fp.tht}
    if len(sides) > 1:
        out.append(Finding("pcb.placement", "info", "through-hole parts on both sides (hand soldering from both sides)",
                           key="pcb.placement:thtboth"))
    bottom = [fp.ref for fp in b.fp_list if fp.side == "B" and fp.pads and not fp.tht and fp.in_pos and not fp.dnp]
    if bottom and ctx.setting("fab.assembly", True):
        out.append(Finding("pcb.placement", "info", f"{len(bottom)} SMD parts on the bottom (two-sided assembly): "
                           + ", ".join(bottom[:10]), key="pcb.placement:bottom"))
    return out


def _polys_overlap(a, b):
    """Interiors intersect (touching edges do not count)."""
    na, nb = len(a), len(b)
    for i in range(na):
        p, q = a[i], a[(i + 1) % na]
        for j in range(nb):
            r, s = b[j], b[(j + 1) % nb]
            if _proper_cross(p, q, r, s):
                return True
    ca = (sum(x for x, _ in a) / na, sum(y for _, y in a) / na)
    cb = (sum(x for x, _ in b) / nb, sum(y for _, y in b) / nb)
    return geom.inside(ca, b) or geom.inside(cb, a)


def _proper_cross(a, b, c, d):
    def o(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


@check("pcb.polarity", "Polarized parts carry a polarity mark", "Placement", needs=("pcb",))
def pcb_polarity(ctx):
    """Diodes, LEDs, cells and polarized capacitors need a silk mark (bar, notch, '+') that sits
    nearer one pad; a symmetric silk outline cannot tell an assembler which way round."""
    b = ctx.board
    out = []
    for fp in b.fp_list:
        if not _is_polarized(fp) or len(fp.pads) < 2 or fp.dnp:
            continue
        silk = "F.SilkS" if fp.side == "F" else "B.SilkS"
        pts = []
        for s in fp.shapes:
            if s.layer in (silk, silk.replace("SilkS", "Silkscreen")):
                pts += s.polyline()
        plus = any(t.text.strip() in ("+", "-") and t.layer.endswith(("SilkS", "Silkscreen")) for t in fp.texts)
        p1 = fp.pad("1")
        others = [p for p in fp.pads if p.num != "1"]
        if not p1 or not others:
            continue
        if plus:
            continue
        if not pts:
            out.append(Finding("pcb.polarity", "warning", f"{fp.ref}: no silk at all, so no polarity mark",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"pcb.polarity:none:{fp.ref}"))
            continue
        # asymmetry of the silk about the midpoint between pad 1 and the next pad, along their axis
        p2 = others[0]
        ax, ay = p2.x - p1.x, p2.y - p1.y
        L = math.hypot(ax, ay) or 1.0
        ux, uy = ax / L, ay / L
        mx, my = (p1.x + p2.x) / 2, (p1.y + p2.y) / 2
        proj = [((x - mx) * ux + (y - my) * uy) for x, y in pts]
        lo, hi = min(proj), max(proj)
        near1 = sum(1 for v in proj if v < -0.15 * L)
        near2 = sum(1 for v in proj if v > 0.15 * L)
        if abs(lo + hi) < 0.08 and abs(near1 - near2) <= max(1, 0.1 * len(proj)):
            out.append(Finding("pcb.polarity", "warning", f"{fp.ref}: silk is symmetric; no visible polarity mark",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y},
                               hint="Use a footprint with a cathode bar / '+' mark, or add one on the silkscreen.",
                               key=f"pcb.polarity:sym:{fp.ref}"))
    return out


@check("pcb.silk", "Silkscreen readable and on the right layer", "Placement", needs=("pcb",))
def pcb_silk(ctx):
    """Silk text below the fab's minimum height / line width is illegible; a reference drawn on copper
    or mask layers is a mistake. Limits come from the fab profile (JLC: 1.0 mm / 0.15 mm)."""
    b = ctx.board
    fab = ctx.fab
    hmin, tmin = fab["silk_text_height"], fab["silk_line_width"]
    out, small = [], []
    for fp in b.fp_list:
        for t in fp.texts:
            if t.hidden or not t.layer.endswith(("SilkS", "Silkscreen")) or not t.text.strip():
                continue
            if t.h < hmin - 1e-3 or t.thickness < tmin - 1e-3:
                small.append((fp.ref, t))
    for t in b.texts:
        if t.layer.endswith(("SilkS", "Silkscreen")) and not t.hidden and (t.h < hmin - 1e-3 or t.thickness < tmin - 1e-3):
            small.append(("board", t))
    if small:
        refs = sorted({r for r, _ in small if r != "board"})
        sizes = sorted({(round(t.h, 2), round(t.thickness, 2)) for _, t in small})
        out.append(Finding("pcb.silk", "warning",
                           f"{len(small)} silk texts are below the fab's {hmin:.2f} mm height / {tmin:.2f} mm line "
                           f"(sizes {', '.join(f'{h}/{w}' for h, w in sizes[:4])}): " + ", ".join(refs[:14])
                           + (" ..." if len(refs) > 14 else ""),
                           {"ref": refs[0] if refs else "", "x": small[0][1].x, "y": small[0][1].y},
                           hint="They may print blurred; enlarge them or accept it (waive this finding).",
                           key=f"pcb.silk:small:{len(small)}"))
    return out


# --------------------------------------------------------------------------- module antennas
import re as _re

ANT_MODULE = _re.compile(r"ESP32[-_ ]?(S2|S3|C2|C3|C5|C6|C61|H2)?[-_ ]?(WROOM|MINI|WROVER|SOLO)|ESP[-_]?12|ESP[-_]?WROOM|ESP8266|"
                         r"NINA[-_]?W10[26]|NINA[-_]?B1\d2|MDBT4\dQ|MDBT50Q|BT832|E73[-_]2G4|WFM200|ATWINC15|ATSAMW25|"
                         r"ISP1807|RAK4630", _re.I)
EXT_ANT = _re.compile(r"(WROOM|MINI|WROVER)[-_ ]?\d?[-_ ]?U\b|[-_](\d)?U(\b|_)|IPEX|U\.?FL|WROVER[-_]?I?E\b|[-_]IE\b|W101|W106|B1\d1", _re.I)


def _to_local(fp, pts):
    return [geom.rot(x - fp.x, y - fp.y, -fp.angle) for x, y in pts]


def _to_board(fp, pts):
    return [(fp.x + geom.rot(x, y, fp.angle)[0], fp.y + geom.rot(x, y, fp.angle)[1]) for x, y in pts]


def antenna_region(fp):
    """(polygon in board mm, from where) for a module's antenna: its own keepout rule area, else the
    end of the module outline beyond its pads (at least 3 mm), else None."""
    for z in fp.zones:
        if z.is_rule_area and z.outline:
            return z.outline[0], "the footprint's keepout"
    outline = [q for s in fp.shapes if s.layer in ("F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd") for q in s.polyline()]
    pads = [q for p in fp.pads for q in p.poly]
    if len(outline) < 3 or not pads:
        return None, None
    ol, pl = _to_local(fp, outline), _to_local(fp, pads)
    fab = [q for s in fp.shapes if s.layer in ("F.Fab", "B.Fab") for q in s.polyline()]
    ox0, oy0, ox1, oy1 = geom.bbox(_to_local(fp, fab) if len(fab) >= 3 else ol)
    px0, py0, px1, py1 = geom.bbox(pl)
    gaps = {"top": py0 - oy0, "bottom": oy1 - py1, "left": px0 - ox0, "right": ox1 - px1}
    side, gap = max(gaps.items(), key=lambda kv: kv[1])
    if gap < 3.0:
        return None, None
    rect = {"top": (ox0, oy0, ox1, py0 - 0.5), "bottom": (ox0, py1 + 0.5, ox1, oy1),
            "left": (ox0, oy0, px0 - 0.5, oy1), "right": (px1 + 0.5, oy0, ox1, oy1)}[side]
    x0, y0, x1, y1 = rect
    return _to_board(fp, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), "the module outline beyond its pads"


@check("pcb.antenna", "Nothing under a module's antenna", "Placement", needs=("pcb",))
def pcb_antenna(ctx):
    """A radio module with a printed antenna (ESP32 WROOM / MINI, ESP8266, NINA-W102, MDBT...) needs
    no copper under or beside its antenna on any layer, and the antenna at the board edge (or over
    it): a ground pour under it detunes it and cuts the range to a fraction. The antenna area is
    the footprint's keepout, else the end of the module beyond its pads. Copper found there (pours,
    tracks, vias, other parts' pads) on any layer is an error; an antenna well inside the board a
    warning (Espressif: at the edge, or 15 mm clear all round)."""
    b = ctx.board
    mods = []
    for fp in b.fp_list:
        txt = " ".join([fp.lib_id, fp.value] + [str(v) for v in fp.fields.values()])
        if ANT_MODULE.search(txt) and not EXT_ANT.search(fp.lib_id + " " + fp.value):
            mods.append(fp)
    if not mods:
        raise NotApplicable("no radio module with a printed antenna")
    out = []
    ol = b.outline[0] if b.outline else None
    for fp in mods:
        region, src = antenna_region(fp)
        if not region:
            out.append(Finding("pcb.antenna", "warning", f"{fp.ref} ({fp.value}): cannot tell where its antenna is (no keepout "
                               "in the footprint, no outline beyond the pads): check by eye that nothing is under it",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"ant:unknown:{fp.ref}"))
            continue
        box = geom.bbox(region)
        pts = [(box[0] + (i + 0.5) * 0.5, box[1] + (j + 0.5) * 0.5)
               for i in range(max(1, int((box[2] - box[0]) / 0.5))) for j in range(max(1, int((box[3] - box[1]) / 0.5)))]
        pts = [q for q in pts if geom.inside(q, region) and (ol is None or geom.inside(q, ol))]
        hits = collections.defaultdict(int)
        for z in b.zones:
            if z.is_rule_area:
                continue
            for layer, polys in z.fills.items():
                for pl in polys:
                    pb = geom.bbox(pl)
                    if not geom.bbox_overlap(pb, box):
                        continue
                    n = sum(1 for q in pts if pb[0] <= q[0] <= pb[2] and pb[1] <= q[1] <= pb[3] and geom.inside(q, pl))
                    if n:
                        hits[(f"{z.net or 'unconnected'} pour", layer)] += n
        for t in b.tracks:
            if geom.bbox_overlap(geom.bbox([t.a, t.b]), box) and any(geom.seg_point_dist(q, t.a, t.b) < t.w / 2 for q in pts):
                hits[(f"{t.net or 'no-net'} track", t.layer)] += 1
        for v in b.vias:
            if box[0] <= v.x <= box[2] and box[1] <= v.y <= box[3] and geom.inside((v.x, v.y), region):
                hits[(f"{v.net} via", "vias")] += 1
        for other in b.fp_list:
            if other is fp:
                continue
            for p in other.pads:
                if box[0] <= p.x <= box[2] and box[1] <= p.y <= box[3] and geom.inside((p.x, p.y), region):
                    hits[(f"{other.ref}'s pads", "/".join(sorted({l for l in p.layers if l.endswith('.Cu')})[:2]))] += 1
        if hits:
            (what, layer), n = max(hits.items(), key=lambda kv: kv[1])
            extra = f" and {len(hits) - 1} more" if len(hits) > 1 else ""
            out.append(Finding("pcb.antenna", "error", f"{fp.ref}'s antenna has copper under it: {what} on {layer}{extra}",
                               {"ref": fp.ref, "layer": layer if layer.endswith(".Cu") else None,
                                "x": (box[0] + box[2]) / 2, "y": (box[1] + box[3]) / 2},
                               hint="Keep every layer clear under the antenna (a keepout rule area on all copper layers, "
                                    "no pours, tracks or parts), as the module's data sheet draws it.",
                               key=f"ant:copper:{fp.ref}"))
        if ol is not None:
            inside = [q for q in region if geom.inside(q, ol)]
            if len(inside) == len(region):
                edge = min(geom.poly_dist(q, ol) for q in region)
                if edge > 1.0:
                    out.append(Finding("pcb.antenna", "warning", f"{fp.ref}'s antenna is {edge:.1f} mm inside the board edge",
                                       {"ref": fp.ref, "x": fp.x, "y": fp.y},
                                       hint="Put the antenna end of the module at the board edge (or over it), or keep 15 mm "
                                            "clear of copper around it.", key=f"ant:edge:{fp.ref}"))
    return out
