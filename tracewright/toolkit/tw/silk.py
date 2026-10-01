"""Silkscreen tidy: put every reference designator where it can be read and does not sit on a pad,
another part's silk or the board edge.

    ops, report = tidy(Board.load(pcb))     # ops for tw.pcb.client.apply ('ref_text')

A reference already clear stays where it is. Otherwise the candidates are around the part's
courtyard (above, below, left, right; horizontal first, then vertical), nearest first; the first
clear one inside the board wins. References that find no clear spot are reported, not hidden.
"""
import math
from . import geom

from . import font


def text_box(cx, cy, text, h, th, rot):
    """The silk a centred reference covers: its strokes as KiCad draws them (tw.font's ink, with this text's pen)."""
    w = font.ink_width(text or "M", h) + th - 0.1524 * h / font.SIZE
    hh = h + th
    if int(round(rot)) % 180 == 90:
        w, hh = hh, w
    return (cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2)


def _seg_hits_box(a, b, box, pad):
    x0, y0, x1, y1 = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    if max(a[0], b[0]) < x0 or min(a[0], b[0]) > x1 or max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1:
        return False
    if x0 <= a[0] <= x1 and y0 <= a[1] <= y1 or x0 <= b[0] <= x1 and y0 <= b[1] <= y1:
        return True
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return any(geom.segments_cross(a, b, corners[i], corners[(i + 1) % 4]) for i in range(4))


class Obstacles:
    def __init__(self, b, side, margin):
        self.m = margin
        self.pads = []
        silk = ("F.SilkS", "F.Silkscreen") if side == "F" else ("B.SilkS", "B.Silkscreen")
        mask = "F.Mask" if side == "F" else "B.Mask"
        for fp in b.fp_list:
            for p in fp.pads:
                if any(l in p.layers for l in (mask, "F.Cu" if side == "F" else "B.Cu")) or p.drill:
                    for pl in p.polys:
                        self.pads.append(geom.bbox(pl))
        self.segs = []
        for s in b.shapes + [s for fp in b.fp_list for s in fp.shapes]:
            if s.layer in silk:
                pts = s.polyline()
                for a, c in zip(pts, pts[1:]):
                    self.segs.append((a, c, s.width / 2, s.owner))
        self.texts = []
        self.outline = b.outline[0] if b.outline else None

    def free(self, box, owner=None):
        m = self.m
        if self.outline:
            inset = [(box[0] - 0.3, box[1] - 0.3), (box[2] + 0.3, box[1] - 0.3), (box[2] + 0.3, box[3] + 0.3),
                     (box[0] - 0.3, box[3] + 0.3)]
            if not all(geom.inside(c, self.outline) for c in inset):
                return False
        for pb in self.pads:
            if geom.bbox_overlap(box, pb, m):
                return False
        for a, c, hw, own in self.segs:
            if _seg_hits_box(a, c, box, m + hw):
                return False
        for tb in self.texts:
            if geom.bbox_overlap(box, tb, m):
                return False
        return True


def tidy(b, margin=0.15, refs=None):
    ops, report = [], {"kept": [], "moved": [], "stuck": []}
    for side in ("F", "B"):
        obs = Obstacles(b, side, margin)
        silk = ("F.SilkS", "F.Silkscreen") if side == "F" else ("B.SilkS", "B.Silkscreen")
        items = []
        for fp in b.fp_list:
            if fp.side != side or (refs and fp.ref not in refs):
                continue
            for t in fp.texts:
                if t.kind == "reference" and t.layer in silk and not t.hidden and t.text:
                    items.append((fp, t))
        # big parts first: they have the most room; small parts then fit around them
        items.sort(key=lambda it: -((it[0].bbox()[2] - it[0].bbox()[0]) * (it[0].bbox()[3] - it[0].bbox()[1])))
        for fp, t in items:
            box = text_box(t.x, t.y, t.text, t.h, t.thickness, t.angle)
            if obs.free(box, fp.ref):
                obs.texts.append(box)
                report["kept"].append(fp.ref)
                continue
            cy_ = fp.courtyard()
            bx = geom.bbox([q for l in cy_ for q in l]) if cy_ else fp.bbox()
            cx, cyc = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
            h, th = t.h, t.thickness
            best = None
            for extra in (0.0, 0.4, 0.9, 1.6):
                for rot in (0, 90):
                    tw = text_box(0, 0, t.text, h, th, rot)
                    w2, h2 = (tw[2] - tw[0]) / 2, (tw[3] - tw[1]) / 2
                    cands = [(cx, bx[1] - h2 - margin - extra), (cx, bx[3] + h2 + margin + extra),
                             (bx[0] - w2 - margin - extra, cyc), (bx[2] + w2 + margin + extra, cyc)]
                    for px, py in cands:
                        box = text_box(px, py, t.text, h, th, rot)
                        if obs.free(box, fp.ref):
                            d = math.hypot(px - t.x, py - t.y) + (0.5 if rot != int(round(t.angle)) % 180 else 0)
                            if best is None or d < best[0]:
                                best = (d, px, py, rot, box)
                if best:
                    break
            if best:
                _, px, py, rot, box = best
                obs.texts.append(box)
                ops.append({"op": "ref_text", "ref": fp.ref, "x": round(px, 3), "y": round(py, 3), "rot": rot})
                report["moved"].append(fp.ref)
            else:
                obs.texts.append(text_box(t.x, t.y, t.text, t.h, t.thickness, t.angle))
                report["stuck"].append(fp.ref)
    return ops, report
