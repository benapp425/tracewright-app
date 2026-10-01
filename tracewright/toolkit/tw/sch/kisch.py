"""KiCad schematic writer (originally a CM5 carrier board's generator, generalised).

Emits KiCad 9 (20250114) S-expressions; `kicad-cli sch upgrade` then rewrites them in the running
KiCad's native format. Coordinates are millimetres on the 1.27 mm grid, screen Y down.

Library symbols (stock copies and generated ICs), symbol instances with rotation, wires, junctions,
no-connects, local and hierarchical labels, text, tinted blocks and zones, and hierarchical sheets.
Every symbol used is written into the project's own symbol library (Project.libname), so the
schematic does not depend on the user's global libraries.
"""
import os, sys, uuid, copy, hashlib, math, datetime
from .. import sexp, env
from .. import font as stroke
from ..sexp import Q, find, findall

GRID = 1.27
LIBNAME = "PROJECT"            # overridden per Project


def symbol_dir():
    d = env.share_dir("symbols")
    if not d:
        raise RuntimeError("KiCad's stock symbol libraries were not found (set TW_KICAD_SHARE)")
    return d


def snap(v):
    return round(round(v / GRID) * GRID, 4)


def uid(seed=None):
    if seed is None:
        return str(uuid.uuid4())
    h = hashlib.sha1(seed.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def font(size=1.27, bold=False, italic=False, color=None, thickness=None):
    f = ["font", ["size", size, size]]
    if thickness:
        f.append(["thickness", thickness])
    if bold:
        f.append(["bold", "yes"])
    if italic:
        f.append(["italic", "yes"])
    if color:
        f.append(["color", *color])
    return f


def effects(size=1.27, justify=None, hide=False, **kw):
    e = ["effects", font(size, **kw)]
    if justify:
        e.append(["justify", *justify.split()])
    if hide:
        e.append(["hide", "yes"])
    return e


# ----------------------------------------------------------------------------- symbols
class LibSymbol:
    """A library symbol (flattened). pins: list of dict(number,name,unit,x,y,rot,type,hidden)."""

    def __init__(self, name, tree):
        self.name = name
        self.tree = tree          # ["symbol", Q(name), ...] with unit subsymbols
        self.pins = []
        for sub in findall(tree, "symbol"):
            parts = str(sub[1]).rsplit("_", 2)
            unit = int(parts[-2])
            for p in findall(sub, "pin"):
                at = find(p, "at")
                hidden = any(x == "hide" for x in p) or bool(find(p, "hide"))
                self.pins.append(dict(number=str(find(p, "number")[1]), name=str(find(p, "name")[1]),
                                      unit=unit, x=float(at[1]), y=float(at[2]),
                                      rot=float(at[3]) if len(at) > 3 else 0.0,
                                      type=p[1], hidden=hidden))
        self.units = sorted({p["unit"] for p in self.pins if p["unit"] > 0}) or [1]

    def bbox(self, unit=1):
        """Bounding box (lib coords, Y up) of body graphics + pins for one unit."""
        xs, ys = [], []
        for sub in findall(self.tree, "symbol"):
            u = int(str(sub[1]).rsplit("_", 2)[-2])
            if u not in (0, unit):
                continue
            for g in sub:
                if not isinstance(g, list):
                    continue
                if g[0] == "rectangle":
                    for k in ("start", "end"):
                        c = find(g, k); xs.append(float(c[1])); ys.append(float(c[2]))
                elif g[0] in ("polyline", "bezier"):
                    for xy in findall(find(g, "pts"), "xy"):
                        xs.append(float(xy[1])); ys.append(float(xy[2]))
                elif g[0] == "circle":
                    c = find(g, "center"); r = float(find(g, "radius")[1])
                    xs += [float(c[1]) - r, float(c[1]) + r]; ys += [float(c[2]) - r, float(c[2]) + r]
                elif g[0] == "arc":
                    for k in ("start", "mid", "end"):
                        c = find(g, k)
                        if c:
                            xs.append(float(c[1])); ys.append(float(c[2]))
                elif g[0] == "pin":
                    at = find(g, "at"); ln = float(find(g, "length")[1]); r = float(at[3]) if len(at) > 3 else 0
                    x, y = float(at[1]), float(at[2])
                    dx, dy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}[int(r) % 360]
                    xs += [x, x + dx * ln]; ys += [y, y + dy * ln]
        if not xs:
            return (-1.27, -1.27, 1.27, 1.27)
        return (min(xs), min(ys), max(xs), max(ys))

    def renamed(self, newname):
        t = copy.deepcopy(self.tree)
        old = str(t[1]).split(":")[-1]
        t[1] = Q(newname)
        for sub in findall(t, "symbol"):
            s = str(sub[1])
            if s.startswith(old + "_"):
                sub[1] = Q(newname.split(":")[-1] + s[len(old):])
        return LibSymbol(newname, t)


def load_stock(libfile, name):
    """Load a KiCad stock symbol, flattening 'extends'."""
    path = libfile if os.path.isabs(libfile) else os.path.join(symbol_dir(), libfile + ".kicad_sym")
    tree = sexp.parse(open(path, encoding="utf-8").read())
    syms = {str(s[1]): s for s in findall(tree, "symbol")}
    s = copy.deepcopy(syms[name])
    ext = find(s, "extends")
    if ext:
        parent = copy.deepcopy(syms[str(ext[1])])
        # child's properties override parent's; graphics/pins come from parent
        props = {str(p[1]): p for p in findall(s, "property")}
        out = [x for x in parent if not (isinstance(x, list) and x and x[0] == "property")]
        out[1] = Q(name)
        pprops = findall(parent, "property")
        merged = []
        for p in pprops:
            merged.append(props.pop(str(p[1]), p))
        merged += list(props.values())
        # insert properties after header atoms
        head = [x for x in out if not isinstance(x, list)]
        body = [x for x in out if isinstance(x, list)]
        nonprop_first = [b for b in body if b[0] in ("pin_numbers", "pin_names", "exclude_from_sim", "in_bom", "on_board", "power")]
        rest = [b for b in body if b not in nonprop_first]
        s = head + nonprop_first + merged + rest
        pname = str(parent[1])
        for sub in findall(s, "symbol"):
            sn = str(sub[1])
            if sn.startswith(pname + "_"):
                sub[1] = Q(name + sn[len(pname):])
    return LibSymbol(name, s)


def _visible(entries):
    return [e for e in entries if e is None or not (len(e) > 3 and e[3] == "hide")]


def _namew(entries):
    """How far the widest pin name of a side reaches from where it starts, as KiCad draws it (mm at 1.27 mm)."""
    n = [stroke.ink(e[1])[1] for e in entries if e]
    return max(n) if n else 0.0


def make_ic(name, units, ref="U", value=None, footprint="", datasheet="", description="",
            pin_len=2.54, power=False, keywords=""):
    """Generate a box symbol whose body is sized from its pin-name text.

    units: list of dict(width=min mm, left=[...], right=[...], top=[...], bottom=[...], title=str)
    each side entry: (number, name, type), None for a gap, or (number, name, type, 'hide') for a
    pin stacked (same position, hidden) on the previous visible pin.
    """
    t = ["symbol", Q(name)]
    if power:
        t.append(["power"])
    t += [["pin_names", ["offset", 1.016]], ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"]]
    geo = []
    for u in units:
        L, Rr = _visible(u.get("left", [])), _visible(u.get("right", []))
        T, B = _visible(u.get("top", [])), _visible(u.get("bottom", []))
        rows = max(len(L), len(Rr), 1)
        text_w = _namew(u.get("left", [])) + _namew(u.get("right", [])) + 2 * 1.016 + 2.54
        w = max(u.get("width", 10.16), text_w, (max(len(T), len(B)) + 1) * 2.54)
        w = math.ceil(w / 2.54) * 2.54
        top_extra = math.ceil(((_namew(u.get("top", [])) + 1.016) if T else 0) / 2.54) * 2.54
        bot_extra = math.ceil(((_namew(u.get("bottom", [])) + 1.016) if B else 0) / 2.54) * 2.54
        title_extra = 2.54 if u.get("title") else 0
        geo.append(dict(w=w, rows=rows, top_extra=top_extra + title_extra, bot_extra=bot_extra,
                        title=u.get("title")))
    g0 = geo[0]
    h0 = (g0["rows"] + 1) * 2.54 + g0["top_extra"] + g0["bot_extra"]
    t.append(["property", Q("Reference"), Q(ref), ["at", -g0["w"] / 2, snap(h0 / 2 + 1.27), 0], effects(justify="left")])
    t.append(["property", Q("Value"), Q(value or name), ["at", -g0["w"] / 2, -snap(h0 / 2 + 1.27), 0], effects(justify="left")])
    t.append(["property", Q("Footprint"), Q(footprint), ["at", 0, 0, 0], effects(hide=True)])
    t.append(["property", Q("Datasheet"), Q(datasheet), ["at", 0, 0, 0], effects(hide=True)])
    t.append(["property", Q("Description"), Q(description), ["at", 0, 0, 0], effects(hide=True)])
    if keywords:
        t.append(["property", Q("ki_keywords"), Q(keywords), ["at", 0, 0, 0], effects(hide=True)])
    short = name.split(":")[-1]
    for ui, u in enumerate(units, start=1):
        g = geo[ui - 1]
        w = g["w"]
        # first pin row on the 2.54 grid; body extends one pitch beyond the outer rows
        y_first = snap(math.floor(((g["rows"] - 1) * 2.54 / 2) / 2.54) * 2.54)
        y_last = y_first - (g["rows"] - 1) * 2.54
        top = y_first + 2.54 + g["top_extra"]
        bot = y_last - 2.54 - g["bot_extra"]
        gfx = ["symbol", Q(f"{short}_{ui}_1")]
        gfx.append(["rectangle", ["start", -w / 2, top], ["end", w / 2, bot],
                    ["stroke", ["width", 0.254], ["type", "default"]], ["fill", ["type", "background"]]])
        if g["title"]:
            gfx.append(["text", Q(g["title"]), ["at", 0, snap(top - 1.905), 0], effects(size=1.27, bold=True)])

        def emit(side, entries):
            if side in ("left", "right"):
                x = -w / 2 - pin_len if side == "left" else w / 2 + pin_len
                rot = 0 if side == "left" else 180
                y, prev = y_first, None
                for e in entries:
                    if e is None:
                        y -= 2.54
                        continue
                    stacked = len(e) > 3 and e[3] == "hide"
                    yy = prev if stacked else y
                    pin = ["pin", e[2], "line", ["at", snap(x), snap(yy), rot], ["length", pin_len]]
                    if stacked:
                        pin.append(["hide", "yes"])
                    pin += [["name", Q(e[1]), effects()], ["number", Q(e[0]), effects()]]
                    gfx.append(pin)
                    if not stacked:
                        prev = y
                        y -= 2.54
            else:
                slots = _visible(entries)
                n = len(slots)
                x0 = snap(-((n - 1) * 2.54) / 2)
                yv = top + pin_len if side == "top" else bot - pin_len
                rot = 270 if side == "top" else 90
                i = -1
                for e in entries:
                    stacked = e is not None and len(e) > 3 and e[3] == "hide"
                    if not stacked:
                        i += 1
                    if e is None:
                        continue
                    pin = ["pin", e[2], "line", ["at", snap(x0 + i * 2.54), snap(yv), rot], ["length", pin_len]]
                    if stacked:
                        pin.append(["hide", "yes"])
                    pin += [["name", Q(e[1]), effects()], ["number", Q(e[0]), effects()]]
                    gfx.append(pin)
        for side in ("left", "right", "top", "bottom"):
            if u.get(side):
                emit(side, u[side])
        t.append(gfx)
    return LibSymbol(name, t)


# ----------------------------------------------------------------------------- geometry
def rot_vec(x, y, ang):
    """Rotate a screen vector (Y down) counter-clockwise (visual) by ang degrees."""
    a = int(round(ang)) % 360
    for _ in range(a // 90):
        x, y = y, -x
    return x, y


class Inst:
    def __init__(self, sch, lib, ref, value, at, rot, unit, fields, uuid_, footprint, dnp, in_bom, on_board, lib_id):
        self.sch, self.lib, self.ref, self.value = sch, lib, ref, value
        self.at, self.rot, self.unit = at, rot, unit
        self.fields, self.uuid, self.footprint = fields, uuid_, footprint
        self.dnp, self.in_bom, self.on_board, self.lib_id = dnp, in_bom, on_board, lib_id

    def pin(self, number):
        cands = [p for p in self.lib.pins if p["number"] == str(number) and p["unit"] in (0, self.unit)]
        if not cands:
            raise KeyError(f"{self.ref}: no pin {number} in unit {self.unit} of {self.lib.name}")
        p = cands[0]
        dx, dy = rot_vec(p["x"], -p["y"], self.rot)
        if getattr(self, "mirror", None) == "y":                 # flipped left to right (unrotated symbols only)
            dx = -dx
        return (snap(self.at[0] + dx), snap(self.at[1] + dy))

    def pin_dir(self, number):
        """Unit vector (screen coords) pointing away from the body at this pin."""
        p = [q for q in self.lib.pins if q["number"] == str(number) and q["unit"] in (0, self.unit)][0]
        tb = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[int(p["rot"]) % 360]   # toward body, screen
        dx, dy = rot_vec(-tb[0], -tb[1], self.rot)
        if getattr(self, "mirror", None) == "y":
            dx = -dx
        return (int(round(dx)), int(round(dy)))

    def bbox(self):
        x0, y0, x1, y1 = self.lib.bbox(self.unit)
        pts = [rot_vec(x, -y, self.rot) for x in (x0, x1) for y in (y0, y1)]
        if getattr(self, "mirror", None) == "y":
            pts = [(-a, b) for a, b in pts]
        xs = [self.at[0] + p[0] for p in pts]; ys = [self.at[1] + p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def field_layout(self):
        """(ref position, ref justify, value position, value justify) as displayed, page mm."""
        x, y = self.at
        is_power = self.ref.startswith("#")
        bx0, by0, bx1, by1 = self.bbox()
        npins = len({p["number"] for p in self.lib.pins if p["unit"] in (0, self.unit)})
        if is_power:
            # value text on the far side of the graphic from the pin, always displayed horizontally
            rpos, rj = (x, y + 3.81), None
            cx, cy = (bx0 + bx1) / 2 - x, (by0 + by1) / 2 - y
            if abs(cy) >= abs(cx):
                vpos, vj = ((x, by1 + 1.27) if cy > 0 else (x, by0 - 1.27)), None
            elif cx < 0:
                vpos, vj = (bx0 - 0.635, y), "right"
            else:
                vpos, vj = (bx1 + 0.635, y), "left"
        small = self.ref.rstrip("0123456789") in ("R", "C", "L", "D", "F", "Y", "FB", "Q", "JP", "TP", "SW", "BT")
        if not is_power and not small:
            npins = 99                                            # connectors/ICs: fields above the body
        if is_power:
            pass
        elif npins <= 2 and (by1 - by0) >= (bx1 - bx0):          # vertical 2-pin part
            yc = (by0 + by1) / 2
            if getattr(self, "fields_left", False):
                rpos, vpos, rj, vj = (bx0 - 0.635, yc - 1.27), (bx0 - 0.635, yc + 1.27), "right", "right"
            else:
                rpos, vpos, rj, vj = (bx1 + 0.635, yc - 1.27), (bx1 + 0.635, yc + 1.27), "left", "left"
        elif npins <= 3 and (bx1 - bx0) > (by1 - by0):           # horizontal small part
            xc = (bx0 + bx1) / 2
            if getattr(self, "fields_above", False):
                rpos, vpos, rj, vj = (xc, by0 - 4.445), (xc, by0 - 1.905), None, None
            elif getattr(self, "fields_below", False):
                rpos, vpos, rj, vj = (xc, by1 + 1.905), (xc, by1 + 4.445), None, None
            else:
                rpos, vpos, rj, vj = (xc, by0 - 1.905), (xc, by1 + 1.905), None, None
        else:                                                     # ICs / connectors: above the body
            rpos, vpos, rj, vj = (bx0, by0 - 4.445), (bx0, by0 - 2.54), "left", "left"
        if self.ref_at:
            rpos, rj = self.ref_at[:2], (self.ref_at[2] if len(self.ref_at) > 2 else rj)
        if self.val_at:
            vpos, vj = self.val_at[:2], (self.val_at[2] if len(self.val_at) > 2 else vj)
        return rpos, rj, vpos, vj

    def field_boxes(self, size=1.27):
        """Page boxes of the visible Reference and Value text (the stroke font's own widths)."""
        rpos, rj, vpos, vj = self.field_layout()
        out = []
        shown = [(self.ref, rpos, rj)] if not self.ref.startswith("#") else []
        if not self.hide_value:
            shown.append((self.value, vpos, vj))
        for s, (px, py), j in shown:
            a, z = stroke.ink(s, size)                  # the strokes of the text, left-justified at px
            w = stroke.width(s, size)
            shift = 0.0 if j == "left" else (-w if j == "right" else -w / 2)
            out.append((px + shift + a, py - size * 0.65, px + shift + z, py + size * 0.65))
        return out

    def pins_by_name(self, name):
        return [p["number"] for p in self.lib.pins if p["name"] == name and p["unit"] in (0, self.unit)]

    P = pin


class Sheet:
    def __init__(self, project, name, filename, title, paper="A3", page="1", rev="A",
                 date=None, company="", comments=()):
        date = date or datetime.date.today().isoformat()
        self.project, self.name, self.filename = project, name, filename
        self.title, self.paper, self.page, self.rev, self.date = title, paper, page, rev, date
        self.company, self.comments = company, comments
        self.uuid = uid(f"sheetfile:{filename}")
        self.items = []
        self.insts = []
        self.subsheets = []     # (Sheet, at, size, pins)
        self.path = None        # set by project: "/rootuuid/sheetuuid"
        self.nets_touch = []

    # ---- drawing primitives
    def wire(self, *pts):
        pts = [(snap(x), snap(y)) for x, y in pts]
        for a, b in zip(pts, pts[1:]):
            if a == b:
                continue
            self.items.append(["wire", ["pts", ["xy", a[0], a[1]], ["xy", b[0], b[1]]],
                               ["stroke", ["width", 0], ["type", "default"]], ["uuid", Q(uid())]])
        return pts[-1]

    def wire_segments(self):
        for it in self.items:
            if it[0] == "wire":
                (_, a0, a1), (_, b0, b1) = it[1][1], it[1][2]
                yield (a0, a1), (b0, b1)

    def is_tee(self, pt):
        """Does a wire pass through pt (not just end there), or do three or more wire ends meet at it?
        Those are the points that need a junction dot."""
        pt = (snap(pt[0]), snap(pt[1]))
        ends = 0
        for a, b in self.wire_segments():
            if pt in (a, b):
                ends += 1
            elif min(a[0], b[0]) - 1e-6 <= pt[0] <= max(a[0], b[0]) + 1e-6 and \
                    min(a[1], b[1]) - 1e-6 <= pt[1] <= max(a[1], b[1]) + 1e-6 and \
                    abs((b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])) < 1e-6:
                return True
        return ends >= 3

    def junction(self, at):
        self.items.append(["junction", ["at", snap(at[0]), snap(at[1])], ["diameter", 0], ["color", 0, 0, 0, 0],
                           ["uuid", Q(uid())]])

    def nc(self, at):
        self.items.append(["no_connect", ["at", snap(at[0]), snap(at[1])], ["uuid", Q(uid())]])

    def label(self, name, at, rot=0, size=1.27):
        just = {0: "left bottom", 90: "left bottom", 180: "right bottom", 270: "right bottom"}[rot % 360]
        self.items.append(["label", Q(name), ["at", snap(at[0]), snap(at[1]), rot], effects(size, justify=just),
                           ["uuid", Q(uid())]])

    def hlabel(self, name, at, rot=0, shape="bidirectional", size=1.27):
        just = {0: "left", 90: "left", 180: "right", 270: "right"}[rot % 360]
        self.items.append(["hierarchical_label", Q(name), ["shape", shape], ["at", snap(at[0]), snap(at[1]), rot],
                           effects(size, justify=just), ["uuid", Q(uid())]])

    def text(self, s, at, size=1.27, bold=False, color=None, justify="left top", italic=False):
        self.items.append(["text", Q(s), ["exclude_from_sim", "no"], ["at", snap(at[0]), snap(at[1]), 0],
                           effects(size, justify=justify, bold=bold, color=color, italic=italic), ["uuid", Q(uid())]])

    def rect(self, x1, y1, x2, y2, width=0, dash="solid", color=None, fill=None, level=0):
        """Rectangle, by default in the theme's colour with no fill (so it prints cleanly in black and
        white). `color` / `fill` (r, g, b, a) override that; KiCad draws fills behind wires and symbols.
        KiCad saves (and so draws) rectangles in UUID order, so the first hex digit carries `level`."""
        f = ["fill", ["type", "color"], ["color", *fill]] if fill else ["fill", ["type", "none"]]
        u = uid()
        self.items.append(["rectangle", ["start", snap(x1), snap(y1)], ["end", snap(x2), snap(y2)],
                           ["stroke", ["width", width], ["type", dash], ["color", *(color or (0, 0, 0, 0))]],
                           f, ["uuid", Q(f"{level:x}{u[1:]}")]])

    def tint(self, x1, y1, x2, y2, fill, color, width=0.1524, dash="dot"):
        """A filled area as an (empty) text box (see block)."""
        x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)
        self.items.append(["text_box", Q(""), ["exclude_from_sim", "no"], ["at", x1, y1, 0],
                           ["size", round(x2 - x1, 4), round(y2 - y1, 4)], ["margins", 0.9525, 0.9525, 0.9525, 0.9525],
                           ["stroke", ["width", width], ["type", dash], ["color", *color]],
                           ["fill", ["type", "color"], ["color", *fill]], effects(1.0, justify="left top"),
                           ["uuid", Q(uid())]])

    def block(self, x1, y1, x2, y2, title, color=None, tcolor=None, fill=None):
        """A functional block: a thin unfilled box in the theme colour with its title centred at the top
        in 2 mm text -- how professional KiCad schematics group a function (e.g. Raspberry Pi's CM5IO).
        `fill` / colours are only for callers that ask for them."""
        if fill:
            self.tint(x1, y1, x2, y2, fill, color or (0, 0, 0, 0), width=0, dash="solid")
        else:
            self.rect(x1, y1, x2, y2, color=color)
        self.text(title, (snap((x1 + x2) / 2), snap(y1 + 3.81)), size=2.0, color=tcolor, justify="bottom")

    # ---- symbols
    def add(self, lib, ref, value, at, rot=0, unit=1, footprint="", fields=None, dnp=False,
            in_bom=None, on_board=None, ref_at=None, val_at=None, hide_value=False,
            fields_left=False, fields_above=False, fields_below=False, mirror=None):
        """in_bom / on_board default to the library symbol's own flags (a mounting hole is not bought)."""
        if in_bom is None:
            in_bom = find(lib.tree, "in_bom") is None or find(lib.tree, "in_bom")[1] != "no"
        if on_board is None:
            on_board = find(lib.tree, "on_board") is None or find(lib.tree, "on_board")[1] != "no"
        lib_id = f"{self.project.libname}:{lib.name.split(':')[-1]}"
        self.project.register(lib)
        inst = Inst(self, lib, ref, value, (snap(at[0]), snap(at[1])), rot, unit, dict(fields or {}),
                    uid(f"sym:{self.filename}:{ref}:{unit}"), footprint, dnp, in_bom, on_board, lib_id)
        inst.ref_at, inst.val_at, inst.hide_value = ref_at, val_at, hide_value
        inst.fields_left, inst.fields_above, inst.fields_below = fields_left, fields_above, fields_below
        inst.mirror = mirror
        self.insts.append(inst)
        return inst

    def place_2pin(self, lib, ref, value, p1, direction="down", **kw):
        """Place a two-pin part so pin 1 sits on p1 and pin 2 lies in `direction` from it."""
        want = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}[direction]
        a = [q for q in lib.pins if q["number"] == "1"][0]
        b = [q for q in lib.pins if q["number"] == "2"][0]
        for rot in (0, 90, 180, 270):
            ax, ay = rot_vec(a["x"], -a["y"], rot)
            bx, by = rot_vec(b["x"], -b["y"], rot)
            d = (bx - ax, by - ay)
            n = math.hypot(*d)
            if abs(d[0] / n - want[0]) < 1e-6 and abs(d[1] / n - want[1]) < 1e-6:
                at = (p1[0] - ax, p1[1] - ay)
                return self.add(lib, ref, value, at, rot=rot, **kw)
        raise ValueError("no rotation matches")

    def place_by_pin(self, lib, ref, value, pin, pos, rot=0, unit=1, **kw):
        q = [p for p in lib.pins if p["number"] == str(pin) and p["unit"] in (0, unit)][0]
        dx, dy = rot_vec(q["x"], -q["y"], rot)
        return self.add(lib, ref, value, (pos[0] - dx, pos[1] - dy), rot=rot, unit=unit, **kw)

    def power(self, lib, at, rot=0, value=None):
        n = self.project.next_pwr()
        pre = "#FLG" if lib.name.split(":")[-1] == "PWR_FLAG" else "#PWR"
        inst = self.add(lib, f"{pre}{n:03d}", value or lib.name.split(":")[-1], at, rot, in_bom=False, on_board=False)
        return inst

    def subsheet(self, sheet, at, size, pins):
        """pins: list of (name, side, offset_mm, shape); side in left/right/top/bottom."""
        self.subsheets.append((sheet, (snap(at[0]), snap(at[1])), (snap(size[0]), snap(size[1])), pins))

    def sheet_pin_pos(self, sheet, name):
        for sh, at, size, pins in self.subsheets:
            if sh is sheet:
                for n, side, off, shape in pins:
                    if n == name:
                        return self._pinpos(at, size, side, off)
        raise KeyError(name)

    @staticmethod
    def _pinpos(at, size, side, off):
        x, y = at
        w, h = size
        if side == "left":
            return (x, snap(y + off))
        if side == "right":
            return (snap(x + w), snap(y + off))
        if side == "top":
            return (snap(x + off), y)
        return (snap(x + off), snap(y + h))

    # ---- output
    def sexpr(self):
        p = self.project
        major = env.kicad().get("major") or 10
        root = ["kicad_sch", ["version", 20260306 if major >= 10 else 20250114], ["generator", Q("eeschema")],
                ["generator_version", Q(f"{major}.0")],
                ["uuid", Q(self.uuid)], ["paper", Q(self.paper)]]
        board = getattr(p, "title", "") or ""
        title = self.title if not board or self.title == board or self.title.startswith(board) else f"{board} - {self.title}"
        tb = ["title_block", ["title", Q(title)], ["date", Q(self.date)], ["rev", Q(self.rev)],
              ["company", Q(self.company)]]
        for i, c in enumerate(self.comments, start=1):
            tb.append(["comment", i, Q(c)])
        root.append(tb)
        libs = ["lib_symbols"]
        used = {}
        for inst in self.insts:
            used[inst.lib_id] = inst.lib
        for lid, lib in sorted(used.items()):
            libs.append(lib.renamed(lid).tree)
        root.append(libs)
        root += self.items
        for inst in self.insts:
            root.append(self._inst_sexpr(inst))
        for sh, at, size, pins in self.subsheets:
            node = ["sheet", ["at", at[0], at[1]], ["size", size[0], size[1]], ["exclude_from_sim", "no"],
                    ["in_bom", "yes"], ["on_board", "yes"], ["dnp", "no"], ["fields_autoplaced", "yes"],
                    ["stroke", ["width", 0], ["type", "solid"]], ["fill", ["color", 0, 0, 0, 0.0]],
                    ["uuid", Q(sh.uuid)],
                    ["property", Q("Sheetname"), Q(sh.name), ["at", at[0], snap(at[1] - 0.7), 0],
                     effects(1.27, justify="left bottom")],
                    ["property", Q("Sheetfile"), Q(sh.filename), ["at", at[0], snap(at[1] + size[1] + 0.6), 0],
                     effects(1.27, justify="left top")]]
            for n, side, off, shape in pins:
                pos = self._pinpos(at, size, side, off)
                rot = {"left": 180, "right": 0, "top": 90, "bottom": 270}[side]
                just = {"left": "left", "right": "right", "top": "right", "bottom": "left"}[side]
                node.append(["pin", Q(n), shape, ["at", pos[0], pos[1], rot], ["uuid", Q(uid(f"spin:{sh.filename}:{n}"))],
                             effects(1.27, justify=just)])
            node.append(["instances", ["project", Q(p.name), ["path", Q(f"/{p.root.uuid}"), ["page", Q(sh.page)]]]])
            root.append(node)
        if self is p.root:
            root.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
        root.append(["embedded_fonts", "no"])
        return root

    def _inst_sexpr(self, inst):
        lib = inst.lib
        x, y = inst.at
        node = ["symbol", ["lib_id", Q(inst.lib_id)], ["at", x, y, inst.rot]] + \
            ([["mirror", "y"]] if getattr(inst, "mirror", None) == "y" else []) + [["unit", inst.unit], ["body_style", 1],
                ["exclude_from_sim", "no"], ["in_bom", "yes" if inst.in_bom else "no"],
                ["on_board", "yes" if inst.on_board else "no"], ["in_pos_files", "yes" if inst.on_board else "no"],
                ["dnp", "yes" if inst.dnp else "no"],
                ["uuid", Q(inst.uuid)]]
        is_power = inst.ref.startswith("#")
        rpos, rj, vpos, vj = inst.field_layout()
        # KiCad rotates field text with the symbol and flips justification for 90/180 degrees
        # (measured, tools/tests: field_orientation.png). Convert "display horizontal, justify J".
        r = int(inst.rot) % 360
        fang = 90 if r in (90, 270) else 0
        flip = r in (90, 180)
        if getattr(inst, "mirror", None) == "y":                   # KiCad mirrors the fields' justification too
            flip = not flip

        def fj(j):
            if j is None or not flip:
                return j
            return {"left": "right", "right": "left"}.get(j, j)
        node.append(["property", Q("Reference"), Q(inst.ref), ["at", round(rpos[0], 3), round(rpos[1], 3), fang],
                     effects(justify=fj(rj), hide=is_power)])
        node.append(["property", Q("Value"), Q(inst.value), ["at", round(vpos[0], 3), round(vpos[1], 3), fang],
                     effects(justify=fj(vj), hide=inst.hide_value)])
        node.append(["property", Q("Footprint"), Q(inst.footprint), ["at", x, y, 0], effects(hide=True)])
        ds = ""
        dp = sexp.prop(lib.tree, "Datasheet")
        if dp:
            ds = str(dp[2])
        node.append(["property", Q("Datasheet"), Q(inst.fields.pop("Datasheet", ds)), ["at", x, y, 0], effects(hide=True)])
        node.append(["property", Q("Description"), Q(inst.fields.pop("Description", "")), ["at", x, y, 0], effects(hide=True)])
        for k, v in inst.fields.items():
            node.append(["property", Q(k), Q(str(v)), ["at", x, y, 0], effects(hide=True)])
        for pn in sorted({p["number"] for p in lib.pins if p["unit"] in (0, inst.unit)}, key=lambda s: (len(s), s)):
            node.append(["pin", Q(pn), ["uuid", Q(uid(f"pin:{inst.uuid}:{pn}"))]])
        node.append(["instances", ["project", Q(self.project.name),
                                   ["path", Q(self.path), ["reference", Q(inst.ref)], ["unit", inst.unit]]]])
        return node

    def write(self, directory):
        with open(os.path.join(directory, self.filename), "w") as f:
            f.write(sexp.dumps(self.sexpr()) + "\n")


class Project:
    def __init__(self, name, libname=None, title=""):
        self.name = name
        self.title = title          # the board's name, put in front of every sheet's title block title
        self.libname = libname or name
        self.root = None
        self.sheets = []
        self.libsyms = {}
        self._pwr = 0

    def next_pwr(self):
        self._pwr += 1
        return self._pwr

    def register(self, lib):
        key = lib.name.split(":")[-1]
        if key in self.libsyms and self.libsyms[key] is not lib:
            if sexp.dumps(self.libsyms[key].tree) != sexp.dumps(lib.tree):
                raise ValueError(f"two different symbols named {key}")
        self.libsyms[key] = lib

    def add_root(self, sheet):
        self.root = sheet
        sheet.path = f"/{sheet.uuid}"
        self.sheets.append(sheet)

    def add_sheet(self, sheet):
        sheet.path = f"/{self.root.uuid}/{sheet.uuid}"
        self.sheets.append(sheet)

    def write_symbol_lib(self, path):
        lib = ["kicad_symbol_lib", ["version", 20241209], ["generator", Q("tracewright")],
               ["generator_version", Q("10.0")]]
        for k in sorted(self.libsyms):
            lib.append(self.libsyms[k].renamed(k).tree)
        with open(path, "w") as f:
            f.write(sexp.dumps(lib) + "\n")
