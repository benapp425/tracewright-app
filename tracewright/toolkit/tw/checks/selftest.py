"""Planted-fault self-test: every check must catch a fault planted in a copy of the fixture board.

    ./tw selftest [ids...] [-v]

The fixture (tw/fixtures/demo, the example USB-C ATtiny85 board) passes the checks. For each case
the fault is planted either in the files of a scratch copy (for what KiCad itself reads: ERC, DRC,
the plotted sheets, the pick-and-place) or in the loaded board / netlist (everything else), and the
check must report a finding that it did not report on the clean board. A check that cannot fail is
not evidence.
"""
import os, sys, re, copy, json, shutil, tempfile, time
from . import load_all, REGISTRY, Finding, NotApplicable
from .context import Context
from .. import env, geom
from ..board import Track, Via, Footprint, Pad, Zone, Shape

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "demo")


def scratch_project(tmp):
    root = os.path.join(tmp, "demo")
    shutil.copytree(FIXTURE, root)
    return env.Project(root)


def _ctx(p):
    return Context(p, refresh=True, offline=False)


# ----------------------------------------------------------------------------- file-level plants
def _edit(path, fn):
    with open(path, encoding="utf-8") as f:
        t = f.read()
    t2 = fn(t)
    assert t2 != t, f"plant did not change {os.path.basename(path)}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(t2)


def plant_erc(p):
    # drop the no-connect flag on U2 pin 6: KiCad reports the unconnected pin
    sch = os.path.join(p.hw, "mcu.kicad_sch")
    _edit(sch, lambda t: re.sub(r"\n\t\(no_connect\n\t\t\(at [^)]*\)\n\t\t\(uuid \"[^\"]*\"\)\n\t\)", "", t, count=1))


def plant_drc(p):
    from ..pcb import client
    b = _board(p)
    c1 = b.footprints["C1"]
    client.apply(p, [{"op": "move", "ref": "C3", "x": c1.x + 0.3, "y": c1.y, "rot": 90}], live=False)


def plant_render(p):
    # a note written over R1's value text on the power sheet
    sch = os.path.join(p.hw, "power.kicad_sch")
    from ..schematic import Hierarchy
    h = Hierarchy.load(p.sch)
    sh, s = h.find_symbol("R1")
    x, y, _, _ = s.field_at["Value"]
    note = (f'\t(text "PLANTED NOTE OVER A VALUE"\n\t\t(exclude_from_sim no)\n\t\t(at {x - 1.27:.2f} {y:.2f} 0)\n'
            f'\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left)\n\t\t)\n'
            f'\t\t(uuid "0badc0de-0000-4000-a000-000000000001")\n\t)\n')
    _edit(sch, lambda t: t.replace("\n\t(symbol\n", "\n" + note + "\t(symbol\n", 1))


def plant_wiring(p):
    # a second wire laid along an existing one, overlapping it (KiCad joins them silently)
    sch = os.path.join(p.hw, "power.kicad_sch")
    from ..schematic import Hierarchy
    h = Hierarchy.load(p.sch)
    sf = [s for s in h.sheets if s.filename == "power.kicad_sch"][0].sf
    w = max(sf.wires, key=lambda w: abs(w[0][0] - w[-1][0]) + abs(w[0][1] - w[-1][1]))
    (x0, y0), (x1, y1) = w[0], w[-1]
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    wire = (f'\t(wire\n\t\t(pts\n\t\t\t(xy {mx:.2f} {my:.2f}) (xy {x1 + (x1 - x0):.2f} {y1 + (y1 - y0):.2f})\n\t\t)\n'
            f'\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n\t\t(uuid "0badc0de-0000-4000-a000-000000000002")\n\t)\n')
    _edit(sch, lambda t: t.replace("\n\t(symbol\n", "\n" + wire + "\t(symbol\n", 1))


def plant_style(p):
    # a ground symbol turned upside down on the power sheet
    sch = os.path.join(p.hw, "power.kicad_sch")
    _edit(sch, lambda t: re.sub(r'(\(lib_id "[^"]*:GND"\)\s*\(at [\d.]+ [\d.]+ )0\)', r"\g<1>180)", t, count=1))


def plant_text_in_sheet(p):
    # the kind of text an agent writes inside a sheet symbol on the root
    note = ('\t(text "Sheet 2 / 14 components\\nOpen to inspect named pin-to-net connections."\n\t\t(exclude_from_sim no)\n'
            '\t\t(at 45.72 50.8 0)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t\t(justify left)\n\t\t)\n'
            '\t\t(uuid "0badc0de-0000-4000-a000-000000000002")\n\t)\n')
    _edit(p.sch, lambda t: t.replace("\n\t(sheet\n", "\n" + note + "\t(sheet\n", 1))


def plant_text_narration(p):
    # a note that explains how KiCad works, on the cover
    note = ('\t(text "Use the NC marks as intentional unused pins. Global labels with the same name are electrically connected."\n'
            '\t\t(exclude_from_sim no)\n\t\t(at 25.4 180.34 0)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n'
            '\t\t\t(justify left)\n\t\t)\n\t\t(uuid "0badc0de-0000-4000-a000-000000000003")\n\t)\n')
    _edit(p.sch, lambda t: t.replace("\n\t(sheet\n", "\n" + note + "\t(sheet\n", 1))


def plant_dru(p):
    # one misspelt constraint: KiCad ignores the whole file without a word
    path = os.path.splitext(p.pcb)[0] + ".kicad_dru"
    with open(path, "a", encoding="utf-8") as f:
        f.write('\n(rule "planted typo"\n  (constraint clearanc (min 0.2mm)))\n')


def plant_cpl(p):
    os.remove(p.path("sourcing", "jlc-placement.json"))          # no corrections: U2 is 90 degrees out


# ----------------------------------------------------------------------------- in-memory plants
def _board(p):
    from ..board import Board
    return Board.load(p.pcb)


def mem_conventions(ctx):
    # the project writes values like 4k7; the demo's Rd resistors are written 5.1k
    ctx.cfg = {**(ctx.cfg or {}), "schematic": {"values": "iec"}}


def mem_nets_model(ctx):
    # a current declared for a net that was renamed: the declaration reaches nothing
    ctx.cfg = {**(ctx.cfg or {}), "nets": {"VCC_OLD": {"kind": "power", "voltage": 5, "current": 1}}}


def mem_placement(ctx):
    fp = ctx.board.footprints["U1"]
    _shift(fp, 200.0 - fp.x, 0)


def _shift(fp, dx, dy):
    fp.x += dx
    fp.y += dy
    for pd in fp.pads:
        pd.x += dx
        pd.y += dy
        pd.polys = [[(x + dx, y + dy) for x, y in pl] for pl in pd.polys]
        pd.poly = pd.polys[0]
    for s in fp.shapes:
        s.pts = [(x + dx, y + dy) for x, y in s.pts]
    for t in fp.texts:
        t.x += dx
        t.y += dy


def mem_polarity(ctx):
    d1 = ctx.board.footprints["D1"]
    p1, p2 = d1.pad("1"), d1.pad("2")
    d1.shapes = [s for s in d1.shapes if not s.layer.endswith(("SilkS", "Silkscreen"))]
    # a symmetric box around both pads
    xs = [q[0] for pl in (p1.poly, p2.poly) for q in pl]
    ys = [q[1] for pl in (p1.poly, p2.poly) for q in pl]
    x0, y0, x1, y1 = min(xs) - 0.3, min(ys) - 0.3, max(xs) + 0.3, max(ys) + 0.3
    d1.shapes.append(Shape("rect", "F.SilkS", 0.12, False, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], owner="D1"))
    d1.texts = [t for t in d1.texts if t.text.strip() not in ("+", "-")]


def mem_silk(ctx):
    for t in ctx.board.footprints["J1"].texts:
        if t.kind == "reference":
            t.h, t.w, t.thickness, t.layer, t.hidden = 0.5, 0.5, 0.08, "F.SilkS", False


def mem_route_style(ctx):
    b = ctx.board
    t1, t2 = Track(), Track()
    t1.a, t1.b, t1.w, t1.layer, t1.net, t1.mid, t1.uuid, t1.locked = (130.0, 130.0), (134.0, 130.0), 0.25, "F.Cu", "PLANTED", None, "", False
    t2.a, t2.b, t2.w, t2.layer, t2.net, t2.mid, t2.uuid, t2.locked = (130.0, 130.0), (133.0, 128.0), 0.25, "F.Cu", "PLANTED", None, "", False
    b.tracks += [t1, t2]


def mem_pairs(ctx):
    b = ctx.board
    b.tracks = [t for t in b.tracks if t.net.rsplit("/", 1)[-1] != "USB_D_N"]


def mem_power(ctx):
    ctx.p.cfg.setdefault("checks", {})["power_paths"] = [{"net": "+5V", "from": "J1", "to": "U1", "amps": 5}]


def mem_decoupling(ctx):
    nl = ctx.netlist
    ctx._cache["netlist"] = nl.mutated({("C4", "1"): "unconnected-(C4-Pad1)"})


def mem_dfm_rules(ctx):
    ctx.pro.rules["min_track_width"] = 0.05


def mem_dfm_copper(ctx):
    v = Via()
    v.x, v.y, v.d, v.drill, v.layers, v.net, v.kind, v.uuid, v.locked = 130.0, 130.0, 0.35, 0.1, ["F.Cu", "B.Cu"], "GND", "through", "", False
    ctx.board.vias.append(v)


def mem_usb_c(ctx):
    nl = ctx.netlist
    ctx._cache["netlist"] = nl.mutated({("J1", "B5"): nl.pin[("J1", "A5")]})       # CC2 tied to CC1


def mem_i2c(ctx):
    nl = ctx.netlist
    ctx._cache["netlist"] = nl.mutated({("R8", "1"): "unconnected-(R8-Pad1)"})     # SDA pull-up lifted from 3V3


def mem_led(ctx):
    nl = ctx.netlist
    rail = [n for n in nl.nets if nl.short(n) == "+3V3"][0]
    ctx._cache["netlist"] = nl.mutated({("D1", "2"): rail})                            # LED straight across 3V3


def mem_ffc(ctx):
    nl = ctx.netlist
    nl.parts["J9"] = {"value": "CAM FH12-22S", "footprint": "Connector_FFC-FPC:Hirose_FH12-22S-0.5SH_1x22-1MP_P0.50mm_Horizontal",
                      "fields": {}, "props": {}, "lib": "", "part": "", "sheet": "/", "dnp": False, "in_bom": True,
                      "on_board": True, "datasheet": "", "description": "", "tstamp": ""}
    p3 = [n for n in nl.nets if nl.short(n) == "+3V3"][0]
    gnd = [n for n in nl.nets if nl.short(n) == "GND"][0]
    for i in range(1, 23):
        nl.pin[("J9", str(i))] = p3 if i == 1 else (gnd if i == 22 else f"unconnected-(J9-Pad{i})")


def _fake_fp(ref, lib, x, y, pads):
    fp = Footprint()
    fp.ref, fp.value, fp.lib_id, fp.x, fp.y = ref, ref, lib, x, y
    for num, px, py, w, h, net in pads:
        pd = Pad()
        pd.ref, pd.num, pd.net, pd.kind, pd.shape = ref, num, net, "smd", "rect"
        pd.x, pd.y, pd.angle, pd.w, pd.h = px, py, 0.0, w, h
        pd.layers = ["F.Cu", "F.Mask", "F.Paste"]
        pd.drill, pd.drill_w, pd.drill_h, pd.rr = False, 0.0, 0.0, 0.0
        pd.poly = geom.rect_poly(px, py, w, h)
        pd.polys = [pd.poly]
        pd.pinfunction = pd.pintype = pd.uuid = ""
        fp.pads.append(pd)
    return fp


def mem_crystal(ctx):
    b = ctx.board
    fp = _fake_fp("Y9", "Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm", 130.0, 130.0,
                  [("1", 128.75, 130.0, 1.0, 1.8, "XIN"), ("2", 131.25, 130.0, 1.0, 1.8, "XOUT")])
    fp.value = "32.768kHz"
    b.fp_list.append(fp)
    b.footprints["Y9"] = fp
    z = Zone()
    z.net, z.layers, z.name = "GND", ["F.Cu"], "planted"
    z.fills = {"F.Cu": [[(127.0, 128.0), (133.0, 128.0), (133.0, 132.0), (127.0, 132.0)]]}
    z.outline = [z.fills["F.Cu"][0]]
    b.zones.append(z)


def mem_thermal(ctx):
    b = ctx.board
    pads = [("EP", 130.0, 130.0, 2.6, 2.6, "GND")] + [(str(i + 1), 128.2 + 0.65 * (i % 4), 128.0, 0.3, 0.7, f"N{i}") for i in range(4)]
    fp = _fake_fp("U9", "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm", 130.0, 130.0, pads)
    b.fp_list.append(fp)
    b.footprints["U9"] = fp
    b.vias = [v for v in b.vias if not geom.inside((v.x, v.y), fp.pads[0].poly)]


def mem_control(ctx):
    nl = ctx.netlist
    ctx._cache["netlist"] = nl.mutated({("U2", "1"): "unconnected-(U2-PB5-Pad1)"})


def mem_mechanical(ctx):
    b = ctx.board
    b.fp_list = [f for f in b.fp_list if not f.ref.startswith("H")]
    b.footprints = {f.ref: f for f in b.fp_list}


def mem_bom_fields(ctx):
    nl = ctx.netlist
    nl.parts["R1"]["fields"]["LCSC"] = "25905"               # missing the C


def mem_bom_board(ctx):
    ctx.board.footprints["R3"].value = "22k"


def mem_sch_nets(ctx):
    nl = ctx.netlist
    sda = [n for n in nl.nets if nl.short(n) == "I2C_SDA"][0]
    ctx._cache["netlist"] = nl.mutated({("J2", "3"): sda.replace("I2C_SDA", "I2C_SDA1")})


def _net_full(nl, short):
    for n in nl.nets:
        if nl.short(n) == short:
            return n
    return short


def _add_part(nl, ref, value, pins, footprint="", lib=""):
    """A part in the netlist: pins {number: (name, type, net short name)}."""
    nl.parts[ref] = {"value": value, "footprint": footprint, "fields": {}, "props": {}, "lib": lib, "part": value,
                     "sheet": "/", "dnp": False, "in_bom": True, "on_board": True, "datasheet": "", "description": "",
                     "tstamp": ""}
    for pin, (name, typ, net) in pins.items():
        full = _net_full(nl, net)
        nl.pin[(ref, pin)] = full
        nl.nets.setdefault(full, []).append((ref, pin))
        nl.pin_info[(ref, pin)] = {"name": name, "type": typ}


def _track(b, a, c, net, w=0.25, layer="F.Cu"):
    t = Track()
    t.a, t.b, t.w, t.layer, t.net, t.mid, t.uuid, t.locked = a, c, w, layer, net, None, "", False
    b.tracks.append(t)
    b.nets.add(net)
    return t


def mem_netclasses(ctx):
    ctx.pro.classes["Planted"] = {"name": "Planted", "clearance": 0.3}
    ctx.pro.patterns.append(("*NO_SUCH_NET", "Planted"))


def mem_hand(ctx):
    ctx.p.cfg.setdefault("fab", {})["assembly"] = False           # self-built: the hand-soldering check applies
    ctx.board.footprints["R1"].lib_id = "Resistor_SMD:R_0201_0603Metric"


def mem_flyback(ctx):
    nl = ctx.netlist
    _add_part(nl, "K9", "G5LE-14 DC5", {"1": ("", "passive", "+5V"), "2": ("", "passive", "K9_COIL"), "3": ("", "passive", "K9_NC"),
                                        "4": ("", "passive", "K9_NO"), "5": ("", "passive", "+5V")}, "Relay_THT:Relay_SPDT_Omron-G5LE-1")
    _add_part(nl, "Q9", "AO3400A", {"1": ("G", "input", "K9_DRIVE"), "2": ("S", "passive", "GND"), "3": ("D", "passive", "K9_COIL")})


def mem_cap_voltage(ctx):
    ctx.netlist.parts["C1"]["value"] = "10u 4V"                   # on +5V


def mem_gates(ctx):
    nl = ctx.netlist
    _add_part(nl, "Q9", "AO3400A", {"1": ("G", "input", "PLANT_GATE"), "2": ("S", "passive", "GND"), "3": ("D", "passive", "PLANT_LOAD")})
    ctx._cache["netlist"] = nl.mutated({("U2", "6"): _net_full(nl, "PLANT_GATE")})     # PB1 drives it alone


def mem_strapping(ctx):
    nl = ctx.netlist
    _add_part(nl, "U9", "ESP32-S3-WROOM-1-N8", {"26": ("IO45", "bidirectional", "PLANT_IO45"), "2": ("3V3", "power_in", "+3V3"),
                                               "1": ("GND", "power_in", "GND")}, "RF_Module:ESP32-S3-WROOM-1")
    _add_part(nl, "R99", "10k", {"1": ("", "passive", "+3V3"), "2": ("", "passive", "PLANT_IO45")})


def mem_open_drain(ctx):
    nl = ctx.netlist
    _add_part(nl, "U9", "TMP102", {"3": ("ALERT", "open_collector", "PLANT_ALERT")})
    _add_part(nl, "U8", "74HC14", {"1": ("1A", "input", "PLANT_ALERT")})


def mem_return_path(ctx):
    b = ctx.board
    z = Zone()
    z.net, z.layers, z.name = "GND", ["B.Cu"], "planted split"
    z.fills = {"B.Cu": [[(125.0, 140.0), (128.5, 140.0), (128.5, 146.0), (125.0, 146.0)],
                        [(131.5, 140.0), (135.0, 140.0), (135.0, 146.0), (131.5, 146.0)]]}
    z.outline = [[(125.0, 140.0), (135.0, 140.0), (135.0, 146.0), (125.0, 146.0)]]
    b.zones.append(z)
    _track(b, (126.0, 143.0), (134.0, 143.0), "PLANT_CLK")


def mem_crosstalk(ctx):
    b = ctx.board
    _track(b, (105.0, 140.0), (125.0, 140.0), "PLANT_CLK")
    _track(b, (105.0, 140.45), (125.0, 140.45), "PLANT_SIG")


def mem_antenna(ctx):
    b = ctx.board
    pads = [(str(i + 1), 116.75, 110.0 + 1.27 * i, 1.5, 0.9, "GND" if i == 0 else f"P{i}") for i in range(14)] + \
           [(str(i + 15), 133.25, 110.0 + 1.27 * i, 1.5, 0.9, f"Q{i}") for i in range(14)]
    fp = _fake_fp("U9", "RF_Module:ESP32-S3-WROOM-1", 125.0, 117.0, pads)
    fp.value = "ESP32-S3-WROOM-1-N8"
    fp.shapes.append(Shape("rect", "F.Fab", 0.1, False, [(116.0, 104.25), (134.0, 104.25), (134.0, 129.75), (116.0, 129.75)],
                           owner="U9"))
    b.fp_list.append(fp)
    b.footprints["U9"] = fp


# ----------------------------------------------------------------------------- verification v2
def mem_pinout(ctx):
    nl = ctx.netlist                                   # U1's symbol drawn for an LDO with VIN on pin 1
    nl.pin_info[("U1", "1")] = {"name": "VI", "type": "power_in"}
    nl.pin_info[("U1", "3")] = {"name": "GND", "type": "power_in"}


def mem_pinout_pad(ctx):
    nl = ctx.netlist                                   # an exposed-pad pin with no pad in the SOIC-8 footprint
    full = _net_full(nl, "GND")
    nl.pin[("U2", "9")] = full
    nl.nets.setdefault(full, []).append(("U2", "9"))
    nl.pin_info[("U2", "9")] = {"name": "EP", "type": "passive"}


def mem_package(ctx):
    ctx.board.footprints["U2"].lib_id = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"     # the narrow SOIC under a wide part


def mem_domains(ctx):
    nl = ctx.netlist                                   # a 5 V shift register driving the 3.3 V MCU's SDA
    _add_part(nl, "U9", "74HC595", {"16": ("VCC", "power_in", "+5V"), "8": ("GND", "power_in", "GND"),
                                    "15": ("QA", "output", "I2C_SDA")})


def mem_regulators(ctx):
    nl = ctx.netlist
    ctx._cache["netlist"] = nl.mutated({("C2", "1"): "unconnected-(C2-Pad1)"})     # the LDO's 22 uF output capacitor gone


def mem_ldo_heat(ctx):
    ctx.p.cfg.setdefault("checks", {})["currents"] = {"+3V3": 1.2}               # 5 V -> 3.3 V at 1.2 A in a SOT-223


def mem_drop(ctx):
    ctx.p.cfg.setdefault("checks", {})["power_paths"] = [{"net": "+3V3", "from": "U1.2", "to": "U2.8", "amps": 8}]


def mem_switcher(ctx):
    nl, b = ctx.netlist, ctx.board
    _add_part(nl, "U9", "TPS54202", {"1": ("GND", "power_in", "GND"), "2": ("SW", "power_out", "PLANT_SW9"),
                                     "3": ("VIN", "power_in", "+5V"), "4": ("FB", "input", "PLANT_FB9")})
    _add_part(nl, "L9", "4.7uH", {"1": ("", "passive", "PLANT_SW9"), "2": ("", "passive", "+3V3")},
              "Inductor_SMD:L_Taiyo-Yuden_NR-40xx")
    _add_part(nl, "C9", "10u 25V", {"1": ("", "passive", "+5V"), "2": ("", "passive", "GND")}, "Capacitor_SMD:C_0805_2012Metric")
    for fp in (_fake_fp("U9", "Package_TO_SOT_SMD:SOT-23-6", 170.0, 170.0,
                        [("1", 169.0, 169.0, 0.6, 0.6, "GND"), ("2", 170.0, 169.0, 0.6, 0.6, "PLANT_SW9"),
                         ("3", 171.0, 169.0, 0.6, 0.6, "+5V"), ("4", 170.0, 171.0, 0.6, 0.6, "PLANT_FB9")]),
               _fake_fp("L9", "Inductor_SMD:L_Taiyo-Yuden_NR-40xx", 170.0, 175.0,
                        [("1", 168.5, 175.0, 1.5, 3.0, "PLANT_SW9"), ("2", 171.5, 175.0, 1.5, 3.0, "+3V3")]),
               _fake_fp("C9", "Capacitor_SMD:C_0805_2012Metric", 185.0, 169.0,
                        [("1", 184.0, 169.0, 1.0, 1.25, "+5V"), ("2", 186.0, 169.0, 1.0, 1.25, "GND")])):
        b.fp_list.append(fp)
        b.footprints[fp.ref] = fp


def mem_pours(ctx):
    b = ctx.board
    z = Zone()
    z.net, z.layers, z.name = next(n for n in b.nets if n.rsplit("/", 1)[-1] == "GND"), ["B.Cu"], "planted island"
    z.fills = {"B.Cu": [[(170.0, 170.0), (176.0, 170.0), (176.0, 176.0), (170.0, 176.0)]]}
    z.outline = [[(170.0, 170.0), (176.0, 170.0), (176.0, 176.0), (170.0, 176.0)]]
    b.zones.append(z)


def mem_layer_change(ctx):
    b = ctx.board
    _track(b, (180.0, 180.0), (185.0, 180.0), "PLANT_CLK", layer="F.Cu")
    _track(b, (185.0, 180.0), (190.0, 180.0), "PLANT_CLK", layer="B.Cu")
    v = Via()
    v.x, v.y, v.d, v.drill, v.layers, v.net, v.kind, v.uuid, v.locked = 185.0, 180.0, 0.6, 0.3, ["F.Cu", "B.Cu"], "PLANT_CLK", "through", "", False
    b.vias.append(v)


def mem_stubs(ctx):
    b = ctx.board
    _track(b, (180.0, 190.0), (190.0, 190.0), "PLANT_CLK")
    _track(b, (190.0, 190.0), (200.0, 190.0), "PLANT_CLK")
    _track(b, (190.0, 190.0), (190.0, 196.0), "PLANT_CLK")                      # a 6 mm branch off a clock


def mem_length_groups(ctx):
    ctx.p.cfg.setdefault("checks", {})["length_groups"] = [{"name": "PLANT", "nets": ["PLANT_A", "PLANT_B"], "tolerance_mm": 1}]
    _track(ctx.board, (180.0, 200.0), (190.0, 200.0), "PLANT_A")
    _track(ctx.board, (180.0, 202.0), (198.0, 202.0), "PLANT_B")


def mem_noise(ctx):
    _track(ctx.board, (180.0, 210.0), (195.0, 210.0), "PLANT_SW")
    _track(ctx.board, (180.0, 210.6), (195.0, 210.6), "PLANT_ADC")


def mem_esd(ctx):
    b = ctx.board
    net = b.footprints["J1"].pad("A6").net
    fp = _fake_fp("D9", "Package_TO_SOT_SMD:SOT-23-6", 180.0, 220.0,
                  [("1", 179.0, 219.0, 0.6, 0.6, net), ("2", 180.0, 219.0, 0.6, 0.6, "GND")])
    fp.value = "USBLC6-2SC6"
    b.fp_list.append(fp)
    b.footprints["D9"] = fp


def mem_quality(ctx):
    b = ctx.board
    fp = _fake_fp("R99", "Resistor_SMD:R_0402_1005Metric", 185.0, 230.0,
                  [("1", 180.0, 230.0, 0.6, 0.6, "PLANT_DETOUR"), ("2", 190.0, 230.0, 0.6, 0.6, "PLANT_DETOUR")])
    b.fp_list.append(fp)
    b.footprints["R99"] = fp
    _track(b, (180.0, 230.0), (180.0, 245.0), "PLANT_DETOUR")                    # 40 mm for a 10 mm connection
    _track(b, (180.0, 245.0), (190.0, 245.0), "PLANT_DETOUR")
    _track(b, (190.0, 245.0), (190.0, 230.0), "PLANT_DETOUR")


def mem_dangling(ctx):
    _track(ctx.board, (180.0, 250.0), (186.0, 250.0), "+5V")                    # a loose piece of +5V


# (check id, what is planted, plant, text the new finding must contain; file plants take the project)
CASES = [
    ("erc", "U2 pin 6 without its no-connect flag", ("file", plant_erc), "pin"),
    ("drc", "C3 moved onto C1", ("file", plant_drc), ""),
    ("drc", "a misspelt constraint in the custom rules file", ("file", plant_dru), "custom rules"),
    ("pcb.netclasses", "a net class pattern that matches no net", ("mem", mem_netclasses), "matches no net"),
    ("sch.render", "a note written over R1's value", ("file", plant_render), "PLANTED"),
    ("sch.wiring", "a wire laid along an existing wire", ("file", plant_wiring), "collinear"),
    ("sch.style", "a ground symbol turned upside down", ("file", plant_style), "points up"),
    ("sch.text", "a part count written inside a sheet symbol", ("file", plant_text_in_sheet), "inside the sheet symbol"),
    ("sch.text", "a note explaining how labels work", ("file", plant_text_narration), "how schematics work"),
    ("sch.conventions", "IEC values chosen, a resistor written 5.1k", ("mem", mem_conventions), "writes values"),
    ("sch.nets", "J2 pin 3 on 'I2C_SDA1' instead of I2C_SDA", ("mem", mem_sch_nets), "I2C_SDA1"),
    ("nets.model", "a net declared under a name no net has", ("mem", mem_nets_model), "VCC_OLD"),
    ("bom.fields", "R1's LCSC code without its C", ("mem", mem_bom_fields), "LCSC"),
    ("bom.board", "R3 is 22k on the board", ("mem", mem_bom_board), "R3"),
    ("pcb.placement", "U1 moved off the board", ("mem", mem_placement), "U1"),
    ("pcb.polarity", "D1's silk made symmetric", ("mem", mem_polarity), "D1"),
    ("pcb.silk", "J1's reference 0.5 mm high", ("mem", mem_silk), "silk"),
    ("route.style", "two tracks meeting at 34 degrees", ("mem", mem_route_style), "acute"),
    ("hs.pairs", "USB_D_N unrouted, USB_D_P routed", ("mem", mem_pairs), "only one half"),
    ("power.width", "5 A declared through the +5V track", ("mem", mem_power), "+5V"),
    ("power.decoupling", "C4 lifted off +3V3", ("mem", mem_decoupling), "U2"),
    ("dfm.rules", "board minimum track 0.05 mm", ("mem", mem_dfm_rules), "track"),
    ("dfm.copper", "a via with a 0.1 mm drill", ("mem", mem_dfm_copper), "drill"),
    ("lessons.usb_c", "CC2 tied to CC1", ("mem", mem_usb_c), "CC1 and CC2"),
    ("lessons.i2c", "SDA pull-up lifted from 3V3", ("mem", mem_i2c), "SDA"),
    ("lessons.led", "D1 straight across +3V3", ("mem", mem_led), "D1"),
    ("lessons.rpi_ffc", "a camera FFC with 3V3 on pin 1", ("mem", mem_ffc), "reversed"),
    ("lessons.crystal", "a GND pour between a crystal's pads", ("mem", mem_crystal), "Y9"),
    ("lessons.thermal_pad", "a QFN exposed pad with no vias", ("mem", mem_thermal), "U9"),
    ("lessons.control_pins", "U2 reset pin left open", ("mem", mem_control), "U2"),
    ("lessons.mechanical", "no mounting holes", ("mem", mem_mechanical), "mounting"),
    ("cpl.jlc", "JLC placement corrections removed", ("file", plant_cpl), "rotate"),
    ("assembly.hand", "R1 an 0201 on a board you solder yourself", ("mem", mem_hand), "too small"),
    ("power.flyback", "a relay coil switched by a MOSFET with no diode", ("mem", mem_flyback), "no flyback diode"),
    ("power.cap_voltage", "C1 on +5V rated 4 V", ("mem", mem_cap_voltage), "rated 4"),
    ("power.gates", "a MOSFET gate driven only by PB1", ("mem", mem_gates), "driven by"),
    ("lessons.strapping", "an ESP32-S3 with GPIO45 pulled up", ("mem", mem_strapping), "GPIO45"),
    ("lessons.open_drain", "an open-drain ALERT with no pull-up", ("mem", mem_open_drain), "nothing pulls"),
    ("si.return_path", "a clock track across a 3 mm gap in the GND pour under it", ("mem", mem_return_path), "gap"),
    ("si.crosstalk", "a signal 0.2 mm beside a clock for 20 mm", ("mem", mem_crosstalk), "beside"),
    ("pcb.antenna", "an ESP32 module with the GND pour under its antenna", ("mem", mem_antenna), "antenna has copper"),
    ("sch.pinout", "U1 drawn with VIN on pin 1 (the real part has GND there)", ("mem", mem_pinout), "U1"),
    ("sch.pinout", "a U2 pin on GND with no pad in the footprint", ("mem", mem_pinout_pad), "pin 9"),
    ("bom.package", "U2 (a wide SOIC-8 part) on the narrow SOIC-8 footprint", ("mem", mem_package), "U2"),
    ("power.domains", "a 5 V shift register driving the 3.3 V MCU's SDA", ("mem", mem_domains), "I2C_SDA"),
    ("power.regulators", "the LDO's 22 uF output capacitor removed", ("mem", mem_regulators), "U1"),
    ("power.thermal", "1.2 A through the SOT-223 LDO from 5 V", ("mem", mem_ldo_heat), "U1"),
    ("power.drop", "8 A declared along the +3V3 tracks", ("mem", mem_drop), "+3V3"),
    ("power.switcher", "a buck's input capacitor 14 mm from its VIN", ("mem", mem_switcher), "U9"),
    ("power.pours", "a GND pour island touching nothing", ("mem", mem_pours), "island"),
    ("si.layer_change", "a clock via with no ground via near it", ("mem", mem_layer_change), "PLANT_CLK"),
    ("si.stubs", "a 6 mm branch off a clock", ("mem", mem_stubs), "stub"),
    ("si.length_groups", "a matched group 8 mm apart", ("mem", mem_length_groups), "skew"),
    ("si.noise", "an ADC line 0.35 mm beside a switch node for 15 mm", ("mem", mem_noise), "PLANT_ADC"),
    ("pcb.esd", "a USB ESD array 70 mm from the connector", ("mem", mem_esd), "D9"),
    ("route.quality", "a 10 mm connection routed as a 40 mm U", ("mem", mem_quality), "PLANT_DETOUR"),
    ("route.quality", "a loose piece of +5V track", ("mem", mem_dangling), "dangling"),
]


def _run(check, ctx, baseline=False):
    try:
        return [f.to_json() for f in (check.fn(ctx) or [])]
    except NotApplicable:
        if baseline:                  # nothing for it on the clean board: the plant must give it something
            return []
        raise


def main(verbose=False, only=None):
    load_all()
    by_id = {c.id: c for c in REGISTRY}
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="tw-selftest-")
    fails, n = [], 0
    try:
        base_p = scratch_project(tmp)
        base_ctx = _ctx(base_p)
        baseline = {}
        for cid, what, (kind, plant), expect in CASES:
            if only and cid not in only:
                continue
            c = by_id.get(cid)
            if c is None:
                fails.append((cid, "no such check"))
                continue
            n += 1
            try:
                if cid not in baseline:
                    baseline[cid] = {(f["key"], f["message"]) for f in _run(c, base_ctx, baseline=True)}
                if kind == "file":
                    sub = tempfile.mkdtemp(dir=tmp)
                    p = scratch_project(sub)
                    plant(p)
                    ctx = _ctx(p)
                else:
                    ctx = _ctx(base_p)
                    ctx._cache.update({k: copy.deepcopy(v) for k, v in base_ctx._cache.items()
                                       if k in ("board", "netlist", "pro", "hier")})
                    ctx.p = env.Project(base_p.root, copy.deepcopy(base_p.cfg))
                    ctx.cfg = ctx.p.cfg
                    for need in ("board", "netlist", "pro"):
                        if need not in ctx._cache:
                            getattr(ctx, need)
                    plant(ctx)
                found = _run(c, ctx)
                new = [f for f in found if (f["key"], f["message"]) not in baseline[cid]]
                hit = [f for f in new if expect.lower() in f["message"].lower()]
                ok = bool(hit)
                msg = hit[0]["message"] if hit else (f"{len(new)} new findings, none mentioning '{expect}'" if new
                                                      else "no new finding")
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            if not ok:
                fails.append((cid, f"{what}: {msg}"))
            if verbose or not ok:
                print(f"  {'ok  ' if ok else 'FAIL'} {cid:<22} {what}  ->  {msg[:110]}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    covered = {c[0] for c in CASES}
    missing = [c.id for c in REGISTRY if c.id not in covered and not c.id.startswith("project.")]
    print(f"{n - len(fails)} of {n} planted faults caught ({time.time() - t0:.0f} s)"
          + (f"; checks without a case: {', '.join(missing)}" if missing and not only else ""))
    return 1 if fails else 0
