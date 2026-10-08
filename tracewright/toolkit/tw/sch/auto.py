"""Schematics laid out by rule: say what connects, and the sheet is drawn the way an engineer draws it.

    from tw.sch import Design, Part, stock
    from tw.sch.auto import Page
    d = Design("Sensor node", title="Sensor node", company="...")
    pw = d.sheet("Power", "power.kicad_sch", "USB-C input and 3.3 V", paper="A4")
    p = Page(d, pw, base=100, catalog=CAT)
    g = p.group("3.3 V REGULATOR")
    u = g.part("LDO", "U")                          # the group's main part
    g.decouple(u, "3", ["C10u"], "+5V")             # capacitors at the pin, the rail on its wire
    g.decouple(u, "2", ["C22u", "C100n"], "+3V3")
    g.power(u, "1", "GND")
    p.layout()                                      # places the groups, draws their blocks
    d.write("hardware/node")                        # labels resolved across sheets, then written

Each group is a titled block. Its first part sits at its origin; each later part goes to the right of
what is drawn. What hangs off a part's pins is asked for in any order and drawn in the order a person
would draw it: short in-line stubs, labels and series parts first, near the pins; then what hangs
down (pull-downs, decoupling, ground) from the lowest pin up, so an upper line reaches further out
and nothing crosses; then what stands up (pull-ups, supplies) from the top pin down. Every candidate
position is checked against everything drawn (bodies, fields, wires, labels, notes) and against
itself, and the first clear one is taken. Supplies point up and ground down.

A signal leaving its group carries a label (nobody runs a wire across the sheet); a signal on more
than one sheet gets a global label, which tw.sch.finish() redraws in the project's schematic style
(sheet pins when hierarchical). What the pages were asked to connect is saved next to the sheets
(.tracewright-nets.json) and finish() checks KiCad's netlist against it.
"""
import copy, math, re
from . import kisch
from .kisch import snap, rot_vec
from .. import font
from .builder import stock, power as power_symbol

P = 2.54
GROUNDS = ("GND", "AGND", "DGND", "PGND", "GNDA", "GNDD", "VSS", "EARTH", "CHASSIS")
UP, DOWN, LEFT, RIGHT = (0, -1), (0, 1), (-1, 0), (1, 0)


def is_ground(rail):
    r = str(rail).upper()
    return r in GROUNDS or r.startswith("GND") or r.endswith("_GND")


def _is_connector(ref):
    letters = "".join(c for c in ref if c.isalpha()).upper()
    return letters in ("J", "P", "CN", "X", "XS", "XP")


def _dir_name(d):
    return {RIGHT: "right", LEFT: "left", UP: "up", DOWN: "down"}[d]


def _rot_of(d):
    """Label rotation reading away from the pin."""
    return {RIGHT: 0, LEFT: 180, UP: 90, DOWN: 270}[d]


def _add(p, d, k):
    return (snap(p[0] + d[0] * k), snap(p[1] + d[1] * k))


def _hit(a, b, gap=0.0):
    e = gap - 1e-6                                 # boxes that only touch do not hit (69.85 - 2.54 is 67.30999...)
    return a[0] < b[2] + e and b[0] < a[2] + e and a[1] < b[3] + e and b[1] < a[3] + e


BALL = re.compile(r"^[A-Z]{1,2}\d{1,2}$")


def _is_bga(inst):
    """Pins named as balls (A1, P12, AA3): a BGA, whose small capacitors go on the bottom, under it."""
    nums = {str(q["number"]) for q in inst.lib.pins}
    return len(nums) >= 16 and all(BALL.match(n) for n in nums)


def _farads(v):
    """A capacitor's value in farads ("100n", "2u2", "10u 25V"), or None."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([pnuµ])(\d*)", str(v or ""))
    if not m:
        return None
    x = float(m.group(1) + ("." + m.group(3) if m.group(3) and "." not in m.group(1) else ""))
    return x * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6}[m.group(2)]


def _rail_tag(rq):
    """How a net label is written: "global" for a supply drawn as a label by choice, "rail?" for one drawn as a label for
    lack of room (local if its symbol is on the sheet anyway), None for a signal (global only when it crosses sheets)."""
    r = rq.get("rail")
    return "rail?" if r == "fallback" else "global" if r else None


def _snap_up(v):
    """The grid point at or after v."""
    return snap(math.ceil(round(v / kisch.GRID, 6)) * kisch.GRID)


def _seg_box(a, b):
    return (min(a[0], b[0]) - 0.15, min(a[1], b[1]) - 0.15, max(a[0], b[0]) + 0.15, max(a[1], b[1]) + 0.15)


_CROSS = set()      # nets labelled on another sheet already (their labels are flagged, centred on the wire), set per group


def _label_box(name, pt, d, flag=None):
    """The page area a label covers, as KiCad draws the kind it will be: a global (or hierarchical) label's flag is
    centred on its wire (its text 1.15 mm up, its outline 0.95 mm down as the plot reads it); a local label's text sits
    above its wire, about 1.55 mm up. flag None: by the net (_CROSS). The flag's 2.5 mm used for every label made
    neighbouring pins 2.54 mm apart collide (every other label jogged); a centred box for a local label missed its
    text's top (a value just above it overlapped on the plot)."""
    if flag is None:                                # a label whose kind is not known yet: room for either
        flag = True if name in _CROSS else "either"
    w = font.ink_width(name) + (0.4 if flag is False else 3.4)
    x, y = pt
    h0, h1 = {True: (1.15, 0.95), False: (1.5, 0.0), "either": (1.55, 0.95)}[flag]
    if d == RIGHT:
        return (x, y - h0, x + w, y + h1)
    if d == LEFT:
        return (x - w, y - h0, x, y + h1)
    if d == UP:
        return (x - h0, y - w, x + h1, y)
    return (x - h0, y, x + h1, y + w)


def _text_box(s, at, size=1.27):
    lines = str(s).split("\n")
    a, z = font.ink(s, size)                       # the strokes, from the anchor
    h = len(lines) * size * 1.65
    return (at[0] + a, at[1] - size * 0.3, at[0] + z, at[1] + h)


def _core(b):
    """A label's box as the same kind as its neighbours: labels that touch are written as one kind (Page.emit), so
    two of them never meet as a local label's text above a global label's flag."""
    if abs((b[3] - b[1]) - 2.5) < 0.01:                 # either kind, sideways: the local text's top above the flag's
        return (b[0], b[1] + 0.4, b[2], b[3])
    if abs((b[2] - b[0]) - 2.5) < 0.01:                 # standing
        return (b[0] + 0.4, b[1], b[2], b[3])
    return b


def _near(a, ka, b, kb, gap):
    """Do two drawn things come within `gap`? Texts side by side need a space between them: 0.3 mm read as one run of
    text ("FB201PWR_FLAG", two notes as one line); above each other 0.3 mm is the line spacing."""
    if ka == kb == "label":
        return _hit(_core(a), _core(b), gap)
    if ka == kb == "text":
        rows = min(a[3], b[3]) - max(a[1], b[1])             # on one line: they share at least half a line's height
        if rows >= 0.5 * min(a[3] - a[1], b[3] - b[1]):
            return _hit((a[0] - 0.7, a[1], a[2] + 0.7, a[3]), b, gap)
    return _hit(a, b, gap)


def _gap(k1, k2):
    if "vlane" in (k1, k2):                        # a top or bottom pin's supply, kept for it: no name or body on it
        other = k2 if k1 == "vlane" else k1
        return {"text": 0.0, "label": 0.0, "body": 0.0}.get(other, -99.0)
    if "halo" in (k1, k2):                         # a margin that only text and labels must keep
        other = k2 if k1 == "halo" else k1
        return 0.0 if other in ("text", "label") else -99.0
    if k1 == "wire" and k2 == "wire":
        return -0.05                               # wires may touch only where they are meant to
    if k1 == k2 == "label":
        return 0.05                                # labels on neighbouring pins, a grid step apart
    if {k1, k2} == {"label", "text"}:
        return 0.1                                 # both boxes hold their text's ink: a hair apart is apart
    if "text" in (k1, k2) or "label" in (k1, k2):
        return 0.3
    return 0.9


class Group:
    """One function on a sheet, drawn in its own coordinates and placed by the page."""

    def __init__(self, page, title):
        self.page, self.title = page, title
        self.ops = []                    # what to draw, in the group's coordinates
        self.boxes = []                  # (kind, box, owner): what is drawn
        self.parts = {}                  # ref -> probe (kisch.Inst, not yet on a sheet)
        self.pending = []                # pin patterns asked for, drawn at the next flush
        self.later = []                  # chains and notes, drawn after the pin patterns

    # ------------------------------------------------------------------ geometry
    def _cat(self, key):
        return self.page.cat[key] if isinstance(key, str) else key

    def _probe(self, key, ref, at, rot=0, unit=1, value=None, fields=None, **flags):
        cat = self._cat(key)
        f = {k: v for k, v in cat.fields.items() if v}
        f.update(fields or {})
        inst = kisch.Inst(self.page.sheet, cat.symbol, ref, value or cat.value, (snap(at[0]), snap(at[1])), rot, unit, f,
                          None, cat.footprint, False, True, True, None)
        inst.ref_at = inst.val_at = None
        inst.hide_value = False
        inst.mirror = flags.get("mirror")
        inst.fields_left = flags.get("fields_left", False)
        inst.fields_above = flags.get("fields_above", False)
        inst.fields_below = flags.get("fields_below", False)
        return inst

    def _two(self, key, ref, p1, direction, value=None, **flags):
        """A two-pin part with pin 1 on p1 and pin 2 toward `direction`."""
        cat = self._cat(key)
        want = {"down": DOWN, "up": UP, "right": RIGHT, "left": LEFT}[direction]
        a = [q for q in cat.symbol.pins if q["number"] == "1"][0]
        b = [q for q in cat.symbol.pins if q["number"] == "2"][0]
        for rot in (0, 90, 180, 270):
            ax, ay = rot_vec(a["x"], -a["y"], rot)
            bx, by = rot_vec(b["x"], -b["y"], rot)
            dx, dy = bx - ax, by - ay
            n = math.hypot(dx, dy)
            if abs(dx / n - want[0]) < 1e-6 and abs(dy / n - want[1]) < 1e-6:
                return self._probe(key, ref, (p1[0] - ax, p1[1] - ay), rot, value=value, **flags)
        raise ValueError(f"{ref}: no rotation puts pin 2 {direction} of pin 1")

    def _part_items(self, inst):
        return [("body", inst.bbox(), inst.ref)] + [("text", b, inst.ref) for b in inst.field_boxes()]

    def _wire_items(self, pts, owner=None):
        """Thin boxes along a wire. Only its first segment, the one leaving the owner's pin, may touch the
        owner's body."""
        out = []
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            out.append(("wire", (min(a[0], b[0]) - 0.15, min(a[1], b[1]) - 0.15, max(a[0], b[0]) + 0.15,
                                 max(a[1], b[1]) + 0.15), owner if i == 0 else None))
        return out

    def _power_items(self, rail, pt, rot=0):
        lib = stock("power", "PWR_FLAG") if rail == "PWR_FLAG" else power_symbol(rail)
        probe = kisch.Inst(self.page.sheet, lib, "#PWR?", rail, pt, rot, 1, {}, None, "", False, False, False, None)
        probe.ref_at = probe.val_at = None
        probe.hide_value = probe.fields_left = probe.fields_above = probe.fields_below = False
        body, owner = probe.bbox(), "#" + rail + str(pt)
        text = probe.field_boxes()[0] if probe.field_boxes() else body
        out = [("body", body, owner), ("text", text, owner)]
        if int(rot) % 180 == 90:          # turned sideways its bars and name fill the pin pitch: the next pin's text keeps
            out.append(("halo", (body[0], body[1] - 0.4, body[2], body[3] + 0.4), owner))           # off them (a label
            out.append(("halo", (text[0], text[1] - 0.4, text[2], text[3] + 0.4), owner))           # 0.2 mm off looks joined)
        return out

    def _clear(self, items, anchor=None):
        """Would these items land on each other, or on anything drawn? A wire may leave the anchor's body
        (it starts at a pin) and touch the parts of its own pattern where it meets their pins. The candidate is
        checked against itself first: when that fails (self.why[0] == "self"), no other place can help."""
        if not self._self_clear(items):
            return False
        for kind, box, owner in items:
            for k2, b2, o2 in self.boxes:
                if k2 == "body" and o2 == anchor:
                    if kind == "wire" and owner == anchor:            # the segment leaving the anchor's pin
                        continue
                    if kind == "body":                                # a symbol on the anchor's pin may meet it
                        if _hit(box, b2, -1.0):
                            self.why = (kind, owner, k2, o2)
                            return False
                        continue
                if _near(box, kind, b2, k2, _gap(kind, k2)):
                    self.why = (kind, owner, k2, o2)
                    return False
        return True

    def _self_clear(self, items):
        """Do these items (one drawing) keep clear of each other? Texts clear, bodies may meet at pins."""
        for i, (k1, b1, o1) in enumerate(items):
            for k2, b2, o2 in items[i + 1:]:
                if "wire" in (k1, k2):
                    if ("text" in (k1, k2)) and _hit(b1, b2, 0.3):
                        self.why = ("self", k1, o1, k2, o2)
                        return False
                    continue
                if o1 == o2 and o1 is not None:
                    continue
                if _near(b1, k1, b2, k2, -1.0 if k1 == k2 == "body" else _gap(k1, k2)):
                    self.why = ("self", k1, o1, k2, o2)
                    return False
        return True

    def _take(self, items):
        self.boxes += items

    def _connect(self, net, ref, pin):
        self.page.nets.setdefault(net, set()).add((ref, str(pin)))

    def bbox(self):
        bs = [b for _, b, _ in self.boxes]
        if not bs:
            return (0, 0, 0, 0)
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    # ------------------------------------------------------------------ parts
    def part(self, key, prefix, ref=None, value=None, rot=0, unit=1, fields=None, gap=15.24, face="auto"):
        """Place a part: the group's first at its origin, each later one to the right of what is drawn
        (its pin patterns included), clear of everything. face: which way a single-row connector's pins point
        -- "auto" toward the circuit (right when it is the group's first part, left when it comes after
        others), "left", "right", or None to leave the symbol as it is drawn. The symbol is mirrored, not
        turned, so pin 1 stays at the top."""
        self.flush()
        ref = self.page.ref(prefix, ref)
        mirror = self._facing(key, ref, rot, unit, value, fields, face)
        if not self.parts:
            inst = self._probe(key, ref, (0, 0), rot, unit, value, fields, mirror=mirror)
        else:
            probe = self._probe(key, ref, (0, 0), rot, unit, value, fields, mirror=mirror)
            x = snap(self.bbox()[2] + gap - probe.bbox()[0])
            inst = None
            for k in range(80):
                cand = self._probe(key, ref, (snap(x + k * P), 0), rot, unit, value, fields, mirror=mirror)
                if self._clear(self._part_items(cand)):
                    inst = cand
                    break
            inst = inst or self._probe(key, ref, (x, 0), rot, unit, value, fields, mirror=mirror)
        self._fields_clear_of_pins(inst)
        self.parts[ref] = inst
        self.__dict__.setdefault("main", []).append(ref)          # the block's own parts (not support parts)
        self.ops.append(("part", inst))
        self._take(self._part_items(inst))
        return inst

    def _facing(self, key, ref, rot, unit, value, fields, face):
        """'y' when a connector must be mirrored to face the way asked (see part), else None."""
        if face is None or not _is_connector(ref) or int(rot) % 360 != 0:
            return None
        probe = self._probe(key, ref, (0, 0), rot, unit, value, fields)
        dirs = {probe.pin_dir(p["number"]) for p in probe.lib.pins if p["unit"] in (0, unit)}
        if len(dirs) != 1 or next(iter(dirs)) not in (LEFT, RIGHT):
            return None                                           # two rows, or pins on several sides: as drawn
        want = RIGHT if face == "right" or (face == "auto" and not self.parts) else LEFT
        return "y" if next(iter(dirs)) != want else None

    def _fields_clear_of_pins(self, inst):
        """Reference and value above the body, moved right of the top pins when they would sit over them
        (a wire from a top pin must not run through the part's own name)."""
        tops = [inst.pin(p["number"])[0] for p in inst.lib.pins if p["unit"] in (0, inst.unit)
                and inst.pin_dir(p["number"]) == UP]
        if not tops:
            return
        boxes = inst.field_boxes()
        if not any(b[0] - 0.8 <= x <= b[2] + 0.8 for x in tops for b in boxes):
            return
        bx0, by0, bx1, by1 = inst.bbox()
        x = snap(max(tops) + 1.27)                            # the usual height, right of the top pins' wires
        inst.ref_at, inst.val_at = (x, snap(by0 - 5.08), "left"), (x, snap(by0 - 2.54), "left")

    # ------------------------------------------------------------------ pin patterns (asked for, drawn at flush)
    def _ask(self, kind, inst, pin, **kw):
        self.pending.append({"kind": kind, "inst": inst, "pin": str(pin), **kw})

    def nc(self, inst, pins):
        for pin in ([pins] if isinstance(pins, (str, int)) else pins):
            self._ask("nc", inst, pin)

    def power(self, inst, pins, rail, flag=False, voltage=None, current=None):
        """A rail at each pin: supplies point up, ground down, on a short stub where the pin does not
        already point that way. flag: a PWR_FLAG where the rail enters the board (connectors). voltage /
        current: what the rail is and carries (the net model: checks, net classes, the app's net list)."""
        if voltage is not None or current is not None:
            self.page.d.net(rail, kind="ground" if is_ground(rail) else "power", voltage=voltage, current=current)
        pins = [pins] if isinstance(pins, (str, int)) else list(pins)
        want = DOWN if is_ground(rail) else UP
        if len(pins) >= 2 and not flag and all(inst.pin_dir(p) == want for p in pins) and \
                not (self.page.conv.get("supplies") == "labels" and not is_ground(rail)):
            self._ask("power_bar", inst, pins[0], pins=[str(p) for p in pins], rail=rail)      # one symbol on a bar
            return
        for pin in pins:
            self._ask("power", inst, pin, rail=rail, flag=flag)

    def divider(self, top, mid, rkey_top, rkey_bottom, bottom="GND", refs=(None, None), cap=None, cap_ref=None, **attrs):
        """A resistor divider, top to bottom: the `top` rail, a resistor, the tap (a short wire to the `mid` net's
        label), a resistor, and `bottom`. cap: a filter capacitor from the tap to `bottom`, beside the lower
        resistor. attrs: what the mid net is (kind="analog" ...; see tw.netmodel). Drawn after the pin
        patterns; returns the references."""
        refs = (self.page.ref("R", refs[0]), self.page.ref("R", refs[1]))
        cap_ref = self.page.ref("C", cap_ref) if cap else None
        if attrs:
            self.page.d.net(mid, **attrs)
        self.later.append(("divider", top, mid, rkey_top, rkey_bottom, bottom, refs, cap, cap_ref))
        return refs + ((cap_ref,) if cap else ())

    def net(self, inst, pins, name, **attrs):
        """A short stub and a label; several pins of one net are joined and labelled once. attrs: what the
        net is (kind="clock", current=2, pair="USB_D_N", impedance=90 ...; see tw.netmodel)."""
        if attrs:
            self.page.d.net(name, **attrs)
        pins = [pins] if isinstance(pins, (str, int)) else list(pins)
        self._ask("net", inst, pins[0], name=name, pins=[str(p) for p in pins])

    def decouple(self, inst, pin, caps, rail, refs=None):
        """Capacitors at a power pin, where they are placed: a wire from the pin to the rail symbol, the
        capacitors hanging off it beside the pin, smallest nearest, each to ground."""
        refs = [self.page.ref("C", r) for r in refs] if refs else [self.page.ref("C") for _ in caps]
        self._ask("decouple", inst, pin, caps=list(caps), rail=rail, refs=refs)
        return refs

    def pull(self, inst, pin, key, to, net=None, ref=None, cap=None, cap_ref=None, gnd="GND"):
        """A pull-up (to a rail) or pull-down (to ground) on the pin's line; the line carries `net`'s label
        beyond it. cap: a capacitor from the same line to ground, drawn with it at the pin (an enable's RC delay,
        a reset's filter): pull(u, "EN", "R10k", "+3V3", net="EN", cap="C1u"). Returns the resistor's reference,
        or (resistor, capacitor) with a cap."""
        ref = self.page.ref("R", ref)
        cap_ref = self.page.ref("C", cap_ref) if cap else None
        self._ask("pull", inst, pin, key=key, to=to, net=net, ref=ref, cap=cap, cap_ref=cap_ref, gnd=gnd)
        return (ref, cap_ref) if cap else ref

    def series(self, inst, pin, key, net, ref=None, before=None):
        """A part in line with the pin (series resistor, ferrite): a stub, the part along the pin's
        direction, then `net`'s label. `before` names the pin's side of the part."""
        ref = self.page.ref("R", ref)
        self._ask("series", inst, pin, key=key, net=net, ref=ref, before=before)
        return ref

    def indicator(self, inst, pin, rkey, dkey, refs=(None, None), gnd="GND"):
        """An LED driven from a pin: the pin, a series resistor and the LED in line, then ground."""
        refs = (self.page.ref("R", refs[0]), self.page.ref("D", refs[1]))
        self._ask("indicator", inst, pin, rkey=rkey, dkey=dkey, refs=refs, gnd=gnd)
        return refs

    def crystal(self, inst, pin_a, pin_b, ykey, caps, ref=None, cap_refs=None, gnd="GND"):
        """A crystal on two oscillator pins of one side, with its load capacitors to ground: the crystal
        stands between the two lines, each capacitor runs out from its end of the crystal to ground."""
        ref = self.page.ref("Y", ref)
        cap_refs = [self.page.ref("C", r) for r in cap_refs] if cap_refs else [self.page.ref("C"), self.page.ref("C")]
        self._ask("crystal", inst, pin_a, pins=[str(pin_a), str(pin_b)], ykey=ykey, caps=list(caps), ref=ref,
                  cap_refs=cap_refs, gnd=gnd)
        return ref, cap_refs

    def led(self, rail, rkey, dkey, refs=(None, None), gnd="GND"):
        """A supply, a series resistor and an LED to ground, top to bottom (drawn after the pin patterns)."""
        refs = (self.page.ref("R", refs[0]), self.page.ref("D", refs[1]))
        self.later.append(("led", rail, rkey, dkey, refs, gnd))
        return refs

    def note(self, text, near=None, why=None, pin=None):
        """A short note on the sheet, beside what it explains -- near: the part (or its reference; pin: one of its pins)
        -- in plain words an engineer reads at a glance: "Boot straps: IO2, IO8 high", "Vout = 0.8 V x (1 + R1/R2)".
        why: the reasoning, numbers and source, kept off the sheet in the design notes (the app shows it on hover)."""
        self.later.append(("note", text, near.ref if hasattr(near, "ref") else near, why, pin))

    def why(self, near, text, pin=None):
        """The reasoning behind a part, pin or net ("net:EN"), kept off the sheet in the design notes."""
        from .notes import anchor_of
        a = anchor_of(near, pin)
        if a:
            self.page.design_notes.append({"anchor": a, "short": "", "why": str(text), "sheet": self.page.sheet.filename})

    # ------------------------------------------------------------------ drawing the patterns
    def _order(self, rq):
        p = rq["inst"].pin(rq["pin"])
        d = rq["inst"].pin_dir(rq["pin"])
        kind = rq["kind"]
        side = {RIGHT: 0, LEFT: 1, UP: 2, DOWN: 3}[d]
        if kind == "nc":
            return (0, 0, 0)
        if kind in ("crystal", "indicator"):
            return (1, side, p[1] if d[1] == 0 else p[0])
        if kind == "power_bar":
            return (2, side, p[0])
        sideways_ok = kind == "power" and d[1] == 0 and _is_connector(rq["inst"].ref)
        hangs = not sideways_ok and (kind == "decouple" or (kind == "pull" and is_ground(rq["to"])) or
                                     (kind == "power" and is_ground(rq["rail"]) and d != DOWN))
        stands = not sideways_ok and ((kind == "pull" and not is_ground(rq["to"])) or
                                      (kind == "power" and not is_ground(rq["rail"]) and d != UP))
        if not hangs and not stands:
            return (1, side, p[1] if d[1] == 0 else p[0])
        if d[1] != 0:
            return (2, side, p[0])
        return (2 if hangs else 3, side, -p[1] if hangs else p[1])

    def _cross(self):
        """Which nets already have labels on another sheet of the design: theirs will be flagged."""
        _CROSS.clear()
        here = self.page.sheet.filename
        for pg in getattr(self.page.d, "pages", []):
            if pg.sheet.filename != here:
                for g in pg.groups:
                    _CROSS.update(op[1] for op in g.ops if op[0] == "label" and (len(op) < 5 or op[4] != "rail?"))
        _CROSS.update(self.page.d.__dict__.get("_global_rails", ()))

    def flush(self):
        """Draw what was asked for, in order. Every pin still waiting keeps its way out (a short stub's
        worth, reserved) so an earlier pattern cannot wall it in. When a pattern still found no room and was
        drawn aside, the group is drawn again with that pin's whole way out kept clear from the start (its
        label's, its part's or its supply symbol's worth), and the drawing with the fewest patterns aside is kept."""
        self._cross()
        todo, self.pending = self.pending, []
        series = {(id(rq["inst"]), rq["pin"]): rq for rq in todo if rq["kind"] == "series"}
        merged = []
        for rq in todo:                                   # a gate resistor and its pull-down: one drawing
            sq = series.get((id(rq["inst"]), rq["pin"])) if rq["kind"] == "pull" else None
            if sq is not None and "pull" not in sq and rq.get("net") in (None, sq.get("before")) or \
                    (sq is not None and "pull" not in sq and sq.get("before") is None):
                sq["pull"] = rq
                continue
            merged.append(rq)
        todo = sorted(merged, key=self._order)
        start, best, lanes = self._state(), None, set()
        for attempt in range(3):
            if attempt:
                self._restore(start)
            n0 = len(self.page.crowded)
            self._draw_all(todo, lanes)
            aside = self.page.crowded[n0:]
            if best is None or len(aside) < best[0]:
                best = (len(aside), self._state())
            stuck = {m.groups() for m in (re.match(r"(\S+) pin (\S+):", c) for c in aside) if m}
            if not aside or stuck <= lanes:
                break
            lanes |= stuck
        if attempt:
            self._restore(best[1])

    def _draw_all(self, todo, lanes):
        keep = {}
        for rq in todo:
            if rq["kind"] == "nc":
                continue
            for pin in rq.get("pins") or [rq["pin"]]:
                p, d = rq["inst"].pin(pin), rq["inst"].pin_dir(pin)
                keep[(rq["inst"].ref, pin)] = [("wire", _seg_box(p, _add(p, d, P)), ("exit", rq["inst"].ref, pin))] + \
                    (self._lane(rq, pin, p, d) if (rq["inst"].ref, pin) in lanes else [])
        self.boxes += [b for bs in keep.values() for b in bs]
        for rq in todo:
            mine = [b for pin in rq.get("pins") or [rq["pin"]] for b in keep.get((rq["inst"].ref, pin), [])]
            self.boxes = [b for b in self.boxes if b not in mine]
            getattr(self, "_draw_" + rq["kind"])(rq)
        self.boxes = [b for b in self.boxes if not (isinstance(b[2], tuple) and b[2][0] in ("exit", "lane"))]

    @staticmethod
    def _lane(rq, pin, p, d):
        """A pin's whole way out: a sideways pin's line as far as its pattern reaches drawn straight (a label's stub
        and text, a series part and its label, an LED's resistor and LED), held as a wire so nothing crosses it or
        sits on it; a top or bottom pin's supply symbol, as wide as its name."""
        kind, tag = rq["kind"], ("lane", rq["inst"].ref, pin)
        label = lambda n: font.ink_width(str(n)) + 3.4 if n else 0
        if d[1] == 0 or kind == "net":
            if kind == "series":
                lead = math.ceil((font.ink_width(rq["before"]) + 2 * P) / P) * P if rq.get("before") else P
                L = lead + 7.62 + P + label(rq.get("net"))
            else:
                L = {"net": P + label(rq.get("name")), "indicator": 3 * P + 2 * 7.62,
                     "pull": 3 * P + label(rq.get("net")),
                     "crystal": P + label(f"{rq['inst'].ref}_{pin}")}.get(kind, 2 * P)    # its label when drawn aside
            return [("wire", _seg_box(_add(p, d, P), _add(p, d, max(2 * P, L))), tag)]
        half = max(1.3, font.ink_width(str(rq.get("rail") or "")) / 2 + 0.3)
        f = _add(p, d, 3 * P if kind == "decouple" else 2 * P)
        return [("vlane", (min(p[0], f[0]) - half, min(p[1], f[1]), max(p[0], f[0]) + half, max(p[1], f[1])), tag)]

    def _state(self):
        return (list(self.ops), list(self.boxes), dict(self.parts), {k: set(v) for k, v in self.page.nets.items()},
                list(self.page.crowded), copy.deepcopy(self.__dict__.get("marks", {})), list(self.later), list(self.page.near))

    def _restore(self, st):
        self.ops, self.boxes, self.parts = list(st[0]), list(st[1]), dict(st[2])
        self.page.nets.clear()
        self.page.nets.update({k: set(v) for k, v in st[3].items()})
        self.page.crowded[:] = st[4]
        self.__dict__["marks"] = copy.deepcopy(st[5])
        self.later[:] = st[6]                             # what an attempt queued for later goes with it
        self.page.near[:] = st[7]

    def _commit(self, ops, items):
        for op in ops:
            if op[0] == "part":
                self.parts[op[1].ref] = op[1]
        self.ops += ops
        self._take(items)

    def _draw_nc(self, rq):
        x, y = rq["inst"].pin(rq["pin"])
        self.ops.append(("nc", (x, y)))
        self.boxes.append(("body", (x - 0.8, y - 0.8, x + 0.8, y + 0.8), rq["inst"].ref))   # its X: notes and labels keep off

    def _draw_power(self, rq):
        inst, pin, rail, flag = rq["inst"], rq["pin"], rq["rail"], rq["flag"]
        if self.page.conv.get("supplies") == "labels" and not is_ground(rail):
            self._draw_net({"inst": inst, "pin": pin, "name": rail, "pins": [pin], "rail": True})
            return
        self._connect(rail, inst.ref, pin)
        p, d = inst.pin(pin), inst.pin_dir(pin)
        want = DOWN if is_ground(rail) else UP
        side_rot = None
        if d[1] == 0 and _is_connector(inst.ref):                  # at a connector pin the symbol points out
            side_rot = (270 if d == LEFT else 90) if is_ground(rail) else (90 if d == LEFT else 270)
        tries = []
        for L in ((2 * P, 3 * P, 4 * P, 5 * P, 6 * P, 7 * P, 8 * P) if flag else (0.0, P, 2 * P, 3 * P, 4 * P, 5 * P, 6 * P)):
            if d == want:
                tries.append(([p, _add(p, d, L)] if L else [p], 0))
            elif d[1] == 0:
                if side_rot is not None:
                    tries.append(([p, _add(p, d, L or P)], side_rot))
                tries.append(([p, _add(p, d, L or P)], 0))
            else:                                                  # pointing the wrong way: out, over, the symbol
                a = _add(p, d, P)
                for side in (RIGHT, LEFT):
                    tries.append(([p, a, _add(a, side, 2 * P + L)], 0))
        for pts, rot in tries:
            end = pts[-1]
            base = self._power_items(rail, end, rot) + (self._wire_items(pts, inst.ref) if len(pts) > 1 else [])
            spots = [None]
            if flag and len(pts) > 1:                              # the flag on the stub, clear of the part and the rail
                L = abs(end[0] - p[0]) + abs(end[1] - p[1])
                spots = [_add(p, d, k * P) for k in range(max(1, int(round(L / P)) - 2), 0, -1)]
            for fpt in spots:
                items = base + (self._power_items("PWR_FLAG", fpt) if fpt else [])
                if self._clear(items, anchor=inst.ref):
                    ops = ([("wire", pts)] if len(pts) > 1 else []) + [("power", rail, end, rot)]
                    if fpt:
                        ops.append(("flag", fpt))
                    self._commit(ops, items)
                    return
        # global where it must be: a local label joins the supply only on a sheet that has its symbol too (VIN_5V was
        # cut off); local where the sheet has it (GND on a connector: a global label's flag ran into the next pin's label)
        self._draw_net({"inst": inst, "pin": pin, "name": rail, "pins": [pin], "rail": "fallback"}, record=False)
        self.page.crowded.append(f"{inst.ref} pin {pin}: {rail} as a label (no room for its symbol) {getattr(self, 'why', '')}")

    def _draw_power_bar(self, rq):
        """Several pins of one rail on one side: a stub from each, a bar joining them, one rail symbol on it
        (pins that do not line up, or no room: a symbol at each)."""
        inst, rail, pins = rq["inst"], rq["rail"], rq["pins"]
        d = inst.pin_dir(pins[0])
        at = {}                                          # stacked pins (one point in the symbol) are joined there
        for p in pins:
            at.setdefault(tuple(round(c, 3) for c in inst.pin(p)), p)
        if len(at) == 1:                                 # all on one point: one symbol (a bar of no length left the
            self._draw_power({"inst": inst, "pin": pins[0], "rail": rail, "flag": False})   # symbol on nothing)
            for pin in pins[1:]:
                self._connect(rail, inst.ref, pin)
            return
        pts = sorted((inst.pin(p) for p in at.values()), key=lambda q: q[0])
        if len({round(q[1], 3) for q in pts}) != 1:
            for pin in pins:
                self._draw_power({"inst": inst, "pin": pin, "rail": rail, "flag": False})
            return
        for L in (P, 2 * P, 3 * P):
            ends = [_add(q, d, L) for q in pts]
            # the symbol on the bar between the middle two stubs, never on a stub's end (four wires would meet)
            k = (len(ends) - 1) // 2
            mid_x = snap((ends[k][0] + ends[k + 1][0]) / 2)
            if any(abs(mid_x - e[0]) < 1e-6 for e in ends):
                mid_x = snap(ends[k][0] + 1.27)
            sym = (mid_x, ends[0][1])
            stubs = [[q, e] for q, e in zip(pts, ends)]
            bar = [ends[0], ends[-1]]
            items = [it for s_ in stubs for it in self._wire_items(s_, inst.ref)] + self._wire_items(bar)
            items += self._power_items(rail, sym, 0)
            if self._clear(items, anchor=inst.ref):
                ops = [("wire", s_) for s_ in stubs] + [("wire", bar), ("power", rail, sym, 0)]
                joins = {tuple(e) for e in ends[1:-1]}                     # stubs meeting the bar mid-way: T joints
                if mid_x not in (ends[0][0], ends[-1][0]):
                    joins.add(sym)
                ops += [("junction", j) for j in sorted(joins)]
                self._commit(ops, items)
                for pin in pins:
                    self._connect(rail, inst.ref, pin)
                return
        for pin in pins:
            self._draw_power({"inst": inst, "pin": pin, "rail": rail, "flag": False})

    def _mark(self, name, pt, d, owner, boxes):
        """Remember where a net's label was drawn in this group, so a later pin of the net can be wired to it."""
        self.__dict__.setdefault("marks", {}).setdefault(name, []).append({"pt": pt, "d": d, "owner": owner, "boxes": boxes})

    def _wire_to_mark(self, inst, pin, name, reach=40.0):
        """A pin whose net is already labelled in this group, on another part: a short orthogonal wire to that
        label's end, the way a person joins neighbours, when it runs clear of everything (and not over the
        label). True when drawn."""
        marks = (self.__dict__.get("marks") or {}).get(name) or []
        p, d = inst.pin(pin), inst.pin_dir(pin)
        marks = sorted((m for m in marks if m["owner"] != inst.ref),
                       key=lambda m: abs(m["pt"][0] - p[0]) + abs(m["pt"][1] - p[1]))
        for m in marks:
            mx, my = m["pt"]
            if abs(mx - p[0]) + abs(my - p[1]) > reach:
                continue
            md = m["d"]
            # the label's own text, less the bit where the wire meets it
            lab = [b for b in m["boxes"] if b[0] == "label"]
            keep = [b for b in m["boxes"] if b[0] != "label"]
            shrunk = [("label", (bx[0] + 0.6 * max(md[0], 0), bx[1] + 0.6 * max(md[1], 0), bx[2] + 0.6 * min(md[0], 0),
                                 bx[3] + 0.6 * min(md[1], 0)), None) for _, bx, _ in lab]
            for L in (P, 2 * P, 3 * P):
                e = _add(p, d, L)
                paths = [[p, e, (mx, my)]] if abs(e[0] - mx) < 1e-6 or abs(e[1] - my) < 1e-6 else \
                    [[p, e, (mx, e[1]), (mx, my)], [p, e, (e[0], my), (mx, my)]]
                for path in paths:
                    pts = [path[0]]
                    for q in path[1:]:
                        if q != pts[-1]:
                            pts.append((snap(q[0]), snap(q[1])))
                    if len(pts) < 2:
                        continue
                    last = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
                    if last[0] * md[0] + last[1] * md[1] < 0:     # coming in from the label's side: it would run over it
                        continue
                    items = self._wire_items(pts, inst.ref)
                    saved = self.boxes
                    self.boxes = [b for b in self.boxes if b not in m["boxes"]] + shrunk
                    ok = self._clear(items, anchor=inst.ref)
                    self.boxes = saved
                    if ok:
                        self._commit([("wire", pts)], items)
                        m["joined"] = m.get("joined", 0) + 1
                        if m["joined"] == 2:                    # three wires meet at the label: a dot
                            self.ops.append(("junction", m["pt"]))
                        return True
        return False

    def _draw_net(self, rq, record=True):
        inst, name, pins = rq["inst"], rq["name"], rq["pins"]
        if rq.get("rail") is True:                         # a supply drawn as a label by choice: a global label
            _CROSS.add(name)
            self.page.d.__dict__.setdefault("_global_rails", set()).add(name)
        if record:
            for pin in pins:
                self._connect(name, inst.ref, pin)
        d = inst.pin_dir(pins[0])
        pts = [inst.pin(pin) for pin in pins]
        if record and len(pins) == 1 and not rq.get("rail") and self._wire_to_mark(inst, pins[0], name):
            return
        for L in (P, 2 * P, 3 * P, 4 * P, 6 * P, 8 * P):
            if len(pins) == 1:
                end = _add(pts[0], d, L)
                ops = [("wire", [pts[0], end]), ("label", name, end, d) + (_rail_tag(rq),)]
                items = self._wire_items([pts[0], end], inst.ref) + [("label", _label_box(name, end, d), None)]
            else:                                                  # stubs to a common line, joined, one label beyond
                reach = max(q[0] * d[0] + q[1] * d[1] for q in pts) + L
                ends = [((reach * d[0]) if d[0] else q[0], (reach * d[1]) if d[1] else q[1]) for q in pts]
                ends = [(snap(e[0]), snap(e[1])) for e in ends]
                ops, items = [], []
                for q, e in zip(pts, ends):
                    ops.append(("wire", [q, e]))
                    items += self._wire_items([q, e], inst.ref)
                ends.sort()
                ops.append(("wire", [ends[0], ends[-1]]))
                items += self._wire_items([ends[0], ends[-1]], inst.ref)
                ops += [("junction", e) for e in ends[1:-1]]
                mid = ends[0] if d[1] == 0 else ends[0]
                out = _add(mid, d, P)
                ops += [("junction", mid), ("wire", [mid, out]), ("label", name, out, d)]
                items += self._wire_items([mid, out], inst.ref) + [("label", _label_box(name, out, d), None)]
            if self._clear(items, anchor=inst.ref):
                self._commit(ops, items)
                if record and not rq.get("rail"):
                    lab = [op for op in ops if op[0] == "label"][-1]
                    self._mark(name, lab[2], lab[3], inst.ref, [it for it in items if it[0] == "label"] +
                               [it for it in items if it[0] == "wire"][-1:])
                return
        if len(pins) == 1:                                  # longer stubs first: past what crowds the pin
            for L in (10 * P, 12 * P, 16 * P, 20 * P):
                end = _add(pts[0], d, L)
                ops = [("wire", [pts[0], end]), ("label", name, end, d) + (_rail_tag(rq),)]
                items = self._wire_items([pts[0], end], inst.ref) + [("label", _label_box(name, end, d), None)]
                if self._clear(items, anchor=inst.ref):
                    self._commit(ops, items)
                    return
        if len(pins) == 1 and d[1] == 0:                   # out, down (or up) to a clear row, then the label
            p = pts[0]
            for vdir in (DOWN, UP):
                for a in (P, 2 * P, 3 * P, 4 * P, 5 * P, 7 * P, 9 * P):
                    for b in range(1, 20):
                        c2 = _add(_add(p, d, a), vdir, b * P)
                        for L in (P, 2 * P, 3 * P, 5 * P):
                            path = [p, _add(p, d, a), c2, _add(c2, d, L)]
                            items = self._wire_items(path, inst.ref) + [("label", _label_box(name, path[-1], d), None)]
                            if self._clear(items, anchor=inst.ref):
                                self._commit([("wire", path), ("label", name, path[-1], d) + (_rail_tag(rq),)], items)
                                return
        # nowhere clear: each pin carries the label itself (no wire, so it cannot touch another net)
        for q in pts:
            self._commit([("label", name, q, d)], [("label", _label_box(name, q, d), None)])
        self.page.crowded.append(f"{inst.ref} pin {pins[0]}: {name} label on the pin (crowded)")

    def _draw_decouple(self, rq):
        inst, pin, caps, rail, refs = rq["inst"], rq["pin"], rq["caps"], rq["rail"], rq["refs"]
        self._connect(rail, inst.ref, pin)
        for r in refs:
            self._connect(rail, r, "1")
            self._connect("GND", r, "2")
        p, d = inst.pin(pin), inst.pin_dir(pin)
        if self.page.conv.get("decoupling") == "row":             # the project draws them together, beside the part
            self._draw_power({"inst": inst, "pin": pin, "rail": rail, "flag": False})
            self.later.append(("caps", caps, refs, rail, f"near {inst.ref} pin {pin}"))   # after the group's own parts
            self._keep_near(inst, [pin], refs, 3.0, caps)
            return
        for pitch in (7.62, 10.16, 12.7, 15.24):
            if d[0] == 0:
                cands = ((reach, side) for reach in range(2, 13) for side in (LEFT, RIGHT))
                plans = (self._decouple_v(inst, p, d, r * P, side, caps, rail, refs, pitch) for r, side in cands)
            else:
                plans = (self._decouple_h(inst, p, d, r * P, caps, rail, refs, pitch) for r in range(2, 17))
            for ops, items in plans:
                if self._clear(items, anchor=inst.ref):
                    self._commit(ops, items)
                    if _is_bga(inst):                              # at the ball on the sheet, under the BGA on the board
                        self._keep_near(inst, [pin], refs, 3.0, caps)
                    return
        # no room at the pin: the rail at the pin, the capacitors on it aside, joined by the rail's name
        self._draw_power({"inst": inst, "pin": pin, "rail": rail, "flag": False})
        self._aside_caps(caps, refs, rail, f"near {inst.ref} pin {pin}")
        self._keep_near(inst, [pin], refs, 3.0, caps)
        self.page.crowded.append(f"{inst.ref} pin {pin}: decoupling drawn beside the part")

    def _cap_down(self, key, ref, top):
        c = self._two(key, ref, top, "down")
        g = c.pin("2")
        return c, [("part", c), ("power", "GND", g, 0)], self._part_items(c) + self._power_items("GND", g)

    def _decouple_v(self, inst, p, d, reach, side, caps, rail, refs, pitch):
        top = _add(p, d, reach + 2 * P)
        rot = 0 if d == UP else 180
        ops = [("wire", [p, top]), ("power", rail, top, rot)]
        items = self._wire_items([p, top], inst.ref) + self._power_items(rail, top, rot)
        yb = _add(p, d, reach)
        end = _add(yb, side, pitch * len(caps))
        ops += [("wire", [yb, end]), ("junction", yb)]
        items += self._wire_items([yb, end], inst.ref)
        for i, key in enumerate(caps):
            x = _add(yb, side, pitch * (i + 1))
            c, o, it = self._cap_down(key, refs[i], x)
            ops += o
            items += it
            if i < len(caps) - 1:
                ops.append(("junction", x))
        return ops, items

    def _decouple_h(self, inst, p, d, reach, caps, rail, refs, pitch):
        end = _add(p, d, reach + pitch * (len(caps) - 1))
        tip = _add(end, d, P)
        ops = [("wire", [p, tip])]
        items = self._wire_items([p, tip], inst.ref)
        for i, key in enumerate(caps):
            x = _add(p, d, reach + pitch * i)
            c, o, it = self._cap_down(key, refs[i], x)
            ops += o + [("junction", x)]
            items += it
        ops.append(("power", rail, tip, 0))
        items += self._power_items(rail, tip)
        return ops, items

    def _draw_pull(self, rq):
        inst, pin, key, to, net, ref = rq["inst"], rq["pin"], rq["key"], rq["to"], rq["net"], rq["ref"]
        cap, cap_ref, gnd = rq.get("cap"), rq.get("cap_ref"), rq.get("gnd") or "GND"
        p, d = inst.pin(pin), inst.pin_dir(pin)
        down = is_ground(to)
        name = net or f"{inst.ref}_{pin}"
        self._connect(name, inst.ref, pin)
        for reach in [k * P for k in range(2, 17)]:
            for tail in ((P, 2 * P) if not cap else (3 * P, 4 * P)):
                t = _add(p, d, reach)
                end = _add(t, d, tail)
                sides = [None] if d[1] == 0 else [RIGHT, LEFT]
                if cap and d[1] != 0:
                    sides = []                             # an RC on a vertical pin: drawn below, beside the part
                for side in sides:
                    if d[1] == 0:
                        r = self._two(key, ref, t, "down") if down else self._two(key, ref, (t[0], snap(t[1] - 7.62)), "down")
                        joint, railp = ("1", "2") if down else ("2", "1")
                        extra = []
                    else:
                        r = self._two(key, ref, _add(t, side, P), _dir_name(side))
                        joint, railp = "1", "2"
                        extra = [[t, r.pin("1")]]
                    rp = r.pin(railp)
                    ops = [("wire", [p, end])] + [("wire", w) for w in extra] + [("junction", t), ("part", r), ("power", to, rp, 0)]
                    items = self._wire_items([p, end], inst.ref) + self._part_items(r) + self._power_items(to, rp)
                    for w in extra:
                        items += self._wire_items(w, ref)
                    if cap:                                # the capacitor from the same line down to ground, a step on
                        ct = _add(t, d, P) if not down else _add(t, d, 2 * P)    # (two T's, never a four-way joint)
                        c = self._two(cap, cap_ref, ct, "down")
                        ops += [("part", c), ("power", gnd, c.pin("2"), 0)] + ([("junction", ct)] if ct != t else [])
                        items += self._part_items(c) + self._power_items(gnd, c.pin("2"))
                    if net:
                        ops.append(("label", name, end, d))
                        items.append(("label", _label_box(name, end, d), None))
                    if self._clear(items, anchor=inst.ref):
                        self._commit(ops, items)
                        self._connect(name, ref, joint)
                        self._connect(to, ref, railp)
                        if cap:
                            self._connect(name, cap_ref, "1")
                            self._connect(gnd, cap_ref, "2")
                        return
        if not cap and d[1] == 0 and self._dogleg_pull(inst, p, d, key, ref, to, net, name):
            return
        # no room on the line: its label at the pin, the resistor aside with the same label (and its capacitor)
        self._draw_net({"inst": inst, "pin": pin, "name": name, "pins": [pin]}, record=False)
        self._aside_pull(key, ref, to, name, f"near {inst.ref} pin {pin}")
        if cap:
            self._aside_rc_cap(cap, cap_ref, name, gnd, f"near {inst.ref} pin {pin}")
        self._keep_near(inst, [pin], [ref] + ([cap_ref] if cap else []), 5.0)
        self.page.crowded.append(f"{inst.ref} pin {pin}: {ref}{' and ' + cap_ref if cap else ''} drawn beside the part {getattr(self, 'why', '')}")

    def _aside_rc_cap(self, key, ref, name, gnd, where=None):
        """An RC's capacitor where there was no room at the pin: from a label of the line down to ground."""
        def make(x, y):
            lab = (x, y)                                      # the line's label, a short wire down to the capacitor
            c = self._two(key, ref, _add(lab, DOWN, P), "down")
            ops = [("label", name, lab, UP), ("wire", [lab, c.pin("1")]), ("part", c), ("power", gnd, c.pin("2"), 0)]
            items = ([("label", _label_box(name, lab, UP), None)] + self._wire_items([lab, c.pin("1")], ref) + self._part_items(c)
                     + self._power_items(gnd, c.pin("2")))
            return ops, items
        self._commit(*self._free_spot(self._beside(make, where)))
        self._connect(name, ref, "1")
        self._connect(gnd, ref, "2")

    def _dogleg_pull(self, inst, p, d, key, ref, to, net, name):
        """Out a little, down (or up) past the other pins' lines, then out to the resistor: the escape a
        person draws for the bottom (or top) pin of a busy side."""
        down = is_ground(to)
        for vdir in (DOWN, UP):
            for a in (P, 2 * P, 3 * P, 4 * P):
                for b in range(1, 10):
                    for reach in range(2, 17):
                        c1 = _add(p, d, a)
                        c2 = _add(c1, vdir, b * P)
                        t = _add(c2, d, reach * P)
                        end = _add(t, d, P)
                        r = self._two(key, ref, t, "down") if down else self._two(key, ref, (t[0], snap(t[1] - 7.62)), "down")
                        joint, railp = ("1", "2") if down else ("2", "1")
                        rp = r.pin(railp)
                        path = [p, c1, c2, end]
                        ops = [("wire", path), ("junction", t), ("part", r), ("power", to, rp, 0)]
                        items = self._wire_items(path, inst.ref) + self._part_items(r) + self._power_items(to, rp)
                        if net:
                            ops.append(("label", name, end, d))
                            items.append(("label", _label_box(name, end, d), None))
                        if self._clear(items, anchor=inst.ref):
                            self._commit(ops, items)
                            self._connect(name, ref, joint)
                            self._connect(to, ref, railp)
                            return True
        return False

    def _dogleg_series(self, inst, p, d, key, ref, net, before):
        """Out, down (or up) to a clear row, then out through the part: how a person clears a busy row."""
        for vdir in (DOWN, UP):
            for a in (P, 2 * P, 3 * P, 4 * P, 5 * P):
                for b in range(1, 10):
                    for lead in range(2, 13):
                        c1 = _add(p, d, a)
                        c2 = _add(c1, vdir, b * P)
                        s0 = _add(c2, d, lead * P)
                        r = self._two(key, ref, s0, _dir_name(d))
                        end = _add(r.pin("2"), d, P)
                        path = [p, c1, c2, s0]
                        ops = [("wire", path), ("part", r), ("wire", [r.pin("2"), end]), ("label", net, end, d)]
                        items = self._wire_items(path, inst.ref) + self._part_items(r) + self._wire_items([r.pin("2"), end], ref)
                        items.append(("label", _label_box(net, end, d), None))
                        if before:
                            lp = _add(c2, d, 1.27)
                            if lead * P < font.ink_width(before) + 2.54:
                                continue
                            ops.append(("llabel", before, lp, d))
                            items.append(("label", _label_box(before, lp, d, flag=False), None))
                        if self._clear(items, anchor=inst.ref):
                            self._commit(ops, items)
                            return True
        return False

    def _draw_series(self, rq):
        pq = rq.get("pull")
        if pq is not None:
            if self._draw_series_pull(rq, pq):
                return
            rest = dict(rq)
            rest.pop("pull")
            self._draw_series(rest)
            self._draw_pull(pq)
            return
        inst, pin, key, net, ref, before = rq["inst"], rq["pin"], rq["key"], rq["net"], rq["ref"], rq["before"]
        p, d = inst.pin(pin), inst.pin_dir(pin)
        near = before or f"{inst.ref}_{pin}"
        self._connect(near, inst.ref, pin)
        self._connect(near, ref, "1")
        self._connect(net, ref, "2")
        leads = sorted({math.ceil(((font.ink_width(before) + 2 * P) if before else P) / P) * P} | {k * P for k in range(1, 17)})
        if before:
            leads = [l for l in leads if l >= math.ceil((font.ink_width(before) + 2 * P) / P) * P]
        for lead in leads:
            a = _add(p, d, lead)
            r = self._two(key, ref, a, _dir_name(d))
            b = r.pin("2")
            end = _add(b, d, P)
            ops = [("wire", [p, a]), ("part", r), ("wire", [b, end]), ("label", net, end, d)]
            items = self._wire_items([p, a], inst.ref) + self._part_items(r) + self._wire_items([b, end], ref)
            items.append(("label", _label_box(net, end, d), None))
            if before:
                lp = _add(p, d, 1.27)
                ops.append(("llabel", before, lp, d))
                items.append(("label", _label_box(before, lp, d, flag=False), None))
            if self._clear(items, anchor=inst.ref):
                self._commit(ops, items)
                return
        if d[1] == 0 and self._dogleg_series(inst, p, d, key, ref, net, before):
            return
        # no room in line: the pin's side labelled, the part aside between the two labels
        self._draw_net({"inst": inst, "pin": pin, "name": near, "pins": [pin]}, record=False)
        self._aside_series(key, ref, near, net, f"near {inst.ref} pin {pin}")
        self._keep_near(inst, [pin], [ref], 5.0)
        self.page.crowded.append(f"{inst.ref} pin {pin}: {ref} drawn beside the part {getattr(self, 'why', '')}")

    def _draw_series_pull(self, rq, pq):
        """A part in line with the pin and a pull on the same line, the way a gate is drawn: the pin, a
        junction with the pull-down (or pull-up) hanging from it, then the series part and its net."""
        inst, pin, key, net, ref = rq["inst"], rq["pin"], rq["key"], rq["net"], rq["ref"]
        before = rq["before"] or pq.get("net")
        pkey, pref, to = pq["key"], pq["ref"], pq["to"]
        p, d = inst.pin(pin), inst.pin_dir(pin)
        down = is_ground(to)
        near = before or f"{inst.ref}_{pin}"
        first = math.ceil(((font.ink_width(before) + 2 * P) if before else 2 * P) / P) * P
        for reach in [first + k * P for k in range(0, 10)]:
            t = _add(p, d, reach)
            for lead in (2 * P, 3 * P, 4 * P):
                a = _add(t, d, lead)
                r = self._two(key, ref, a, _dir_name(d))
                b = r.pin("2")
                end = _add(b, d, P)
                sides = [None] if d[1] == 0 else [RIGHT, LEFT]
                for side in sides:
                    if d[1] == 0:                           # a horizontal line: the pull hangs below (or stands above)
                        q = self._two(pkey, pref, t, "down") if down else self._two(pkey, pref, (t[0], snap(t[1] - 7.62)), "down")
                        joint, railp = ("1", "2") if down else ("2", "1")
                        extra = []
                    else:                                   # a vertical line: the pull goes out sideways
                        q = self._two(pkey, pref, _add(t, side, P), _dir_name(side))
                        joint, railp = "1", "2"
                        extra = [[t, q.pin("1")]]
                    rp = q.pin(railp)
                    ops = [("wire", [p, a]), ("junction", t), ("part", r), ("wire", [b, end]), ("label", net, end, d),
                           ("part", q), ("power", to, rp, 0)] + [("wire", w) for w in extra]
                    items = self._wire_items([p, a], inst.ref) + self._part_items(r) + self._wire_items([b, end], ref)
                    items.append(("label", _label_box(net, end, d), None))
                    items += self._part_items(q) + self._power_items(to, rp)
                    for w in extra:
                        items += self._wire_items(w, pref)
                    if before:
                        lp = _add(p, d, 1.27)
                        ops.append(("llabel", before, lp, d))
                        items.append(("label", _label_box(before, lp, d, flag=False), None))
                    if self._clear(items, anchor=inst.ref):
                        self._commit(ops, items)
                        self._connect(near, inst.ref, pin)
                        self._connect(near, ref, "1")
                        self._connect(net, ref, "2")
                        self._connect(near, pref, joint)
                        self._connect(to, pref, railp)
                        return True
        return False

    def _indicator_chain(self, rq, start, d):
        """The resistor and the LED in line from `start` along d, anode toward the resistor, then ground."""
        rref, dref = rq["refs"]
        r = self._two(rq["rkey"], rref, start, _dir_name(d))
        anode = _add(r.pin("2"), d, P)
        led = self._two(rq["dkey"], dref, _add(anode, d, 7.62), _dir_name((-d[0], -d[1])))
        k = led.pin("1")
        end = _add(k, d, P)
        ops = [("part", r), ("wire", [r.pin("2"), led.pin("2")]), ("part", led), ("wire", [k, end]), ("power", rq["gnd"], end, 0)]
        items = self._part_items(r) + self._wire_items([r.pin("2"), led.pin("2")], rref)
        items += self._part_items(led) + self._wire_items([k, end], dref) + self._power_items(rq["gnd"], end)
        return ops, items

    def _draw_indicator(self, rq):
        inst, pin, (rref, dref), gnd = rq["inst"], rq["pin"], rq["refs"], rq["gnd"]
        p, d = inst.pin(pin), inst.pin_dir(pin)
        a_net, k_net = f"{inst.ref}_{pin}", f"{rref}_{dref}"
        self._connect(a_net, inst.ref, pin)
        self._connect(a_net, rref, "1")
        self._connect(k_net, rref, "2")
        self._connect(k_net, dref, "2")
        self._connect(gnd, dref, "1")
        if d[1] != 0:
            self.page.crowded.append(f"{inst.ref} pin {pin}: indicator needs a sideways pin")
        paths = [[p, _add(p, d, lead * P)] for lead in range(1, 14)]
        if d[1] == 0:                          # out, down (or up) to a clear row, then out through the pair
            paths += [[p, c1, c2, _add(c2, d, lead * P)] for vdir in (DOWN, UP) for a in (P, 2 * P, 3 * P, 4 * P, 5 * P)
                      for c1 in [_add(p, d, a)] for b in range(1, 10) for c2 in [_add(c1, vdir, b * P)] for lead in range(2, 13)]
        for path in paths:
            ops, items = self._indicator_chain(rq, path[-1], d)
            if self._clear(self._wire_items(path, inst.ref) + items, anchor=inst.ref):
                self._commit([("wire", path)] + ops, self._wire_items(path, inst.ref) + items)
                return
        # no room in line: the pin labelled, the resistor and LED aside from the same label
        self._draw_net({"inst": inst, "pin": pin, "name": a_net, "pins": [pin]}, record=False)

        def make(x, y):
            a, s0 = (x, y), _add((x, y), RIGHT, P)
            ops, items = self._indicator_chain(rq, s0, RIGHT)
            return ([("wire", [a, s0]), ("label", a_net, a, LEFT)] + ops,
                    self._wire_items([a, s0], rref) + [("label", _label_box(a_net, a, LEFT), None)] + items)
        self._commit(*self._free_spot(self._beside(make, f"near {inst.ref} pin {pin}")))
        self.page.crowded.append(f"{inst.ref} pin {pin}: indicator drawn beside the part")

    def _crystal_plan(self, rq, top, bot, d, reach, owner):
        """The crystal standing between the two lines `reach` pitches out along d, each load capacitor out from its
        end to ground: (ops, items)."""
        ref, (c1, c2), gnd = rq["ref"], rq["cap_refs"], rq["gnd"]
        x = snap(max(top[0] * d[0], bot[0] * d[0]) * d[0] + d[0] * reach * P)
        mid = snap((top[1] + bot[1]) / 2)
        ytop, ybot = snap(mid - 3.81), snap(mid + 3.81)
        y = self._two(rq["ykey"], ref, (x, ytop), "down")
        nt, nb = y.pin("1"), y.pin("2")
        xj = snap(x - d[0] * 2 * P)                      # the lines jog before the crystal, then meet its pins
        ops = [("wire", [top, (xj, top[1]), (xj, nt[1]), nt]) if top[1] != nt[1] else ("wire", [top, nt]),
               ("wire", [bot, (xj, bot[1]), (xj, nb[1]), nb]) if bot[1] != nb[1] else ("wire", [bot, nb]), ("part", y)]
        lines = self._wire_items(ops[0][1], owner) + self._wire_items(ops[1][1], owner)
        caps = []
        for node, cref, key, where in ((nt, c1, rq["caps"][0], "fields_above"), (nb, c2, rq["caps"][1], "fields_below")):
            c = self._two(key, cref, _add(node, d, P), _dir_name(d), **{where: True})
            g = _add(c.pin("2"), d, P)
            ops += [("wire", [node, c.pin("1")]), ("junction", node), ("part", c), ("wire", [c.pin("2"), g]), ("power", gnd, g, 0)]
            caps += self._wire_items([node, c.pin("1")], ref) + self._part_items(c) + self._wire_items([c.pin("2"), g], cref)
            caps += self._power_items(gnd, g)
        # name above, value below, centred; when one reaches a capacitor's text (a long value: "24MHz 8pF"), it starts
        # at the crystal and runs away from the capacitors
        edge, away = snap(x + d[0] * 2.54), ("left" if d[0] < 0 else "right")
        above, below = snap(ytop - 3.81), snap(ybot + 3.81)
        lower, higher = snap(below + 2 * P), snap(above - 2 * P)            # past the capacitors' own text
        for y.ref_at, y.val_at in (((x, above, None), (x, below, None)), ((x, above, None), (edge, below, away)),
                                   ((edge, above, away), (edge, below, away)), ((x, above, None), (x, lower, None)),
                                   ((x, higher, None), (x, lower, None)), ((edge, higher, away), (edge, lower, away))):
            items = lines + self._part_items(y) + caps
            if self._self_clear(items):
                break
        return ops, items

    def _draw_crystal(self, rq):
        inst, (pa, pb), gnd = rq["inst"], rq["pins"], rq["gnd"]
        a, b = inst.pin(pa), inst.pin(pb)
        d = inst.pin_dir(pa)
        if d[1] != 0 or inst.pin_dir(pb) != d:
            self.page.crowded.append(f"{inst.ref}: crystal pins must be on one side")
            return
        top, bot = (a, b) if a[1] < b[1] else (b, a)
        tpin, bpin = (pa, pb) if a[1] < b[1] else (pb, pa)
        tn, bn = f"{inst.ref}_{tpin}", f"{inst.ref}_{bpin}"
        ref, (c1, c2) = rq["ref"], rq["cap_refs"]
        for n, r_, q in ((tn, inst.ref, tpin), (bn, inst.ref, bpin), (tn, ref, "1"), (bn, ref, "2"), (tn, c1, "1"), (bn, c2, "1"),
                         (gnd, c1, "2"), (gnd, c2, "2")):
            self._connect(n, r_, q)
        for reach in range(3, 16):
            ops, items = self._crystal_plan(rq, top, bot, d, reach, inst.ref)
            if self._clear(items, anchor=inst.ref):
                self._commit(ops, items)
                return
        # no room beside the pins: each pin labelled, the crystal and its capacitors aside between the two labels
        self._draw_net({"inst": inst, "pin": tpin, "name": tn, "pins": [tpin]}, record=False)
        self._draw_net({"inst": inst, "pin": bpin, "name": bn, "pins": [bpin]}, record=False)

        def make(x, y):
            t, b_ = (x, y), (x, snap(y + 7.62))
            ops, items = self._crystal_plan(rq, t, b_, RIGHT, 3, None)
            return (ops + [("label", tn, t, LEFT), ("label", bn, b_, LEFT)],
                    items + [("label", _label_box(tn, t, LEFT), None), ("label", _label_box(bn, b_, LEFT), None)])
        self._commit(*self._free_spot(self._beside(make, f"near {inst.ref} pins {tpin}, {bpin}")))
        self._keep_near(inst, [tpin, bpin], [ref, c1, c2], 5.0)
        self.page.crowded.append(f"{inst.ref}: crystal drawn beside the part {getattr(self, 'why', '')}")

    # ------------------------------------------------------------------ parts drawn aside (a label at each end)
    def _beside(self, make, where):
        """A drawing set aside from the pin it serves, with a line under it saying which pin ("near U1 pin P12"):
        joined to the pin only by a net's name, nothing else on the sheet says where its parts belong."""
        if not where:
            return make

        def made(x, y):
            ops, items = make(x, y)
            at = (snap(min(b[0] for _, b, _ in items)), _snap_up(max(b[3] for _, b, _ in items) + 1.27))
            return ops + [("text", where, at)], items + [("text", _text_box(where, at), None)]
        return made

    def _keep_near(self, inst, pins, refs, max_mm, caps=None):
        """The layout keeps parts drawn aside at their pin (a placement constraint, rewritten each run). Under a BGA the
        small capacitors (2.2 uF and less) go on the bottom, beside their ball's via: there is no room on top."""
        bga = _is_bga(inst)
        bottom = bga and _project_limit("assembly_sides") != "top only"
        for i, r in enumerate(refs):
            where = ", ".join(str(p) for p in pins)
            self.page.near.append({"kind": "near", "ref": r, "to": f"{inst.ref}.{pins[0]}", "max_mm": max_mm,
                                   "why": (f"under {inst.ref} (BGA), beside ball {where}'s via" if bga else
                                           f"drawn beside {inst.ref} on the schematic, for pin {where}")})
            f = _farads(self._cat(caps[i]).value) if bottom and caps and i < len(caps) else None
            if f is not None and f <= 2.2e-6:
                self.page.near.append({"kind": "side", "ref": r, "side": "B",
                                       "why": f"decoupling for {inst.ref} ball {where}: on the bottom, under the BGA"})

    WIDE = 240.0        # mm: drawings set aside go right of the block's parts until it is this wide, then on shelves below

    def _free_spot(self, make, strict=False):
        """Where make(x, y) -> (ops, items) is clear: right of what is drawn, at the top, while the block stays within
        WIDE (or its parts' own width); then the first gap on the shelves below, right of the parts, top to bottom and
        left to right; then below everything. A BGA's supplies, each with its capacitors aside, used to run off the
        paper in one row (660 mm). None when the drawing lands on itself and `strict`: no place can help."""
        bb = self.bbox()
        ops, items = make(0.0, 0.0)
        if not self._self_clear(items):
            return None if strict else make(snap(bb[2] + 12.7), snap(bb[1]))
        u = (min(b[0] for _, b, _ in items), min(b[1] for _, b, _ in items),       # its reach from (x, y)
             max(b[2] for _, b, _ in items), max(b[3] for _, b, _ in items))
        parts = [self.parts[r].bbox() for r in getattr(self, "main", []) if r in self.parts]
        px1 = max(b[2] for b in parts) if parts else bb[0]
        right = bb[0] + max(self.WIDE, px1 - bb[0] + 40.0, u[2] - u[0] + 20.0)
        top = snap(bb[1] + 2 * P)
        for col in range(0, 120):                                   # right of everything, at the top
            x = snap(bb[2] + 7.62 + col * P)
            if x + u[2] > right:
                break
            ops, items = make(x, top)
            if self._clear(items):
                return ops, items
        g, boxes = 0.9, [b for _, b, _ in self.boxes]               # 0.9: the widest gap two kinds must keep
        y = snap(bb[1] - u[1])
        while y + u[1] <= bb[3]:                                    # shelves: each row's first gap right of the parts
            lo, hi = y + u[1] - g, y + u[3] + g
            spans = sorted((b[0] - g, b[2] + g) for b in boxes if b[1] < hi and b[3] > lo)
            x = _snap_up(px1 + 7.62 - u[0])
            while True:
                for a, b_ in spans:
                    if b_ <= x + u[0]:
                        continue
                    if a >= x + u[2]:
                        break
                    x = _snap_up(b_ - u[0])
                if x + u[2] > right:
                    break
                ops, items = make(x, y)
                if self._clear(items):
                    return ops, items
                x = snap(x + P)                                     # a hair too close after all: on along the row
            y = snap(y + P)
        for col in range(0, 120):                                   # below everything
            ops, items = make(snap(bb[0] + col * 2 * P), snap(bb[3] + 5.08 - u[1]))
            if self._clear(items):
                return ops, items
        return None if strict else make(snap(bb[2] + 12.7), snap(bb[1]))

    def _aside_pull(self, key, ref, to, name, where=None):
        down = is_ground(to)

        def make(x, y):
            top = (x, y)
            r = self._two(key, ref, _add(top, DOWN, P), "down")
            ops = [("part", r), ("wire", [top, r.pin("1")])]
            items = self._part_items(r) + self._wire_items([top, r.pin("1")], ref)
            if down:                                          # the net's label on top, ground below
                ops += [("label", name, top, UP), ("power", to, r.pin("2"), 0)]
                items += [("label", _label_box(name, top, UP), None)] + self._power_items(to, r.pin("2"))
            else:                                             # the rail on top, the net's label below
                bot = _add(r.pin("2"), DOWN, P)
                ops = [("part", r), ("power", to, r.pin("1"), 0), ("wire", [r.pin("2"), bot]), ("label", name, bot, DOWN)]
                items = self._part_items(r) + self._power_items(to, r.pin("1")) + self._wire_items([r.pin("2"), bot], ref)
                items.append(("label", _label_box(name, bot, DOWN), None))
            return ops, items
        self._commit(*self._free_spot(self._beside(make, where)))
        self._connect(name, ref, "1" if down else "2")
        self._connect(to, ref, "2" if down else "1")

    def _aside_series(self, key, ref, near, net, where=None):
        def make(x, y):
            a = (x, y)
            r = self._two(key, ref, _add(a, RIGHT, P), "right")
            b = _add(r.pin("2"), RIGHT, P)
            ops = [("wire", [a, r.pin("1")]), ("part", r), ("wire", [r.pin("2"), b]), ("label", near, a, LEFT), ("label", net, b, RIGHT)]
            items = self._wire_items([a, r.pin("1")], ref) + self._part_items(r) + self._wire_items([r.pin("2"), b], ref)
            items += [("label", _label_box(near, a, LEFT), None), ("label", _label_box(net, b, RIGHT), None)]
            return ops, items
        self._commit(*self._free_spot(self._beside(make, where)))

    def _aside_caps(self, caps, refs, rail, where=None):
        def plan(pitch):
            def make(x, y):
                top = (x, y)
                end = _add(top, RIGHT, pitch * len(caps))
                ops = [("power", rail, top, 0), ("wire", [top, end])]
                items = self._power_items(rail, top) + self._wire_items([top, end])
                for i, key in enumerate(caps):
                    c, o, it = self._cap_down(key, refs[i], _add(top, RIGHT, pitch * (i + 1)))
                    ops += o + ([("junction", _add(top, RIGHT, pitch * (i + 1)))] if i < len(caps) - 1 else [])
                    items += it
                return ops, items
            return make
        for pitch in (7.62, 10.16, 12.7, 15.24):           # wider when a value ("10u 25V") reaches the next capacitor
            got = self._free_spot(self._beside(plan(pitch), where), strict=True)
            if got:
                self._commit(*got)
                return
        self._commit(*self._free_spot(self._beside(plan(15.24), where)))

    # ------------------------------------------------------------------ chains and notes
    def finish(self):
        self.flush()
        self._cross()
        for item in self.later:
            if item[0] == "led":
                self._draw_led(*item[1:])
            elif item[0] == "divider":
                self._draw_divider(*item[1:])
        for item in self.later:                           # decoupling rows set aside: once the group's parts have their places
            if item[0] == "caps":
                self._aside_caps(*item[1:])
        for item in self.later:
            if item[0] == "note":
                self._draw_note(*item[1:])
        self.later = []

    def _draw_led(self, rail, rkey, dkey, refs, gnd):
        rref, dref = refs
        bb = self.bbox()
        x0 = snap(bb[2] + 10.16) if self.boxes else 0
        for k in range(60):
            top = (snap(x0 + k * P), snap(bb[1] + 5.08) if self.boxes else 0)
            r = self._two(rkey, rref, top, "down")
            cath = (top[0], snap(r.pin("2")[1] + 7.62 + 2 * P))
            led = self._two(dkey, dref, cath, "up")
            ops = [("part", r), ("part", led), ("wire", [r.pin("2"), led.pin("2")]), ("power", rail, top, 0),
                   ("power", gnd, led.pin("1"), 0)]
            items = self._part_items(r) + self._part_items(led) + self._wire_items([r.pin("2"), led.pin("2")])
            items += self._power_items(rail, top) + self._power_items(gnd, led.pin("1"))
            if self._clear(items):
                self._commit(ops, items)
                self._connect(rail, rref, "1")
                self._connect(f"{rref}_{dref}", rref, "2")
                self._connect(f"{rref}_{dref}", dref, "2")
                self._connect(gnd, dref, "1")
                return
        raise RuntimeError(f"no room for {rref} and {dref}")

    def _draw_divider(self, top_rail, mid, rkt, rkb, bottom, refs, cap, cap_ref):
        """top rail / R / tap -> label / R / bottom, top to bottom; the filter cap from the tap, beside."""
        r1ref, r2ref = refs
        own = {r1ref, r2ref, cap_ref}
        served = sorted((r, p) for r, p in self.page.nets.get(mid, set()) if r not in own and not re.match(r"(R|C|L|FB)\d", r))
        chip = self.parts.get(served[0][0]) if len(served) == 1 else None
        where = f"near {served[0][0]} pin {served[0][1]}" if chip else None     # which pin the tap feeds, said beside it
        bb = self.bbox()
        x0 = snap(bb[2] + 10.16) if self.boxes else 0
        for k in range(60):
            top = (snap(x0 + k * P), snap(bb[1] + 5.08) if self.boxes else 0)
            r1 = self._two(rkt, r1ref, top, "down", fields_left=True)       # names on the left, the tap goes right
            tap = _add(r1.pin("2"), DOWN, P)
            r2 = self._two(rkb, r2ref, _add(tap, DOWN, P), "down", fields_left=True)
            ops = [("part", r1), ("part", r2), ("power", top_rail, top, 0), ("wire", [r1.pin("2"), tap]), ("wire", [tap, r2.pin("1")]),
                   ("power", bottom, r2.pin("2"), 0), ("junction", tap)]
            items = self._part_items(r1) + self._part_items(r2) + self._power_items(top_rail, top) + self._power_items(bottom, r2.pin("2"))
            items += self._wire_items([r1.pin("2"), tap]) + self._wire_items([tap, r2.pin("1")])
            if cap:
                q = _add(tap, RIGHT, 3 * P)
                c = self._two(cap, cap_ref, _add(q, DOWN, P), "down")
                end = _add(q, RIGHT, 3 * P)
                ops += [("wire", [tap, q]), ("wire", [q, c.pin("1")]), ("wire", [q, end]), ("junction", q), ("part", c),
                        ("power", bottom, c.pin("2"), 0), ("label", mid, end, RIGHT)]
                items += self._wire_items([tap, q]) + self._wire_items([q, c.pin("1")]) + self._wire_items([q, end]) + self._part_items(c)
                items += self._power_items(bottom, c.pin("2")) + [("label", _label_box(mid, end, RIGHT), None)]
            else:
                end = _add(tap, RIGHT, 4 * P)
                ops += [("wire", [tap, end]), ("label", mid, end, RIGHT)]
                items += self._wire_items([tap, end]) + [("label", _label_box(mid, end, RIGHT), None)]
            if where:
                ops, items = self._beside(lambda _x, _y, o=ops, i=items: (o, i), where)(0, 0)
            if self._clear(items):
                self._commit(ops, items)
                if chip:
                    self._keep_near(chip, [served[0][1]], [r for r in (r1ref, r2ref, cap_ref) if r], 5.0)
                self._connect(top_rail, r1ref, "1")
                for ref, pin in ((r1ref, "2"), (r2ref, "1")) + (((cap_ref, "1"),) if cap else ()):
                    self._connect(mid, ref, pin)
                self._connect(bottom, r2ref, "2")
                if cap:
                    self._connect(bottom, cap_ref, "2")
                return
        raise RuntimeError(f"no room for the divider {r1ref} / {r2ref}")

    def _draw_note(self, text, near, why=None, pin=None):
        from .notes import anchor_of
        lines = str(text).split("\n")
        if not near:
            self.page.note_issues.append(f"note with no part to sit beside: \u201c{lines[0][:50]}\u201d (give it near=)")
        if max(len(l) for l in lines) > 72 or len(lines) > 2:
            self.page.note_issues.append(f"long note: \u201c{lines[0][:50]}...\u201d -- one plain line on the sheet; the reasoning in why=")
        if why and near:
            a = anchor_of(near, pin)
            if a:
                self.page.design_notes.append({"anchor": a, "short": str(text), "why": str(why), "sheet": self.page.sheet.filename})
        inst = self.parts.get(near) if isinstance(near, str) else None
        if inst is not None and pin is not None:            # beside one pin: the note sits at the pin's end
            try:
                px, py = inst.pin(str(pin))
                x0, y0, x1, y1 = px - 1.27, py - 1.27, px + 1.27, py + 1.27
            except Exception:
                x0, y0, x1, y1 = inst.bbox()
        else:
            x0, y0, x1, y1 = inst.bbox() if inst else self.bbox()
        tb = _text_box(text, (0, 0))
        w, h = tb[2] - tb[0], tb[3] - tb[1]
        spots = [(x0, y1 + 3.81), (x1 + 3.81, y0), (x0 - w - 3.81, y0), (x0, y0 - h - 3.81)]
        spots += [(x0 + dx, y1 + 3.81 + k * P) for k in range(1, 16) for dx in (0, -w / 2)]
        spots += [(x1 + 3.81 + k * P, y0 + dy) for k in range(1, 16) for dy in (0, P * 2)]
        for sx, sy in spots:
            at = (snap(sx), snap(sy))
            box = _text_box(text, at)
            if self._clear([("text", box, None)]):
                self._commit([("text", text, at)], [("text", box, None)])
                return
        # nowhere beside it: under everything the group has drawn, which is always clear
        gx0, gy0, gx1, gy1 = self.bbox()
        for sx in (x0, gx0):
            for k in range(0, 40):
                at = (snap(sx), snap(gy1 + 3.81 + k * P))
                box = _text_box(text, at)
                if self._clear([("text", box, None)]):
                    self._commit([("text", text, at)], [("text", box, None)])
                    self.page.crowded.append(f"note near {near}: below the group")
                    return
        at = (snap(gx0), snap(gy1 + 3.81 + 40 * P))
        self._commit([("text", text, at)], [("text", _text_box(text, at), None)])
        self.page.crowded.append(f"note near {near}: crowded")


def _project_limit(key):
    """A design limit of the project the script runs in (tracewright.json constraints), or None."""
    try:
        from .. import env, constraints
        return constraints.get(env.project().cfg).get(key)
    except Exception:
        return None


def _project_conventions():
    """The schematic conventions of the project the script runs in (tracewright.json), or the defaults."""
    from . import conventions
    try:
        from .. import env
        cfg = env.project().cfg
    except Exception:
        cfg = {}
    c = conventions.get(cfg)
    c["_chosen_paper"] = "paper" in conventions.chosen(cfg)
    return c


class Page:
    """One sheet laid out by rule: groups of parts and patterns, placed in reading order."""

    SIZES = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420)}

    def __init__(self, design, sheet, base=100, catalog=None, conventions=None):
        self.d, self.sheet, self.base = design, sheet, base
        self.cat = catalog or {}
        self.conv = conventions if conventions is not None else _project_conventions()
        if self.conv.get("paper") and self.conv.get("_chosen_paper"):
            sheet.paper = self.conv["paper"]
        self.groups = []
        self.nets = {}                   # net -> {(ref, pin)}: what the drawing must connect
        self.crowded = []
        self.design_notes = []           # the reasoning behind parts, pins and nets, kept off the sheet (notes.py)
        self.near = []                   # parts drawn aside, kept at their pin in the layout (placeplan)
        self.note_issues = []            # notes too long, or with no part to sit beside: told to Claude by finish()
        self._n = {}
        self.placed = None
        if not hasattr(design, "pages"):
            design.pages = []
        design.pages.append(self)

    def ref(self, prefix, given=None):
        """A part's reference: the one the script gave (kept, so numbering passes it by), else the next free one.
        Numbering once handed out D101 to an LED while the script had named a diode D101: KiCad took them for one
        part and joined their nets (VSYS to GND)."""
        taken = self.d.__dict__.setdefault("_refs", set())
        if given:
            taken.add(given)
            return given
        while True:
            if self.conv.get("designators") == "sequential":       # R1, R2, ... across the whole design
                seq = self.d.__dict__.setdefault("_seq", {})
                seq[prefix] = seq.get(prefix, 0) + 1
                ref = f"{prefix}{seq[prefix]}"
            else:
                self._n[prefix] = self._n.get(prefix, 0) + 1
                ref = f"{prefix}{self.base + self._n[prefix]}"
            if ref not in taken:
                taken.add(ref)
                return ref

    def group(self, title):
        g = Group(self, title)
        self.groups.append(g)
        return g

    def layout(self, margin=15.24, gap=10.16):
        """Place the groups on the sheet in reading order, each in its titled block (wide enough for its
        title): every block goes to the highest, then leftmost, spot where it fits (beside the blocks
        above, or in the room under a short one), clear of the title block in the corner; the sheet
        grows to A3, then A2, only when they do not fit."""
        import time as _time
        t0 = _time.time()
        for g in self.groups:
            g.finish()
        pads = (6.35, 10.16, 6.35, 6.35)             # left, top (the title), right, bottom
        dims = []
        for g in self.groups:
            x0, y0, x1, y1 = g.bbox()
            w = max(x1 - x0 + pads[0] + pads[2], len(g.title) * 2.0 * 0.95 + 10.16)
            dims.append((x0, y0, w, y1 - y0 + pads[1] + pads[3], x1 - x0))
        order = [self.sheet.paper] + [p for p in ("A4", "A3", "A2") if p != self.sheet.paper and
                                      self.SIZES[p][0] >= self.SIZES.get(self.sheet.paper, (0, 0))[0]]
        spots = None
        for paper in order:
            W, H = self.SIZES.get(paper, self.SIZES["A4"])
            spots = self._pack(dims, W, H, margin, gap)
            if spots is not None:
                self.sheet.paper = paper
                break
        if spots is None:                               # too much for A2 as well: rows, running off the bottom
            W, H = self.SIZES["A2"]
            spots, x, y, row_h = [], margin, margin, 0
            for (_, _, w, h, _) in dims:
                if x + w > W - margin and x > margin:
                    x, y, row_h = margin, y + row_h + gap, 0
                spots.append((x, y))
                x += w + gap
                row_h = max(row_h, h)
            self.sheet.paper = "A2"
        self.placed = []
        for g, (gx0, gy0, w, h, cw), (x, y) in zip(self.groups, dims, spots):
            ox = x + pads[0] - gx0 + (w - pads[0] - pads[2] - cw) / 2      # content centred in a block widened for its title
            oy = y + pads[1] - gy0
            ox, oy = round(ox / P) * P, round(oy / P) * P
            self.placed.append((g, (ox, oy), (snap(x), snap(y), snap(x + w), snap(y + h))))
        from . import progress
        progress(f"laid out {self.sheet.filename}: {_time.time() - t0:.1f} s, {len(self.crowded)} crowded spots, {self.sheet.paper}")
        return self

    @staticmethod
    def _pack(dims, W, H, margin, gap, title=(115.0, 38.0)):
        """Top-left positions for blocks (w, h) in order on a W x H sheet, or None when they do not fit."""
        right, bottom = W - margin, H - margin
        tb = (right - title[0], bottom - title[1], right, bottom)        # KiCad's title block, bottom right
        placed = []

        def free(x, y, w, h):
            if x < margin - 1e-6 or y < margin - 1e-6 or x + w > right + 1e-6 or y + h > bottom + 1e-6:
                return False
            if x < tb[2] and x + w > tb[0] and y < tb[3] and y + h > tb[1]:
                return False
            return all(x + w + gap <= bx0 + 1e-6 or bx1 + gap <= x + 1e-6 or y + h + gap <= by0 + 1e-6 or by1 + gap <= y + 1e-6
                       for bx0, by0, bx1, by1 in placed)
        out = []
        for (_, _, w, h, _) in dims:
            cands = {(margin, margin)}
            for bx0, by0, bx1, by1 in placed:
                cands |= {(bx1 + gap, by0), (bx0, by1 + gap), (margin, by1 + gap), (bx1 + gap, margin)}
            best = None
            for x, y in sorted(cands, key=lambda c: (round(c[1], 3), round(c[0], 3))):
                if free(x, y, w, h):
                    best = (x, y)
                    break
            if best is None:
                return None
            out.append(best)
            placed.append((best[0], best[1], best[0] + w, best[1] + h))
        return out

    def _label_kinds(self, global_nets, supplies_here):
        """Global or local for every label on the sheet, one kind for labels that touch: a local label's text sits
        above its wire and a global label's flag is centred on its wire, so the two on neighbouring pins overlap. The
        layout kept room for either kind where a net's sheets were not known yet; here, if any label in a touching
        group must be global, the group is (a global label named once is harmless: KiCad's ERC does not flag it)."""
        labs = []
        for gi, (g, (ox, oy), _) in enumerate(self.placed):
            for oi, op in enumerate(g.ops):
                if op[0] == "label":
                    b = _label_box(op[1], (op[2][0] + ox, op[2][1] + oy), op[3], flag="either")
                    labs.append(((gi, oi), _label_kind(op, global_nets, supplies_here), b))
        parent = list(range(len(labs)))

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        order = sorted(range(len(labs)), key=lambda i: labs[i][2][0])
        for a_, i in enumerate(order):                     # sweep in x: only boxes that reach each other
            bi = labs[i][2]
            for j in order[a_ + 1:]:
                bj = labs[j][2]
                if bj[0] > bi[2] + 0.05:
                    break
                if _hit(bi, bj, 0.05):
                    parent[root(i)] = root(j)
        glob = {}
        for i, (_, g_, _) in enumerate(labs):
            glob[root(i)] = glob.get(root(i), False) or g_
        return {k: glob[root(i)] for i, (k, _, _) in enumerate(labs)}

    def emit(self, global_nets):
        supplies_here = {op[1] for g in self.groups for op in g.ops if op[0] == "power"}       # rails with a symbol on this sheet
        """Draw the laid-out groups onto the sheet (called by Design.write, once the whole design is known)."""
        if self.placed is None:
            self.layout()
        sh = self.sheet
        final = self._label_kinds(global_nets, supplies_here)
        for gi, (g, (ox, oy), (x0, y0, x1, y1)) in enumerate(self.placed):
            sh.block(x0, y0, x1, y1, g.title)
            mv = lambda p, ox=ox, oy=oy: (snap(p[0] + ox), snap(p[1] + oy))
            for oi, op in enumerate(g.ops):
                kind = op[0]
                if kind == "part":
                    inst = op[1]
                    ra = (mv(inst.ref_at[:2]) + tuple(inst.ref_at[2:])) if inst.ref_at else None
                    va = (mv(inst.val_at[:2]) + tuple(inst.val_at[2:])) if inst.val_at else None
                    sh.add(inst.lib, inst.ref, inst.value, mv(inst.at), rot=inst.rot, unit=inst.unit,
                           footprint=inst.footprint, fields=inst.fields, fields_left=inst.fields_left,
                           fields_above=inst.fields_above, fields_below=inst.fields_below, ref_at=ra, val_at=va,
                           mirror=getattr(inst, "mirror", None))
                elif kind == "wire":
                    sh.wire(*[mv(p) for p in op[1]])
                elif kind == "junction":
                    sh.junction(mv(op[1]))
                elif kind == "nc":
                    sh.nc(mv(op[1]))
                elif kind == "power":
                    _, rail, pt, rot = op
                    sh.power(power_symbol(rail), mv(pt), rot=rot, value=rail)
                elif kind == "flag":
                    sh.power(stock("power", "PWR_FLAG"), mv(op[1]))
                    sh.junction(mv(op[1]))
                elif kind == "label":
                    name, pt, d = op[1], op[2], op[3]
                    if final[(gi, oi)]:
                        q = mv(pt)
                        sh.items.append(["global_label", kisch.Q(name), ["shape", "bidirectional"], ["at", q[0], q[1], _rot_of(d)],
                                         ["fields_autoplaced", "yes"],
                                         kisch.effects(justify="left" if _rot_of(d) in (0, 90) else "right"),
                                         ["uuid", kisch.Q(kisch.uid())]])
                    else:
                        sh.label(name, mv(pt), _rot_of(d))
                elif kind == "llabel":
                    _, name, pt, d = op
                    sh.label(name, mv(pt), _rot_of(d))
                elif kind == "text":
                    sh.text(op[1], mv(op[2]), size=1.27)


def _label_kind(op, global_nets, supplies_here):
    tag = op[4] if len(op) > 4 else None
    if tag == "rail?":
        return op[1] not in supplies_here
    return op[1] in global_nets or tag == "global"


def resolve(design):
    """Nets named on more than one sheet: they get global labels."""
    where = {}
    for pg in getattr(design, "pages", []):
        for g in pg.groups:
            for op in g.ops:
                if op[0] == "label" and (len(op) < 5 or op[4] != "rail?"):   # a supply's label: local if its symbol is there
                    where.setdefault(op[1], set()).add(pg.sheet.filename)
    return {n for n, s in where.items() if len(s) > 1}


def intended(design):
    """Every connection the pages asked for: {net: [[ref, pin], ...]}."""
    nets = {}
    for pg in getattr(design, "pages", []):
        for n, pins in pg.nets.items():
            nets.setdefault(n, set()).update(pins)
    return {n: sorted([list(p) for p in pins]) for n, pins in nets.items()}


def check_netlist(netlist, want):
    """What KiCad's netlist does differently from what was asked: pins missing, a net split in two,
    two nets joined."""
    problems, of = [], {}
    for name, pins in want.items():
        nets = set()
        for r, p in pins:
            n = netlist.pin.get((r, str(p)))
            if n is None:
                problems.append(f"{r}.{p} ({name}) is not in the netlist")
            else:
                nets.add(n)
        if len(nets) > 1:
            problems.append(f"{name} is split in {len(nets)} nets")
        for n in nets:
            of.setdefault(n, set()).add(name)
    for n, names in of.items():
        if len(names) > 1:
            problems.append(f"{', '.join(sorted(names))} are joined")
    return problems
