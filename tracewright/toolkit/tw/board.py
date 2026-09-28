"""Read a .kicad_pcb (KiCad 7 - 10) into plain geometry, in board millimetres (y down).

No KiCad needed: the file is parsed directly, and footprint-local geometry is transformed to board
coordinates. Pads carry a copper polygon, so checks and the viewer never re-derive pad shapes.

    b = Board.load("x.kicad_pcb")
    b.footprints["U1"].pads[0].poly      # [(x, y), ...] in board mm
    b.outline                            # closed loops from Edge.Cuts, largest first
"""
import math, os
from .sexp import parse, find, findall, value, flag, Q
from . import geom

COPPER_ORDER_HINT = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, 31)] + ["B.Cu"]


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _xy(node, key):
    c = find(node, key)
    return (_f(c[1]), _f(c[2])) if c else None


class Pad:
    __slots__ = ("ref", "num", "net", "kind", "shape", "x", "y", "angle", "w", "h", "layers", "drill",
                 "drill_w", "drill_h", "rr", "poly", "polys", "pinfunction", "pintype", "uuid")

    def to_json(self):
        return {"n": self.num, "net": self.net, "k": self.kind, "s": self.shape, "x": round(self.x, 4),
                "y": round(self.y, 4), "a": round(self.angle, 3), "w": round(self.w, 4), "h": round(self.h, 4),
                "l": self.layers, "d": [round(self.drill_w, 4), round(self.drill_h, 4)] if self.drill else None,
                "p": [[[round(x, 4), round(y, 4)] for x, y in pl] for pl in self.polys],
                "fn": self.pinfunction or None, "pt": self.pintype or None}


class Shape:
    """kind: line | arc | circle | rect | poly | curve. pts in board mm; circle: pts=[centre], r."""
    __slots__ = ("kind", "layer", "width", "fill", "pts", "r", "owner")

    def __init__(self, kind, layer, width, fill, pts, r=0.0, owner=None):
        self.kind, self.layer, self.width, self.fill, self.pts, self.r, self.owner = kind, layer, width, fill, pts, r, owner

    def polyline(self):
        """The drawn outline as a list of points (arcs and circles discretised)."""
        if self.kind == "arc":
            return geom.arc_points(*self.pts)
        if self.kind == "circle":
            c = self.pts[0]
            p = geom.circle_poly(c[0], c[1], self.r, 36)
            return p + [p[0]]
        if self.kind in ("rect", "poly"):
            return list(self.pts) + [self.pts[0]]
        return list(self.pts)

    def to_json(self):
        d = {"t": self.kind, "l": self.layer, "w": round(self.width, 4)}
        if self.kind == "circle":
            d["c"] = [round(self.pts[0][0], 4), round(self.pts[0][1], 4)]
            d["r"] = round(self.r, 4)
        elif self.kind == "arc":
            d["p"] = [[round(x, 4), round(y, 4)] for x, y in geom.arc_points(*self.pts)]
        else:
            d["p"] = [[round(x, 4), round(y, 4)] for x, y in self.pts]
        if self.fill:
            d["f"] = 1
        return d


class Text:
    __slots__ = ("text", "x", "y", "angle", "h", "w", "thickness", "layer", "justify", "hidden", "kind", "owner",
                 "mirror", "bold", "italic")

    def to_json(self):
        return {"s": self.text, "x": round(self.x, 4), "y": round(self.y, 4), "a": round(self.angle, 2),
                "h": round(self.h, 3), "w": round(self.w, 3), "th": round(self.thickness, 3), "l": self.layer,
                "j": self.justify, "k": self.kind, "m": 1 if self.mirror else 0}


class Footprint:
    def __init__(self):
        self.ref = ""
        self.value = ""
        self.lib_id = ""
        self.x = self.y = self.angle = 0.0
        self.side = "F"
        self.locked = False
        self.attrs = set()
        self.fields = {}
        self.pads = []
        self.shapes = []
        self.texts = []
        self.zones = []
        self.models = []
        self.uuid = ""
        self.path = ""
        self.sheetname = ""
        self.sheetfile = ""
        self.description = ""

    @property
    def dnp(self):
        return "dnp" in self.attrs

    @property
    def in_bom(self):
        return "exclude_from_bom" not in self.attrs

    @property
    def in_pos(self):
        return "exclude_from_pos_files" not in self.attrs and "board_only" not in self.attrs

    @property
    def tht(self):
        return "through_hole" in self.attrs

    def courtyard(self):
        """Courtyard outlines (lists of points) on the footprint's side."""
        lay = "F.CrtYd" if self.side == "F" else "B.CrtYd"
        shapes = [s for s in self.shapes if s.layer == lay]
        loops = [s.polyline()[:-1] for s in shapes if s.kind in ("rect", "poly", "circle")]
        segs = [s.polyline() for s in shapes if s.kind in ("line", "arc")]
        if segs:
            closed, _ = geom.chain_loops(segs, tol=0.05)
            loops += closed
        return [l for l in loops if len(l) >= 3]

    def bbox(self, layers=None):
        """Box of pads + graphics (+ courtyard) in board mm."""
        pts = []
        for p in self.pads:
            for pl in p.polys:
                pts += pl
        for s in self.shapes:
            if layers and s.layer not in layers:
                continue
            if s.layer.endswith(("Fab", "CrtYd", "SilkS", "Silkscreen")) or layers:
                pts += s.polyline()
        if not pts:
            return (self.x - 0.5, self.y - 0.5, self.x + 0.5, self.y + 0.5)
        return geom.bbox(pts)

    def pad(self, num):
        for p in self.pads:
            if p.num == str(num):
                return p
        return None

    def to_json(self):
        cy = self.courtyard()
        return {"ref": self.ref, "val": self.value, "lib": self.lib_id, "x": round(self.x, 4), "y": round(self.y, 4),
                "a": round(self.angle, 3), "side": self.side, "locked": self.locked, "dnp": self.dnp,
                "bbox": [round(v, 3) for v in self.bbox()],
                "cy": [[[round(x, 3), round(y, 3)] for x, y in l] for l in cy],
                "pads": [p.to_json() for p in self.pads],
                "g": [s.to_json() for s in self.shapes if not s.layer.endswith("CrtYd")],
                "t": [t.to_json() for t in self.texts if not t.hidden],
                "fields": {k: v for k, v in self.fields.items() if v and k not in ("Reference", "Value")},
                "path": self.path, "sheet": self.sheetname}


class Track:
    __slots__ = ("a", "b", "mid", "w", "layer", "net", "uuid", "locked")

    def length(self):
        if self.mid:
            pts = geom.arc_points(self.a, self.mid, self.b)
            return sum(geom.dist(p, q) for p, q in zip(pts, pts[1:]))
        return geom.dist(self.a, self.b)


class Via:
    __slots__ = ("x", "y", "d", "drill", "layers", "net", "kind", "uuid", "locked")


class Zone:
    def __init__(self):
        self.net = ""
        self.layers = []
        self.name = ""
        self.priority = 0
        self.outline = []            # [[pts], ...] (first = outline, KiCad allows several)
        self.fills = {}              # layer -> [[pts], ...] (fractured: holes joined by slits)
        self.keepout = None          # dict of not_allowed flags for rule areas
        self.min_thickness = 0.0
        self.clearance = 0.0
        self.connect = ""
        self.uuid = ""
        self.owner = None

    @property
    def is_rule_area(self):
        return self.keepout is not None


class Board:
    def __init__(self):
        self.path = None
        self.version = 0
        self.layers = []             # [(ordinal, name, type, user_name)]
        self.copper = []             # copper layer names, front to back
        self.nets = set()
        self.footprints = {}         # ref -> Footprint (duplicates get a suffix #2 ...)
        self.fp_list = []
        self.tracks = []
        self.vias = []
        self.zones = []
        self.shapes = []             # board-level graphics
        self.texts = []
        self.thickness = 1.6
        self.title = {}
        self.outline = []            # closed loops (Edge.Cuts), largest first
        self.outline_open = []       # Edge.Cuts pieces that do not close
        self.groups = []
        self.stackup = []            # [{"name", "type", "thickness", "epsilon_r"}] top to bottom (setup/stackup)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        b = cls.parse(text)
        b.path = path
        return b

    @classmethod
    def parse(cls, text):
        t = parse(text)
        if not t or t[0] != "kicad_pcb":
            raise ValueError("not a KiCad board file")
        b = cls()
        b.version = int(value(t, "version", 0) or 0)
        netcodes = {}
        lay = find(t, "layers")
        if lay:
            for l in lay[1:]:
                if isinstance(l, list) and len(l) >= 3:
                    b.layers.append((l[0], str(l[1]), str(l[2]), str(l[3]) if len(l) > 3 else ""))
        cu = [name for _, name, typ, _ in b.layers if name.endswith(".Cu")]
        b.copper = sorted(cu, key=lambda n: COPPER_ORDER_HINT.index(n) if n in COPPER_ORDER_HINT else 99)
        gen = find(t, "general")
        if gen:
            b.thickness = _f(value(gen, "thickness", 1.6), 1.6)
        setup = find(t, "setup")
        su = find(setup, "stackup") if setup else None
        for l in (findall(su, "layer") if su else []):
            if len(l) < 2:
                continue
            b.stackup.append({"name": str(l[1]), "type": str(value(l, "type", "") or ""),
                              "thickness": _f(value(l, "thickness", 0.0)), "epsilon_r": _f(value(l, "epsilon_r", 0.0))})
        tb = find(t, "title_block")
        if tb:
            for c in tb[1:]:
                if isinstance(c, list) and len(c) >= 2:
                    key = c[0] if c[0] != "comment" else f"comment{c[1]}"
                    b.title[key] = str(c[-1])
        for c in t[1:]:
            if not isinstance(c, list) or not c:
                continue
            k = c[0]
            if k == "net" and len(c) >= 3:                       # KiCad <= 9 net table
                netcodes[str(c[1])] = str(c[2])
                if str(c[2]):
                    b.nets.add(str(c[2]))
            elif k == "footprint" or k == "module":
                fp = _footprint(c, netcodes, b)
                b.fp_list.append(fp)
            elif k in ("segment", "arc"):
                tr = Track()
                tr.a, tr.b = _xy(c, "start"), _xy(c, "end")
                tr.mid = _xy(c, "mid") if k == "arc" else None
                tr.w = _f(value(c, "width", 0.2))
                tr.layer = str(value(c, "layer", ""))
                tr.net = _net(c, netcodes)
                tr.uuid = str(value(c, "uuid", "") or value(c, "tstamp", ""))
                tr.locked = flag(c, "locked")
                if tr.net:
                    b.nets.add(tr.net)
                b.tracks.append(tr)
            elif k == "via":
                v = Via()
                v.x, v.y = _xy(c, "at")
                v.d = _f(value(c, "size", 0.6))
                v.drill = _f(value(c, "drill", 0.3))
                ls = find(c, "layers")
                v.layers = [str(x) for x in ls[1:]] if ls else ["F.Cu", "B.Cu"]
                v.net = _net(c, netcodes)
                v.kind = "blind" if "blind" in c else ("micro" if "micro" in c else "through")
                v.uuid = str(value(c, "uuid", "") or value(c, "tstamp", ""))
                v.locked = flag(c, "locked")
                if v.net:
                    b.nets.add(v.net)
                b.vias.append(v)
            elif k == "zone":
                b.zones.append(_zone(c, netcodes))
            elif k.startswith("gr_") and k not in ("gr_text", "gr_text_box"):
                s = _shape(c, k[3:], None, 0, 0, 0)
                if s:
                    b.shapes.append(s)
            elif k in ("gr_text", "gr_text_box"):
                tx = _text(c, "gr", 0, 0, 0, None)
                if tx:
                    b.texts.append(tx)
            elif k == "group":
                mem = find(c, "members")
                b.groups.append({"name": str(c[1]) if len(c) > 1 and not isinstance(c[1], list) else "",
                                 "members": [str(x) for x in (mem[1:] if mem else [])]})
        seen = {}
        for fp in b.fp_list:
            key = fp.ref or "?"
            if key in b.footprints:
                seen[key] = seen.get(key, 1) + 1
                key = f"{key}#{seen[key]}"
            b.footprints[key] = fp
        b._outline()
        return b

    def _outline(self):
        pieces, loops = [], []
        for s in self.shapes + [s for fp in self.fp_list for s in fp.shapes]:
            if s.layer != "Edge.Cuts":
                continue
            if s.kind in ("circle", "rect", "poly"):
                pl = s.polyline()[:-1]
                if len(pl) >= 3:
                    loops.append(pl)
            else:
                pieces.append(s.polyline())
        closed, open_ = geom.chain_loops(pieces)
        loops += closed
        loops.sort(key=lambda l: -abs(geom.area(l)))
        self.outline, self.outline_open = loops, open_

    # ------------------------------------------------------------------ queries
    def bbox(self):
        if self.outline:
            return geom.bbox(self.outline[0])
        pts = []
        for fp in self.fp_list:
            b = fp.bbox()
            pts += [(b[0], b[1]), (b[2], b[3])]
        for t in self.tracks:
            pts += [t.a, t.b]
        return geom.bbox(pts) if pts else (0, 0, 100, 80)

    def size(self):
        x0, y0, x1, y1 = self.bbox()
        return (x1 - x0, y1 - y0)

    def copper_mm(self, layer):
        """The copper thickness the stackup gives a layer (mm), or None when the board has no stackup."""
        for l in self.stackup:
            if l["name"] == layer and l["type"] == "copper" and l["thickness"] > 0:
                return l["thickness"]
        return None

    def dielectric_between(self, a, b):
        """(total thickness mm, epsilon_r) of the dielectric between copper layers a and b (stackup order),
        or None without a stackup."""
        names = [l["name"] for l in self.stackup]
        if a not in names or b not in names:
            return None
        i, j = sorted((names.index(a), names.index(b)))
        mid = [l for l in self.stackup[i + 1:j] if l["type"] not in ("copper",) and l["thickness"] > 0]
        if not mid:
            return None
        t = sum(l["thickness"] for l in mid)
        er = sum(l["thickness"] * (l["epsilon_r"] or 4.5) for l in mid) / t
        return t, er

    def pads(self):
        for fp in self.fp_list:
            for p in fp.pads:
                yield p

    def net_pads(self):
        out = {}
        for p in self.pads():
            if p.net:
                out.setdefault(p.net, []).append(p)
        return out

    def on_board(self, fp, margin=0.0):
        """Is the footprint's origin inside the board outline?"""
        if not self.outline:
            return True
        return geom.inside((fp.x, fp.y), self.outline[0])

    def summary(self):
        w, h = self.size()
        routed = {t.net for t in self.tracks if t.net}
        return {"size_mm": [round(w, 2), round(h, 2)], "copper_layers": len(self.copper), "layers": self.copper,
                "footprints": len(self.fp_list), "pads": sum(len(f.pads) for f in self.fp_list),
                "nets": len(self.nets), "tracks": len(self.tracks), "vias": len(self.vias),
                "zones": len([z for z in self.zones if not z.is_rule_area]),
                "rule_areas": len([z for z in self.zones if z.is_rule_area]),
                "nets_with_tracks": len(routed), "outline_closed": bool(self.outline) and not self.outline_open,
                "thickness": self.thickness,
                "off_board": sorted(f.ref for f in self.fp_list if self.outline and not self.on_board(f))}

    def to_json(self):
        """Compact geometry for the viewer."""
        cu = {n: i for i, n in enumerate(self.copper)}
        zones = []
        for z in self.zones:
            zones.append({"net": z.net, "l": z.layers, "name": z.name, "pri": z.priority, "rule": z.is_rule_area,
                          "ko": z.keepout or None, "o": [[[round(x, 3), round(y, 3)] for x, y in pl] for pl in z.outline],
                          "f": {l: [[[round(x, 3), round(y, 3)] for x, y in pl] for pl in pls] for l, pls in z.fills.items()},
                          "own": z.owner})
        return {
            "version": self.version,
            "bbox": [round(v, 3) for v in self.bbox()],
            "copper": self.copper,
            "layers": [{"n": n, "t": t, "u": u} for _, n, t, u in self.layers],
            "thickness": self.thickness,
            "title": self.title,
            "outline": [[[round(x, 3), round(y, 3)] for x, y in l] for l in self.outline],
            "outline_open": [[[round(x, 3), round(y, 3)] for x, y in l] for l in self.outline_open],
            "footprints": [fp.to_json() for fp in self.fp_list],
            "tracks": [[round(t.a[0], 4), round(t.a[1], 4), round(t.b[0], 4), round(t.b[1], 4), round(t.w, 4),
                        t.layer, t.net] + ([round(t.mid[0], 4), round(t.mid[1], 4)] if t.mid else [])
                       for t in self.tracks],
            "vias": [[round(v.x, 4), round(v.y, 4), round(v.d, 4), round(v.drill, 4), v.net, v.kind, v.layers]
                     for v in self.vias],
            "zones": zones,
            "shapes": [s.to_json() for s in self.shapes],
            "texts": [t.to_json() for t in self.texts if not t.hidden],
            "nets": sorted(self.nets),
        }


# ----------------------------------------------------------------------------- element parsers
def _net(node, netcodes):
    c = find(node, "net")
    if not c or len(c) < 2:
        return ""
    if isinstance(c[1], Q) or len(c) == 2 and not str(c[1]).lstrip("-").isdigit():
        return str(c[1])
    if len(c) >= 3:
        return str(c[2])
    return netcodes.get(str(c[1]), "")


def _layers(node, copper):
    ls = find(node, "layers")
    out = []
    for l in (ls[1:] if ls else []):
        l = str(l)
        if l in ("*.Cu", "F&B.Cu"):
            out += copper if l == "*.Cu" else ["F.Cu", "B.Cu"]
        elif l.startswith("*."):
            out += ["F." + l[2:], "B." + l[2:]]
        else:
            out.append(l)
    return out


def _footprint(c, netcodes, board):
    fp = Footprint()
    fp.lib_id = str(c[1]) if len(c) > 1 and not isinstance(c[1], list) else ""
    fp.side = "B" if str(value(c, "layer", "F.Cu")).startswith("B.") else "F"
    at = find(c, "at")
    if at:
        fp.x, fp.y = _f(at[1]), _f(at[2])
        fp.angle = _f(at[3]) if len(at) > 3 else 0.0
    fp.locked = "locked" in c or flag(c, "locked")
    fp.uuid = str(value(c, "uuid", "") or value(c, "tstamp", ""))
    fp.path = str(value(c, "path", ""))
    fp.sheetname = str(value(c, "sheetname", ""))
    fp.sheetfile = str(value(c, "sheetfile", ""))
    fp.description = str(value(c, "descr", ""))
    attr = find(c, "attr")
    if attr:
        fp.attrs = {str(a) for a in attr[1:] if not isinstance(a, list)}
    if flag(c, "dnp"):
        fp.attrs.add("dnp")
    for key, token in (("exclude_from_bom", "exclude_from_bom"), ("exclude_from_pos_files", "exclude_from_pos_files"),
                       ("board_only", "board_only")):
        if flag(c, key):
            fp.attrs.add(token)
    X, Y, A = fp.x, fp.y, fp.angle
    copper = board.copper or ["F.Cu", "B.Cu"]
    for ch in c[2:]:
        if not isinstance(ch, list) or not ch:
            continue
        k = ch[0]
        if k == "property":
            name, val = str(ch[1]), str(ch[2]) if len(ch) > 2 else ""
            fp.fields[name] = val
            if name == "Reference":
                fp.ref = val
            elif name == "Value":
                fp.value = val
            tx = _text(ch, name.lower() if name in ("Reference", "Value") else "field", X, Y, A, fp.ref)
            if tx and name in ("Reference", "Value"):
                tx.text = val
                fp.texts.append(tx)
            elif tx and not tx.hidden:
                fp.texts.append(tx)
        elif k == "fp_text":
            kind = str(ch[1])
            tx = _text(ch, kind, X, Y, A, fp.ref)
            if tx:
                if kind == "reference":
                    fp.ref = fp.ref or tx.text
                elif kind == "value":
                    fp.value = fp.value or tx.text
                fp.texts.append(tx)
        elif k.startswith("fp_") and k not in ("fp_text_box",):
            s = _shape(ch, k[3:], fp, X, Y, A)
            if s:
                fp.shapes.append(s)
        elif k == "pad":
            fp.pads.append(_pad(ch, fp, netcodes, copper))
        elif k == "zone":
            z = _zone(ch, netcodes)
            z.owner = fp.ref
            fp.zones.append(z)
            board.zones.append(z)
        elif k == "model":
            fp.models.append(str(ch[1]) if len(ch) > 1 else "")
    for t in fp.texts:
        if t.text in ("${REFERENCE}", "%R"):
            t.text = fp.ref
        elif t.text in ("${VALUE}", "%V"):
            t.text = fp.value
    for p in fp.pads:
        p.ref = fp.ref
    for n in (p.net for p in fp.pads):
        if n:
            board.nets.add(n)
    return fp


def _pad(c, fp, netcodes, copper):
    p = Pad()
    p.num = str(c[1])
    p.kind = str(c[2]) if len(c) > 2 else "smd"
    p.shape = str(c[3]) if len(c) > 3 and not isinstance(c[3], list) else "rect"
    at = find(c, "at")
    lx, ly = (_f(at[1]), _f(at[2])) if at else (0.0, 0.0)
    p.angle = _f(at[3]) if at and len(at) > 3 else fp.angle
    dx, dy = geom.rot(lx, ly, fp.angle)
    p.x, p.y = fp.x + dx, fp.y + dy
    sz = find(c, "size")
    p.w, p.h = (_f(sz[1]), _f(sz[2])) if sz else (0.0, 0.0)
    p.layers = _layers(c, copper)
    p.net = _net(c, netcodes)
    p.pinfunction = str(value(c, "pinfunction", "") or "")
    p.pintype = str(value(c, "pintype", "") or "")
    p.uuid = str(value(c, "uuid", "") or "")
    dr = find(c, "drill")
    p.drill = False
    p.drill_w = p.drill_h = 0.0
    if dr and len(dr) > 1:
        vals = [x for x in dr[1:] if not isinstance(x, list)]
        if vals and vals[0] == "oval":
            p.drill_w, p.drill_h = _f(vals[1]), _f(vals[2] if len(vals) > 2 else vals[1])
        elif vals:
            p.drill_w = p.drill_h = _f(vals[0])
        p.drill = p.drill_w > 0
    p.rr = _f(value(c, "roundrect_rratio", 0.25), 0.25)
    cx, cy, a = p.x, p.y, p.angle
    s = p.shape
    if s == "circle":
        poly = geom.circle_poly(cx, cy, p.w / 2, 24)
    elif s == "oval":
        poly = geom.oval_poly(cx, cy, p.w, p.h, a)
    elif s == "roundrect":
        poly = geom.roundrect_poly(cx, cy, p.w, p.h, p.rr * min(p.w, p.h), a)
    elif s == "trapezoid":
        d = find(c, "rect_delta")
        ddx, ddy = (_f(d[1]) / 2, _f(d[2]) / 2) if d else (0.0, 0.0)
        hx, hy = p.w / 2, p.h / 2
        local = [(-hx - ddy, hy + ddx), (hx + ddy, hy - ddx), (hx - ddy, -hy + ddx), (-hx + ddy, -hy - ddx)]
        poly = geom.xform(local, cx, cy, a)
    else:                                            # rect, chamfered rect (drawn square), custom anchor
        poly = geom.rect_poly(cx, cy, p.w, p.h, a)
    p.polys = [poly]
    if s == "custom":
        prim = find(c, "primitives")
        opts = find(c, "options")
        anchor = value(opts, "anchor", "rect") if opts else "rect"
        if anchor == "circle":
            p.polys = [geom.circle_poly(cx, cy, p.w / 2, 16)]
        for g in (prim[1:] if prim else []):
            if not isinstance(g, list):
                continue
            pts = None
            k = g[0]
            wid = _f(value(g, "width", 0.0))
            if k == "gr_poly":
                ptsn = find(g, "pts")
                pts = [(_f(q[1]), _f(q[2])) for q in findall(ptsn, "xy")] if ptsn else None
            elif k == "gr_rect":
                (x0, y0), (x1, y1) = _xy(g, "start"), _xy(g, "end")
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            elif k == "gr_circle":
                cc, e = _xy(g, "center"), _xy(g, "end")
                pts = geom.circle_poly(cc[0], cc[1], geom.dist(cc, e) + wid / 2, 20)
            elif k in ("gr_line", "gr_arc"):
                a0, a1 = _xy(g, "start"), _xy(g, "end")
                mid = _xy(g, "mid")
                line = geom.arc_points(a0, mid, a1) if mid else [a0, a1]
                for q0, q1 in zip(line, line[1:]):
                    L = geom.dist(q0, q1) or 1e-9
                    nx_, ny_ = -(q1[1] - q0[1]) / L * wid / 2, (q1[0] - q0[0]) / L * wid / 2
                    quad = [(q0[0] + nx_, q0[1] + ny_), (q1[0] + nx_, q1[1] + ny_), (q1[0] - nx_, q1[1] - ny_),
                            (q0[0] - nx_, q0[1] - ny_)]
                    p.polys.append(geom.xform(quad, cx, cy, a))
                continue
            if pts:
                p.polys.append(geom.xform(pts, cx, cy, a))
    p.poly = p.polys[0]
    return p


def _stroke_width(node):
    st = find(node, "stroke")
    if st:
        return _f(value(st, "width", 0.0))
    return _f(value(node, "width", 0.0))


def _fill(node):
    f = find(node, "fill")
    if not f or len(f) < 2:
        return False
    v = f[1]
    if isinstance(v, list):                          # (fill (type solid))
        return value(f, "type", "none") not in ("none", "no")
    return str(v) in ("yes", "solid")


def _shape(c, kind, fp, X, Y, A):
    layer = str(value(c, "layer", ""))
    w = _stroke_width(c)
    fill = _fill(c)
    T = (lambda p: (X + geom.rot(p[0], p[1], A)[0], Y + geom.rot(p[0], p[1], A)[1])) if fp else (lambda p: p)
    owner = fp.ref if fp else None
    if kind == "line":
        a, b = _xy(c, "start"), _xy(c, "end")
        if not a or not b:
            return None
        return Shape("line", layer, w, False, [T(a), T(b)], owner=owner)
    if kind == "arc":
        a, m, b = _xy(c, "start"), _xy(c, "mid"), _xy(c, "end")
        if not (a and b):
            return None
        if not m:                                     # KiCad 5 arcs: (start centre) (end start) (angle)
            ang = _f(value(c, "angle", 90))
            cx, cy = a
            sx, sy = b
            r = math.hypot(sx - cx, sy - cy)
            a0 = math.atan2(sy - cy, sx - cx)
            a1 = a0 + math.radians(ang)
            am = (a0 + a1) / 2
            a, m, b = (sx, sy), (cx + r * math.cos(am), cy + r * math.sin(am)), (cx + r * math.cos(a1), cy + r * math.sin(a1))
        return Shape("arc", layer, w, False, [T(a), T(m), T(b)], owner=owner)
    if kind == "circle":
        cc, e = _xy(c, "center"), _xy(c, "end")
        if not cc or not e:
            return None
        return Shape("circle", layer, w, fill, [T(cc)], r=geom.dist(cc, e), owner=owner)
    if kind == "rect":
        a, b = _xy(c, "start"), _xy(c, "end")
        if not a or not b:
            return None
        pts = [(a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1])]
        return Shape("rect", layer, w, fill, [T(p) for p in pts], owner=owner)
    if kind in ("poly", "curve"):
        ptsn = find(c, "pts")
        pts = [(_f(q[1]), _f(q[2])) for q in findall(ptsn, "xy")] if ptsn else []
        if kind == "curve" and len(pts) == 4:
            pts = _bezier(pts)
        if len(pts) < 2:
            return None
        return Shape("poly" if kind == "poly" else "line", layer, w, fill if kind == "poly" else False,
                     [T(p) for p in pts], owner=owner)
    return None


def _bezier(p, n=16):
    out = []
    for k in range(n + 1):
        t = k / n
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        out.append((a * p[0][0] + b * p[1][0] + c * p[2][0] + d * p[3][0],
                    a * p[0][1] + b * p[1][1] + c * p[2][1] + d * p[3][1]))
    return out


def _text(c, kind, X, Y, A, owner):
    tx = Text()
    tx.kind = kind
    tx.owner = owner
    if c[0] == "property":
        tx.text = str(c[2]) if len(c) > 2 else ""
    elif c[0] in ("gr_text", "gr_text_box", "fp_text_box"):
        tx.text = str(c[1]) if len(c) > 1 else ""
    else:
        tx.text = str(c[2]) if len(c) > 2 else ""
    at = find(c, "at")
    lx, ly, ang = (_f(at[1]), _f(at[2]), _f(at[3]) if len(at) > 3 else 0.0) if at else (0.0, 0.0, 0.0)
    if owner is not None or kind in ("reference", "value", "user", "field"):
        dx, dy = geom.rot(lx, ly, A)
        tx.x, tx.y = X + dx, Y + dy
    else:
        tx.x, tx.y = lx, ly
    tx.angle = ang
    tx.layer = str(value(c, "layer", ""))
    tx.hidden = flag(c, "hide")
    eff = find(c, "effects")
    tx.h = tx.w = 1.0
    tx.thickness = 0.15
    tx.justify = ""
    tx.mirror = False
    tx.bold = tx.italic = False
    if eff:
        fnt = find(eff, "font")
        if fnt:
            sz = find(fnt, "size")
            if sz:
                tx.h, tx.w = _f(sz[1], 1.0), _f(sz[2], 1.0)
            tx.thickness = _f(value(fnt, "thickness", tx.h * 0.15), tx.h * 0.15)
            tx.bold = flag(fnt, "bold")
            tx.italic = flag(fnt, "italic")
        j = find(eff, "justify")
        if j:
            tx.justify = " ".join(str(x) for x in j[1:])
            tx.mirror = "mirror" in j
        if flag(eff, "hide"):
            tx.hidden = True
    if tx.text.startswith("${") and not owner and kind == "gr":
        pass
    return tx


def _zone(c, netcodes):
    z = Zone()
    z.net = _net(c, netcodes)
    nn = value(c, "net_name")
    if not z.net and nn:
        z.net = str(nn)
    if find(c, "layers"):
        z.layers = [str(x) for x in find(c, "layers")[1:]]
    elif value(c, "layer"):
        z.layers = [str(value(c, "layer"))]
    z.name = str(value(c, "name", "") or "")
    z.priority = int(_f(value(c, "priority", 0)))
    z.uuid = str(value(c, "uuid", "") or value(c, "tstamp", ""))
    z.min_thickness = _f(value(c, "min_thickness", 0.25))
    cp = find(c, "connect_pads")
    if cp:
        z.connect = " ".join(str(x) for x in cp[1:] if not isinstance(x, list))
        z.clearance = _f(value(cp, "clearance", 0.0))
    ko = find(c, "keepout")
    if ko:
        z.keepout = {str(x[0]): str(x[1]) == "not_allowed" for x in ko[1:] if isinstance(x, list) and len(x) > 1}
    for pg in findall(c, "polygon"):
        pts = find(pg, "pts")
        if pts:
            z.outline.append(_pts(pts))
    for fpg in findall(c, "filled_polygon"):
        layer = str(value(fpg, "layer", z.layers[0] if z.layers else ""))
        pts = find(fpg, "pts")
        if pts:
            z.fills.setdefault(layer, []).append(_pts(pts))
    return z


def _pts(ptsnode):
    out = []
    for q in ptsnode[1:]:
        if isinstance(q, list) and q:
            if q[0] == "xy":
                out.append((_f(q[1]), _f(q[2])))
            elif q[0] == "arc":
                s, m, e = _xy(q, "start"), _xy(q, "mid"), _xy(q, "end")
                if s and m and e:
                    out += geom.arc_points(s, m, e)[:-1] + [e]
    return out


def load(path):
    return Board.load(path)
