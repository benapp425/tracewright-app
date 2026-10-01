"""Read a KiCad schematic hierarchy (.kicad_sch, KiCad 7 - 10) into plain data (page mm, y down).

    h = Hierarchy.load("x.kicad_sch")
    for sh in h.sheets:                  # one entry per sheet *instance*, root first
        sh.name_path, sh.file, sh.page
        for s in sh.symbols:             # references resolved for this instance
            s.ref, s.value, s.bbox, s.pins   # pins: [(number, name, type, x, y)] connection points

Symbol geometry follows KiCad's transform: library coordinates (y up) are flipped, rotated by the
instance angle, then mirrored in page space ((mirror x) flips y, (mirror y) flips x).
"""
import os, math
from .sexp import parse, find, findall, value, flag, Q


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _xy(node, key="at"):
    c = find(node, key)
    return (_f(c[1]), _f(c[2])) if c else (0.0, 0.0)


_ROT = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}


def transform(rot, mirror):
    """2x2 matrix (a, b, c, d): page offset = (a*x + b*y, c*x + d*y) for in-memory lib coords (y down)."""
    a, b, c, d = _ROT.get(int(round(rot)) % 360, (1, 0, 0, 1))
    if mirror == "x":
        c, d = -c, -d
    elif mirror == "y":
        a, b = -a, -b
    return a, b, c, d


class LibSym:
    """A library symbol from a schematic's lib_symbols (with 'extends' resolved by KiCad on save)."""

    def __init__(self, node):
        self.name = str(node[1])
        self.node = node
        self.power = flag(node, "power") or bool(find(node, "power"))
        self.pins = []            # dict(number, name, type, unit, style, x, y (lib, y up), angle, length, hidden)
        self.graphics = []        # (unit, style, kind, node)
        for sub in findall(node, "symbol"):
            unit, style = _unit_style(str(sub[1]))
            for g in sub[2:]:
                if not isinstance(g, list):
                    continue
                if g[0] == "pin":
                    at = find(g, "at")
                    nm, nb = find(g, "name"), find(g, "number")
                    self.pins.append(dict(number=str(nb[1]) if nb else "", name=str(nm[1]) if nm else "",
                                          type=str(g[1]) if len(g) > 1 else "", shape=str(g[2]) if len(g) > 2 else "",
                                          unit=unit, style=style, x=_f(at[1]), y=_f(at[2]),
                                          angle=_f(at[3]) if len(at) > 3 else 0.0,
                                          length=_f(value(g, "length", 2.54)),
                                          hidden=flag(g, "hide")))
                elif g[0] in ("rectangle", "polyline", "circle", "arc", "bezier", "text", "text_box"):
                    self.graphics.append((unit, style, g[0], g))
        self.units = sorted({p["unit"] for p in self.pins if p["unit"] > 0}) or [1]

    def unit_pins(self, unit, style=1):
        return [p for p in self.pins if p["unit"] in (0, unit) and p["style"] in (0, style)]

    def local_bbox(self, unit, style=1):
        """Body graphics + pins of one unit, lib coords (y up)."""
        xs, ys = [], []
        for u, st, kind, g in self.graphics:
            if u not in (0, unit) or st not in (0, style):
                continue
            if kind in ("rectangle", "text_box"):
                if kind == "rectangle":
                    a, b = _xy(g, "start"), _xy(g, "end")
                else:
                    a = _xy(g, "at")
                    sz = find(g, "size")
                    b = (a[0] + _f(sz[1]), a[1] + _f(sz[2])) if sz else a
                xs += [a[0], b[0]]
                ys += [a[1], b[1]]
            elif kind in ("polyline", "bezier"):
                pts = find(g, "pts")
                for q in (findall(pts, "xy") if pts else []):
                    xs.append(_f(q[1]))
                    ys.append(_f(q[2]))
            elif kind == "circle":
                c = _xy(g, "center")
                r = _f(value(g, "radius", 0))
                xs += [c[0] - r, c[0] + r]
                ys += [c[1] - r, c[1] + r]
            elif kind == "arc":
                for k in ("start", "mid", "end"):
                    if find(g, k):
                        p = _xy(g, k)
                        xs.append(p[0])
                        ys.append(p[1])
        for p in self.unit_pins(unit, style):
            dx, dy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.get(int(p["angle"]) % 360, (1, 0))
            xs += [p["x"], p["x"] + dx * p["length"]]
            ys += [p["y"], p["y"] + dy * p["length"]]
        if not xs:
            return (-1.27, -1.27, 1.27, 1.27)
        return (min(xs), min(ys), max(xs), max(ys))


def _unit_style(subname):
    parts = subname.rsplit("_", 2)
    try:
        return int(parts[-2]), int(parts[-1])
    except (ValueError, IndexError):
        return 0, 0


class Symbol:
    __slots__ = ("lib_id", "lib", "ref", "value", "unit", "x", "y", "rot", "mirror", "uuid", "fields", "field_at",
                 "in_bom", "on_board", "dnp", "is_power", "bbox", "pins", "footprint", "style", "node")

    def to_json(self):
        return {"ref": self.ref, "val": self.value, "lib": self.lib_id, "unit": self.unit, "x": self.x, "y": self.y,
                "rot": self.rot, "mirror": self.mirror, "uuid": self.uuid, "bbox": [round(v, 3) for v in self.bbox],
                "power": self.is_power, "dnp": self.dnp, "fp": self.footprint,
                "pins": [[n, nm, t, round(x, 3), round(y, 3)] for n, nm, t, x, y in self.pins],
                "fields": {k: v for k, v in self.fields.items() if k not in ("Reference", "Value") and v}}


class SchFile:
    """One parsed .kicad_sch file (shared by every instance of the sheet)."""

    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        self.tree = parse(self.text)
        t = self.tree
        self.uuid = str(value(t, "uuid", ""))
        self.version = int(_f(value(t, "version", 0)))
        pap = find(t, "paper")
        self.paper = str(pap[1]) if pap and len(pap) > 1 else "A4"
        self.portrait = bool(pap and "portrait" in pap[2:])
        self.title = {}
        tb = find(t, "title_block")
        if tb:
            for c in tb[1:]:
                if isinstance(c, list) and len(c) >= 2:
                    self.title[c[0] if c[0] != "comment" else f"comment{c[1]}"] = str(c[-1])
        ls = find(t, "lib_symbols")
        self.libs = {str(s[1]): LibSym(s) for s in (findall(ls, "symbol") if ls else [])}
        self.symbol_nodes = findall(t, "symbol")
        self.wires = [_pts(w) for w in findall(t, "wire")]
        self.buses = [_pts(w) for w in findall(t, "bus")]
        self.bus_entries = []
        for be in findall(t, "bus_entry"):
            at = _xy(be)
            sz = find(be, "size")
            self.bus_entries.append((at, (at[0] + _f(sz[1]), at[1] + _f(sz[2])) if sz else at))
        self.junctions = [_xy(j) for j in findall(t, "junction")]
        self.no_connects = [_xy(n) for n in findall(t, "no_connect")]
        self.labels = []
        for kind in ("label", "global_label", "hierarchical_label", "directive_label", "netclass_flag"):
            for l in findall(t, kind):
                at = find(l, "at")
                lab = {"kind": kind, "text": str(l[1]) if len(l) > 1 and not isinstance(l[1], list) else "",
                       "x": _f(at[1]) if at else 0.0, "y": _f(at[2]) if at else 0.0,
                       "rot": _f(at[3]) if at and len(at) > 3 else 0.0, "shape": str(value(l, "shape", "") or "")}
                lab["box"] = label_box(lab)
                self.labels.append(lab)
        self.texts = []
        for tx in findall(t, "text") + findall(t, "text_box"):
            at = find(tx, "at")
            self.texts.append({"kind": tx[0], "text": str(tx[1]), "x": _f(at[1]) if at else 0.0,
                               "y": _f(at[2]) if at else 0.0})
        self.rects = []
        for r in findall(t, "rectangle"):
            a, b = _xy(r, "start"), _xy(r, "end")
            self.rects.append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
        for tb_ in findall(t, "text_box"):
            a = _xy(tb_)
            sz = find(tb_, "size")
            if sz:
                self.rects.append((a[0], a[1], a[0] + _f(sz[1]), a[1] + _f(sz[2])))
        self.sheet_nodes = findall(t, "sheet")


def _pts(node):
    p = find(node, "pts")
    return [(_f(q[1]), _f(q[2])) for q in (findall(p, "xy") if p else [])]


def label_box(lab, size=1.27):
    """The page area a label covers -- its text at KiCad's own glyph widths, with a global or hierarchical
    label's flag -- reading away from its anchor in the label's direction."""
    from . import font
    flag = lab["kind"] in ("global_label", "hierarchical_label")
    along = font.width(lab["text"] or "M", size) + (size * 2.6 if flag else size * 0.25)
    a0, a1 = (-size * 1.05, size * 1.05) if flag else (-size * 1.45, size * 0.35)     # across: the text sits above a local label's wire
    x, y, r = lab["x"], lab["y"], int(round(lab.get("rot") or 0)) % 360
    if r == 0:
        return [round(x, 3), round(y + a0, 3), round(x + along, 3), round(y + a1, 3)]
    if r == 180:
        return [round(x - along, 3), round(y + a0, 3), round(x, 3), round(y + a1, 3)]
    if not flag:                                          # upright text beside a vertical wire: either side
        a0, a1 = -size * 1.45, size * 1.45
    if r == 90:                                           # reads upwards
        return [round(x + a0, 3), round(y - along, 3), round(x + a1, 3), round(y, 3)]
    return [round(x + a0, 3), round(y, 3), round(x + a1, 3), round(y + along, 3)]


class Sheet:
    """One sheet instance."""

    def __init__(self, sf, path, name_path, name, page, parent=None):
        self.file = sf.path
        self.sf = sf
        self.path = path              # "/root-uuid/sheet-uuid"
        self.name_path = name_path    # "/Power/" ("/" for the root)
        self.name = name
        self.page = page
        self.parent = parent
        self.symbols = []
        self.children = []            # [{name, file, path, at, size, pins: [(name, shape, x, y)]}]

    @property
    def filename(self):
        return os.path.basename(self.file)

    def to_json(self):
        sf = self.sf
        return {"path": self.path, "name_path": self.name_path, "name": self.name, "page": self.page,
                "file": self.filename, "paper": sf.paper, "portrait": sf.portrait, "title": sf.title,
                "symbols": [s.to_json() for s in self.symbols],
                "labels": sf.labels, "children": self.children,
                "wires": [[[round(x, 3), round(y, 3)] for x, y in w] for w in sf.wires],
                "junctions": sf.junctions, "no_connects": sf.no_connects}


class Hierarchy:
    def __init__(self):
        self.root = None
        self.sheets = []
        self.files = {}
        self.project = ""

    @classmethod
    def load(cls, root_path):
        h = cls()
        root_path = os.path.abspath(root_path)
        h.project = os.path.splitext(os.path.basename(root_path))[0]
        rf = h._file(root_path)
        root = Sheet(rf, "/" + rf.uuid, "/", "", "1")
        h.root = root
        h._walk(root, os.path.dirname(root_path), depth=0)
        return h

    def _file(self, path):
        path = os.path.abspath(path)
        if path not in self.files:
            self.files[path] = SchFile(path)
        return self.files[path]

    def _walk(self, sheet, base, depth):
        self.sheets.append(sheet)
        sf = sheet.sf
        sheet.symbols = [self._symbol(n, sf, sheet.path) for n in sf.symbol_nodes]
        if depth > 12:
            return
        for sn in sf.sheet_nodes:
            name, fname = "", ""
            for p in findall(sn, "property"):
                if p[1] in ("Sheetname", "Sheet name"):
                    name = str(p[2])
                elif p[1] in ("Sheetfile", "Sheet file"):
                    fname = str(p[2])
            at, sz = _xy(sn), find(sn, "size")
            size = (_f(sz[1]), _f(sz[2])) if sz else (0.0, 0.0)
            uid = str(value(sn, "uuid", ""))
            pins = []
            for pn in findall(sn, "pin"):
                pa = find(pn, "at")
                pins.append((str(pn[1]), str(pn[2]) if len(pn) > 2 and not isinstance(pn[2], list) else "",
                             _f(pa[1]) if pa else 0.0, _f(pa[2]) if pa else 0.0))
            page = ""
            inst = find(sn, "instances")
            if inst:
                for pr in findall(inst, "project"):
                    for pth in findall(pr, "path"):
                        if str(pth[1]) == sheet.path:
                            page = str(value(pth, "page", ""))
            child_path = sheet.path + "/" + uid
            sheet.children.append({"name": name, "file": fname, "path": child_path, "at": at, "size": size,
                                   "pins": pins, "page": page})
            fpath = os.path.join(base, fname)
            if not fname or not os.path.exists(fpath):
                continue
            child = Sheet(self._file(fpath), child_path, sheet.name_path + name + "/", name, page or str(len(self.sheets) + 1),
                          parent=sheet)
            self._walk(child, os.path.dirname(fpath), depth + 1)

    def _symbol(self, n, sf, path):
        s = Symbol()
        s.node = n
        s.lib_id = str(value(n, "lib_id", ""))
        lname = str(value(n, "lib_name", "") or s.lib_id)
        s.lib = sf.libs.get(lname) or sf.libs.get(s.lib_id)
        at = find(n, "at")
        s.x, s.y = (_f(at[1]), _f(at[2])) if at else (0.0, 0.0)
        s.rot = _f(at[3]) if at and len(at) > 3 else 0.0
        m = find(n, "mirror")
        s.mirror = str(m[1]) if m and len(m) > 1 else ""
        s.unit = int(_f(value(n, "unit", 1), 1))
        s.style = int(_f(value(n, "body_style", value(n, "convert", 1)), 1))
        s.uuid = str(value(n, "uuid", ""))
        s.in_bom = value(n, "in_bom", "yes") != "no"
        s.on_board = value(n, "on_board", "yes") != "no"
        s.dnp = value(n, "dnp", "no") == "yes"
        s.fields, s.field_at = {}, {}
        for p in findall(n, "property"):
            k, v = str(p[1]), str(p[2]) if len(p) > 2 else ""
            s.fields[k] = v
            pa = find(p, "at")
            eff = find(p, "effects")
            hidden = flag(p, "hide") or (eff is not None and flag(eff, "hide"))
            s.field_at[k] = (_f(pa[1]) if pa else s.x, _f(pa[2]) if pa else s.y,
                             _f(pa[3]) if pa and len(pa) > 3 else 0.0, not hidden)
        s.ref = s.fields.get("Reference", "")
        inst = find(n, "instances")
        if inst:
            for pr in findall(inst, "project"):
                for pth in findall(pr, "path"):
                    if str(pth[1]) == path:
                        s.ref = str(value(pth, "reference", s.ref))
                        s.unit = int(_f(value(pth, "unit", s.unit), s.unit))
        s.value = s.fields.get("Value", "")
        s.footprint = s.fields.get("Footprint", "")
        s.is_power = bool(s.lib and s.lib.power) or s.ref.startswith("#")
        tr = transform(s.rot, s.mirror)
        if s.lib:
            x0, y0, x1, y1 = s.lib.local_bbox(s.unit, s.style)
            corners = [_apply(tr, s.x, s.y, x, y) for x in (x0, x1) for y in (y0, y1)]
            s.bbox = (min(c[0] for c in corners), min(c[1] for c in corners),
                      max(c[0] for c in corners), max(c[1] for c in corners))
            s.pins = []
            for p in s.lib.unit_pins(s.unit, s.style):
                px, py = _apply(tr, s.x, s.y, p["x"], p["y"])
                s.pins.append((p["number"], p["name"], p["type"], px, py))
        else:
            s.bbox = (s.x - 1.27, s.y - 1.27, s.x + 1.27, s.y + 1.27)
            s.pins = []
        return s

    # ------------------------------------------------------------------ queries
    def all_symbols(self, include_power=False):
        for sh in self.sheets:
            for s in sh.symbols:
                if include_power or not s.is_power:
                    yield sh, s

    def find_symbol(self, ref):
        for sh, s in self.all_symbols(True):
            if s.ref == ref:
                return sh, s
        return None, None

    def by_file(self, filename):
        return [s for s in self.sheets if s.filename == filename]


def _apply(tr, X, Y, lx, ly):
    """Library point (y up) -> page point."""
    a, b, c, d = tr
    x, y = lx, -ly
    return (X + a * x + b * y, Y + c * x + d * y)


def load(path):
    return Hierarchy.load(path)
