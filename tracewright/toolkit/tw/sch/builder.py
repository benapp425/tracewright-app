"""Sheet builder: a parts catalog, reference numbering, wiring shorthands, functional blocks, layout
zones, notes next to parts, a cover sheet with contents and general notes -- drawn the way professional
schematics are (see the knowledge base's schematic-conventions lesson): theme colours only, thin
unfilled boxes with titles, 1.27 mm text, filled title blocks, signal flow left to right, supplies up
and ground down.

    from tw.sch import Design, Part, stock
    d = Design("Sensor node")                        # project name = symbol library name
    R, C = stock("Device", "R"), stock("Device", "C")
    CAT = {"R10k": Part(R, "Resistor_SMD:R_0402_1005Metric", "10k", "0402WGF1002TCE", "UNI-ROYAL", "C25744")}
    root = d.root("Sensor node", paper="A4")
    pw = d.sheet("Power", "power.kicad_sch", "Power")
    b = d.builder(pw, 100, CAT)
    b.block(12.7, 12.7, 150, 90, "3.3 V REGULATOR")
    r = b.two("R10k", "R", (50.8, 40.64), "down")
    b.gnd(r.pin("2"))
    ...
    d.write("hardware/node")                          # sheets + symbol library + library tables
"""
import os, math, json
from . import kisch
from .kisch import snap

NOTE = None                  # notes, titles and boxes use the theme's colours: nothing carries meaning
TITLE = None                 # by colour, and the PDF prints cleanly in black and white

# kinds of block, kept for scripts that pass one; they no longer change how a block looks
KIND = {
    "power": "POWER: input, protection, converters, rails",
    "hs": "HIGH SPEED: controlled-impedance pairs",
    "rf": "RF: antennas and keep-outs",
    "sensor": "SENSORS and clocks",
    "control": "CONTROL: processor, boot, debug",
    "io": "I/O: connectors and drivers",
    "mech": "MECHANICAL",
}


class Part:
    """A catalog entry: symbol, footprint (lib:name), value and the ordering fields."""

    def __init__(self, symbol, footprint, value, mpn="", manufacturer="", lcsc="", datasheet="", **fields):
        self.symbol, self.footprint, self.value = symbol, footprint, value
        self.fields = {"MPN": mpn, "Manufacturer": manufacturer, "LCSC": lcsc}
        if datasheet:
            self.fields["Datasheet"] = datasheet
        self.fields.update(fields)


_STOCK = {}


def stock(lib, name):
    """A KiCad stock symbol ('Device', 'R'), cached; copied into the project library on write."""
    key = (lib, name)
    if key not in _STOCK:
        _STOCK[key] = kisch.load_stock(lib, name)
    return _STOCK[key]


_POWER = {}


def power(name):
    """A power symbol for a rail. Stock ones (GND, +3V3, +5V, ...) come from KiCad's power library;
    any other rail name gets a copy of +5V whose value names the net (KiCad's rule for power symbols), made once."""
    try:
        return stock("power", name)
    except KeyError:
        if name not in _POWER:
            s = stock("power", "+5V").renamed(f"PWR_{''.join(c if c.isalnum() else '_' for c in name)}")
            for p in kisch.findall(s.tree, "property"):
                if p[1] == "Value":
                    p[2] = kisch.Q(name)
            _POWER[name] = s
        return _POWER[name]


class Builder:
    """Wiring for one sheet. base: reference numbering start (100 -> R101, C101, ...)."""

    def __init__(self, sheet, base, catalog=None):
        self.sh, self.base, self.n = sheet, base, {}
        self.cat = catalog or {}
        self._gnd = None

    def ref(self, prefix):
        self.n[prefix] = self.n.get(prefix, 0) + 1
        return f"{prefix}{self.base + self.n[prefix]}"

    def _part(self, key, extra):
        p = self.cat[key] if isinstance(key, str) else key
        f = {k: v for k, v in p.fields.items() if v}
        f.update(extra or {})
        return p.symbol, p.footprint, p.value, f

    # ------------------------------------------------------------------ parts
    def two(self, key, prefix, p1, direction="down", value=None, fields=None, ref=None, **kw):
        """A two-pin part with pin 1 on p1 and pin 2 toward `direction`."""
        sym, fp, val, f = self._part(key, fields)
        return self.sh.place_2pin(sym, ref or self.ref(prefix), value or val, p1, direction, footprint=fp, fields=f, **kw)

    def ic(self, key, prefix, pin, pos, rot=0, unit=1, value=None, fields=None, ref=None, **kw):
        """A part placed so `pin` lands on `pos`."""
        sym, fp, val, f = self._part(key, fields)
        return self.sh.place_by_pin(sym, ref or self.ref(prefix), value or val, pin, pos, rot=rot, unit=unit,
                                    footprint=fp, fields=f, **kw)

    # ------------------------------------------------------------------ wiring
    def w(self, *pts):
        return self.sh.wire(*pts)

    def j(self, pt):
        self.sh.junction(pt)

    def gnd(self, pt, drop=0.0, rot=0):
        """A ground symbol at pt, pointing down (rot 90 / 270 point right / left, for connector pins;
        never upside down)."""
        if drop:
            self.w(pt, (pt[0], pt[1] + drop))
            pt = (pt[0], pt[1] + drop)
        self.sh.power(power("GND"), pt, rot=rot)
        return pt

    def rail(self, pt, name, rot=0):
        """A power symbol (net = name) at pt; rot 0 points up (supplies go up, ground down), 90 left,
        270 right -- sideways only at connector pins."""
        self.sh.power(power(name), pt, rot=rot, value=name)
        return pt

    def flag(self, pt):
        self.sh.power(stock("power", "PWR_FLAG"), pt)

    def lab(self, pt, name, side="r"):
        self.sh.label(name, pt, {"r": 0, "u": 90, "l": 180, "d": 270}[side])

    def hl(self, pt, name, side="r", shape="input"):
        self.sh.hlabel(name, pt, {"r": 0, "u": 90, "l": 180, "d": 270}[side], shape)

    def nc(self, inst, pin):
        self.sh.nc(inst.pin(pin))

    def stub(self, inst, pin, length=2.54):
        a = inst.pin(pin)
        d = inst.pin_dir(pin)
        b = (a[0] + d[0] * length, a[1] + d[1] * length)
        self.w(a, b)
        return b

    def pin_lab(self, inst, pin, name, length=2.54, hier=None):
        """A short wire out of a pin ending in a label that reads away from the body."""
        b = self.stub(inst, pin, length)
        d = inst.pin_dir(pin)
        side = {(1, 0): "r", (-1, 0): "l", (0, -1): "u", (0, 1): "d"}[d]
        if hier:
            self.hl(b, name, side, hier)
        else:
            self.lab(b, name, side)
        return b

    def pin_gnd(self, inst, pin, length=2.54):
        b = self.stub(inst, pin, length) if length else inst.pin(pin)
        self.gnd(b)
        return b

    def pin_rail(self, inst, pin, name, length=2.54):
        b = self.stub(inst, pin, length) if length else inst.pin(pin)
        d = inst.pin_dir(pin)
        self.rail(b, name, rot={(0, -1): 0, (0, 1): 180, (1, 0): 270, (-1, 0): 90}[d])
        return b

    # ------------------------------------------------------------------ conventional sub-circuits
    def tee(self, pt):
        """A junction dot at pt if wires meet there as a T (never four ways: split those into two Ts)."""
        if self.sh.is_tee(pt):
            self.j(pt)

    def pin_cap(self, inst, pin, keys, rail, side="left", pitch=7.62, reach=5.08, prefix="C", refs=None):
        """Decoupling drawn the way it is placed: from a power pin, a wire to the rail symbol, and the
        capacitors hanging from it beside the pin, nearest (smallest) first, each to ground.
        A pin pointing up/down gets a branch to `side` ('left'/'right') at `reach` from the pin; a pin
        pointing left/right carries its caps along its own wire. Returns the capacitors."""
        a = inst.pin(pin)
        d = inst.pin_dir(pin)
        caps = []
        if d[0] == 0:                                     # vertical pin: rail straight out, branch aside
            top = (a[0], a[1] + d[1] * (reach + 5.08))
            self.w(a, top)
            self.rail(top, rail, rot=0 if d[1] < 0 else 180)
            yb = a[1] + d[1] * reach
            sx = -1 if side == "left" else 1
            end = (a[0] + sx * pitch * len(keys), yb)
            self.w((a[0], yb), end)
            self.j((a[0], yb))
            for i, key in enumerate(keys):
                x = a[0] + sx * pitch * (i + 1)
                c = self.two(key, prefix, (x, yb), "down", ref=refs[i] if refs else None)
                self.gnd(c.pin("2"))
                if i < len(keys) - 1:
                    self.j((x, yb))
                caps.append(c)
        else:                                             # horizontal pin: caps along its wire, rail at the end
            end = (a[0] + d[0] * (reach + pitch * len(keys)), a[1])
            self.w(a, end)
            for i, key in enumerate(keys):
                x = a[0] + d[0] * (reach + pitch * i)
                c = self.two(key, prefix, (x, a[1]), "down", ref=refs[i] if refs else None)
                self.gnd(c.pin("2"))
                self.j((x, a[1]))
                caps.append(c)
            self.rail(end, rail)
        return caps

    def pull(self, pt, key, rail="+3V3", up=True, prefix="R", **kw):
        """A pull-up (from `rail`, drawn above pt) or pull-down (to GND, below) onto the wire point pt,
        with a junction dot when pt is a T. Returns the resistor."""
        if up:
            r = self.two(key, prefix, (pt[0], pt[1] - 7.62), "down", **kw)
            self.rail(r.pin("1"), rail)
        else:
            r = self.two(key, prefix, pt, "down", **kw)
            self.gnd(r.pin("2"))
        self.tee(pt)
        return r

    def inline(self, inst, pin, key, prefix="R", gap=2.54, **kw):
        """A two-pin part in series with a pin (a series resistor, a ferrite): a short stub, then the
        part along the pin's direction. Returns (part, its far pin position)."""
        a = self.stub(inst, pin, gap)
        d = inst.pin_dir(pin)
        direction = {(1, 0): "right", (-1, 0): "left", (0, -1): "up", (0, 1): "down"}[d]
        part = self.two(key, prefix, a, direction, **kw)
        return part, part.pin("2")

    def decouple(self, x, y_rail, values, pitch=15.24, net=None):
        """A row of capacitors hanging from a rail wire at y_rail, pin 1 up, grounded; returns them."""
        out = []
        for i, key in enumerate(values):
            xi = x + i * pitch
            c = self.two(key, "C", (xi, y_rail), "down")
            self.gnd(c.pin("2"))
            out.append(c)
        if len(out) > 1:
            self.w((x, y_rail), (x + (len(out) - 1) * pitch, y_rail))
            for c in out[1:-1]:
                self.j(c.pin("1"))
        return out

    # ------------------------------------------------------------------ text and structure
    def text(self, s, at, size=1.27, color=NOTE, bold=False):
        self.sh.text(s, at, size=size, color=color, bold=bold)

    def note(self, s, at, dx=0.0, dy=0.0):
        """A short design note beside the part or pin it explains: why the value, the current, the
        layout constraint. 1.27 mm text in the theme's note colour."""
        self.sh.text(s, (at[0] + dx, at[1] + dy), size=1.27, color=NOTE)

    def block(self, x1, y1, x2, y2, title, kind=None):
        """A functional block: thin unfilled box, title centred at the top (2 mm). `kind` is accepted
        for older scripts and ignored."""
        self.sh.block(x1, y1, x2, y2, title)

    def zone(self, parts, kind=None, caption=None, pad=1.27, grow=(0, 0, 0, 0), cap=None):
        """A dashed outline around parts that need care in layout (hot loop, switch node, crystal,
        pairs) with a one-line caption saying what the care is. parts: placed symbols and/or (x, y)
        points; grow: (left, top, right, bottom) mm. The outline includes the parts' field boxes so it
        never cuts their text. `kind` is accepted for older scripts and ignored."""
        xs, ys = [], []
        for q in parts:
            if isinstance(q, tuple):
                xs.append(q[0])
                ys.append(q[1])
            else:
                for x0, y0, x1, y1 in [q.bbox()] + q.field_boxes():
                    xs += [x0, x1]
                    ys += [y0, y1]
        x0, y0 = min(xs) - pad - grow[0], min(ys) - pad - grow[1]
        x1, y1 = max(xs) + pad + grow[2], max(ys) + pad + grow[3]
        self.sh.rect(x0, y0, x1, y1, dash="dash")
        if caption:
            at = cap if cap else (x0 + 0.635, y0 - 0.635)
            self.sh.text(caption, at, size=1.27, justify="left bottom")
        return (x0, y0, x1, y1)

    def legend(self, *args, **kwargs):
        """Nothing: blocks are no longer colour-coded, so there is no key to draw (kept so older
        scripts still run)."""
        return None

    def notes(self, at, lines, title="NOTES"):
        """General notes (the cover sheet's "unless otherwise noted" list), numbered."""
        body = "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))
        self.sh.text(f"{title}\n{body}", at, size=1.27)


class Design:
    """A whole generated schematic: root sheet, sub-sheets, and the write step.

    title: the board's name, put in front of every sheet's title-block title ("Sensor node - Power");
    company / rev / comments fill every title block (a sheet can override them)."""

    def __init__(self, name, libname=None, title="", company="", rev="A", comments=()):
        self.p = kisch.Project(name, libname=libname or "".join(c if c.isalnum() or c in "_-" else "_" for c in name),
                               title=title)
        self._page = 1
        self.tb = {"company": company, "rev": rev, "comments": tuple(comments)}
        self.described = []            # (page, sheet name, what it holds) for the contents list
        self.net_attrs = {}            # net -> what the design says it is (tw.netmodel), written with the sheets

    def net(self, name, **attrs):
        """Say what a net is, for the checks, the router's net classes and the app's net list:
        d.net("VPYRO", kind="power", voltage=8.4, current=5); d.net("USB_D_P", kind="pair", pair="USB_D_N",
        impedance=90); d.net("GPS_RF", kind="rf", impedance=50). Fields: see tw.netmodel.FIELDS."""
        from .. import netmodel
        rec = netmodel._clean({k: v for k, v in attrs.items() if v is not None})
        self.net_attrs.setdefault(name, {}).update(rec)
        return self.net_attrs[name]

    @property
    def libname(self):
        return self.p.libname

    def _tb(self, kw):
        return {**self.tb, **kw}

    def root(self, title, filename=None, paper="A3", **kw):
        fn = filename or f"{self.p.name}.kicad_sch"
        sh = kisch.Sheet(self.p, "", fn, title, paper=paper, page="1", **self._tb(kw))
        self.p.add_root(sh)
        return sh

    def sheet(self, name, filename, title, paper="A3", description="", **kw):
        """A sub-sheet; `description` (what it holds) goes into the cover sheet's contents list."""
        self._page += 1
        sh = kisch.Sheet(self.p, name, filename, title, paper=paper, page=str(self._page), **self._tb(kw))
        self.p.add_sheet(sh)
        self.described.append((self._page, name, description or title))
        return sh

    def contents(self, at, cover="Cover: this page -- board description, contents, general notes"):
        """The cover sheet's contents list: page, sheet, what it holds."""
        rows = [f"1   {cover}"] + [f"{pg}   {name}: {what}" for pg, name, what in self.described]
        self.p.root.text("CONTENTS\n" + "\n".join(rows), at, size=1.27)

    def builder(self, sheet, base, catalog=None):
        return Builder(sheet, base, catalog)

    def write(self, hw_dir, stem=None):
        """Write every sheet, the project symbol library and the library tables into hw_dir. Pages laid
        out by rule (tw.sch.auto) are drawn first, their labels resolved across sheets, and what they
        were asked to connect saved for finish() to check against KiCad's netlist."""
        os.makedirs(os.path.join(hw_dir, "lib"), exist_ok=True)
        pages = [pg for pg in getattr(self, "pages", []) if not getattr(pg, "emitted", False)]
        if pages:
            from . import auto
            glob = auto.resolve(self)
            for pg in pages:
                pg.emit(glob)
                pg.emitted = True
            with open(os.path.join(hw_dir, ".tracewright-nets.json"), "w") as f:
                json.dump({"nets": auto.intended(self), "crowded": [c for pg in self.pages for c in pg.crowded],
                           "notes": [c for pg in self.pages for c in getattr(pg, "note_issues", [])],
                           "attrs": self.net_attrs}, f, indent=1)
            from . import notes as design_notes          # the reasoning behind the sheets, kept beside them
            design_notes.replace_script(hw_dir, [n for pg in self.pages for n in getattr(pg, "design_notes", [])])
            from .. import placeplan                     # parts drawn aside: the layout keeps them at their pin
            placeplan.replace_script(hw_dir, [c for pg in self.pages for c in getattr(pg, "near", [])])
        elif self.net_attrs:                           # hand-placed sheets: only what the nets are
            with open(os.path.join(hw_dir, ".tracewright-nets.json"), "w") as f:
                json.dump({"attrs": self.net_attrs}, f, indent=1)
        _no_duplicate_refs(self.p.sheets)
        for sh in self.p.sheets:
            sh.write(hw_dir)
        self.p.write_symbol_lib(os.path.join(hw_dir, "lib", f"{self.libname}.kicad_sym"))
        _ensure_lib(os.path.join(hw_dir, "sym-lib-table"), "sym_lib_table", self.libname,
                    f"${{KIPRJMOD}}/lib/{self.libname}.kicad_sym")
        _ensure_lib(os.path.join(hw_dir, "fp-lib-table"), "fp_lib_table", self.libname,
                    f"${{KIPRJMOD}}/lib/{self.libname}.pretty")
        os.makedirs(os.path.join(hw_dir, "lib", f"{self.libname}.pretty"), exist_ok=True)
        return [os.path.join(hw_dir, sh.filename) for sh in self.p.sheets]


def _no_duplicate_refs(sheets):
    """Two parts under one reference are one part to KiCad: their pins' nets join (an LED numbered D101 beside a
    diode named D101 joined VSYS to GND). One part's units may share it, each unit once."""
    seen = {}
    for sh in sheets:
        for inst in sh.insts:
            if inst.ref.startswith("#"):
                continue
            seen.setdefault(inst.ref, []).append((inst.lib.name.split(":")[-1], inst.unit, inst.value, sh.filename))
    bad = []
    for ref, uses in seen.items():
        if len({u[0] for u in uses}) > 1 or len({(u[0], u[1]) for u in uses}) < len(uses):
            bad.append(f"{ref}: " + " and ".join(f"{v or lib} on {f}" for lib, _, v, f in uses))
    if bad:
        raise ValueError("one reference for two parts (KiCad would join their nets); give one another reference: "
                         + "; ".join(bad))


def _ensure_lib(path, head, name, uri):
    """Add (or keep) one library entry in a lib table without disturbing the others."""
    line = f'\t(lib (name "{name}")(type "KiCad")(uri "{uri}")(options "")(descr "project library"))\n'
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(f"({head}\n\t(version 7)\n{line})\n")
        return
    txt = open(path, encoding="utf-8").read()
    if f'(name "{name}")' in txt:
        return
    i = txt.rstrip().rfind(")")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt[:i] + line + txt[i:])
