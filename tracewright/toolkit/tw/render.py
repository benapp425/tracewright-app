"""Quick-look images (PNG, Pillow): a board from its file, a schematic sheet from KiCad's plot.

    ./tw render --board [--layers F.Cu,B.Cu] [--region x0,y0,x1,y1] [-o board.png]
    ./tw render --sheet /Power/ [--region ...] [-o sheet.png]

The board image draws what a reviewer looks at: outline, pours (faint), tracks by layer, vias, pads,
silkscreen and reference designators, with optional highlights. It is not a fab plot (use outputs).
"""
import os, math
from .board import Board
from . import geom

LAYER_RGB = {"F.Cu": (200, 52, 52), "B.Cu": (52, 96, 214), "In1.Cu": (200, 160, 40), "In2.Cu": (60, 170, 90),
             "In3.Cu": (170, 90, 200), "In4.Cu": (40, 170, 170), "In5.Cu": (210, 118, 56), "In6.Cu": (110, 186, 216),
             "In7.Cu": (216, 108, 156), "In8.Cu": (140, 190, 80)}
BG = (18, 20, 24)


def _rgb(layer, alpha=255):
    c = LAYER_RGB.get(layer, (150, 150, 150))
    return c + (alpha,)


def board_image(b, layers=None, region=None, px_per_mm=None, max_px=2400, highlight=(), nets=(), notes=()):
    from PIL import Image, ImageDraw, ImageFont
    copper = b.copper or ["F.Cu", "B.Cu"]
    layers = [l for l in (layers or copper) if l in copper] or copper
    x0, y0, x1, y1 = region or b.bbox()
    pad = 1.5
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    if px_per_mm is None:
        px_per_mm = max(4.0, min(40.0, max_px / max(x1 - x0, y1 - y0, 1)))
    W, H = int((x1 - x0) * px_per_mm), int((y1 - y0) * px_per_mm)
    im = Image.new("RGBA", (W, H), BG + (255,))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov, "RGBA")
    T = lambda p: ((p[0] - x0) * px_per_mm, (p[1] - y0) * px_per_mm)
    # board body
    if b.outline:
        dr.polygon([T(p) for p in b.outline[0]], fill=(22, 40, 30, 255))
        for hole in b.outline[1:]:
            dr.polygon([T(p) for p in hole], fill=BG + (255,))
    back_to_front = list(reversed(copper))
    for l in back_to_front:
        if l not in layers:
            continue
        for z in b.zones:
            if z.is_rule_area or l not in z.fills:
                continue
            for pl in z.fills[l]:
                if len(pl) >= 3:
                    dr.polygon([T(p) for p in pl], fill=_rgb(l, 34))
    for l in back_to_front:
        if l not in layers:
            continue
        col = _rgb(l, 230)
        for t in b.tracks:
            if t.layer != l:
                continue
            w = max(1, int(round(t.w * px_per_mm)))
            pts = geom.arc_points(t.a, t.mid, t.b) if t.mid else [t.a, t.b]
            c = (255, 230, 90, 255) if t.net in nets else col
            dr.line([T(p) for p in pts], fill=c, width=w)
            r = w / 2
            for p in (pts[0], pts[-1]):
                q = T(p)
                dr.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=c)
    for fp in b.fp_list:
        hl = fp.ref in highlight
        for p in fp.pads:
            on = [l for l in p.layers if l in layers]
            if not on:
                continue
            c = (255, 230, 90, 255) if (p.net in nets or hl) else (205, 178, 96, 255)
            for pl in p.polys:
                if len(pl) >= 3:
                    dr.polygon([T(q) for q in pl], fill=c)
            if p.drill:
                q = T((p.x, p.y))
                r = min(p.drill_w, p.drill_h) / 2 * px_per_mm
                dr.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=BG + (255,))
    for v in b.vias:
        q = T((v.x, v.y))
        r = v.d / 2 * px_per_mm
        c = (255, 230, 90, 255) if v.net in nets else (190, 190, 190, 255)
        dr.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=c)
        rd = v.drill / 2 * px_per_mm
        dr.ellipse([q[0] - rd, q[1] - rd, q[0] + rd, q[1] + rd], fill=BG + (255,))
    silk = {"F.SilkS", "F.Silkscreen"} if "F.Cu" in layers else set()
    if "B.Cu" in layers and "F.Cu" not in layers:
        silk = {"B.SilkS", "B.Silkscreen"}
    for s in b.shapes + [s for fp in b.fp_list for s in fp.shapes]:
        if s.layer in silk or s.layer == "Edge.Cuts":
            c = (235, 235, 235, 220) if s.layer != "Edge.Cuts" else (240, 220, 60, 255)
            w = max(1, int(round(max(s.width, 0.1) * px_per_mm)))
            pts = s.polyline()
            if len(pts) >= 2:
                dr.line([T(p) for p in pts], fill=c, width=w)
    def font_of(px):
        try:
            return ImageFont.load_default(size=max(9, int(px)))
        except TypeError:
            return ImageFont.load_default()
    font = font_of(min(1.3 * px_per_mm, 40))
    if px_per_mm >= 16:                              # pad numbers on multi-pin parts when zoomed in
        pfont = font_of(min(0.55 * px_per_mm, 22))
        for fp in b.fp_list:
            if len(fp.pads) <= 2:
                continue
            for pd in fp.pads:
                if not any(l in layers for l in pd.layers) or not pd.num:
                    continue
                dr.text(T((pd.x, pd.y)), pd.num, fill=(30, 30, 30, 255), font=pfont, anchor="mm")
    for fp in b.fp_list:
        if not fp.pads:
            continue
        bx = fp.bbox()
        cx, cy = T(((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2))
        c = (255, 240, 120, 255) if fp.ref in highlight else (245, 245, 245, 255)
        dr.text((cx, cy), fp.ref, fill=c, font=font, anchor="mm", stroke_width=max(2, int(font.size / 8)) if hasattr(font, "size") else 2,
                stroke_fill=(0, 0, 0, 255))
        if fp.ref in highlight:
            dr.rectangle([T((bx[0], bx[1])), T((bx[2], bx[3]))], outline=(255, 230, 90, 255), width=3)
    for n in notes:
        q = T((n["x"], n["y"]))
        dr.ellipse([q[0] - 6, q[1] - 6, q[0] + 6, q[1] + 6], outline=(255, 120, 60, 255), width=3)
        if n.get("text"):
            dr.text((q[0] + 9, q[1] - 9), n["text"], fill=(255, 150, 90, 255), font=font)
    im = Image.alpha_composite(im, ov).convert("RGB")
    return im


def board_png(project, out, region=None, layers=None, px_per_mm=None, **kw):
    b = Board.load(project.pcb)
    ls = [l.strip() for l in layers.split(",")] if isinstance(layers, str) else layers
    im = board_image(b, layers=ls, region=region, px_per_mm=px_per_mm, **kw)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    im.save(out)
    return out


def sheet_png(project, sheet, out, region=None, px_per_mm=None):
    """A schematic sheet by name path ('/', '/Power/') or file name, from KiCad's own plot."""
    from . import kicad
    from .schematic import Hierarchy
    from .svg import SvgDoc
    from .checks.context import map_svgs
    d = os.path.join(project.build, "sch_svg")
    files = sorted(os.path.join(d, f) for f in os.listdir(d)) if os.path.isdir(d) else []
    newest = max((os.path.getmtime(f) for f in project.sheets()), default=0)
    if not files or min(os.path.getmtime(f) for f in files) < newest:
        files = kicad.sch_svg(project.sch, d, drawing_sheet=False, theme="_builtin_default")
    h = Hierarchy.load(project.sch)
    m = map_svgs(h, files, SvgDoc.load)
    key = sheet
    if key not in m:
        for np_, (doc, f) in m.items():
            sh = next((s for s in h.sheets if s.name_path == np_), None)
            if sh and (sh.filename == sheet or sh.name == sheet.strip("/")):
                key = np_
                break
    if key not in m:
        raise KeyError(f"no sheet {sheet!r}; sheets: {', '.join(sorted(m))}")
    doc = m[key][0]
    if px_per_mm is None:
        w = (region[2] - region[0]) if region else doc.width
        px_per_mm = max(3.0, min(24.0, 2400 / max(w, 1)))
    im = doc.render(px_per_mm, region=region)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    im.save(out)
    return out
