"""Checks learned the hard way: each one caught (or would have caught) a real board mistake that
passed ERC and DRC. The lesson behind each is in the Tracewright knowledge base."""
import re, collections
from . import check, Finding, NotApplicable, examined, plural
from .. import geom


def _fp_pads(fp):
    return {p.num: p for p in fp.pads}


# --------------------------------------------------------------------------- USB-C
@check("lessons.usb_c", "USB-C receptacle: D+/D- and CC wired right", "Lessons", needs=("netlist",))
def usb_c(ctx):
    """USB-C: A6/B6 are D+ and A7/B7 are D-, and both rows must be joined (a plug fits either way
    up). CC1 (A5) and CC2 (B5) need their own 5.1 k pull-downs on a sink -- one resistor on both
    breaks orientation detection and some chargers give no power at all."""
    nl = ctx.netlist
    out = []
    seen = []
    for ref, p in nl.parts.items():
        pins = set(nl.pins_of(ref))
        if not {"A6", "A7", "B6", "B7"} <= pins:
            continue
        seen.append(ref)
        a6, b6, a7, b7 = (nl.net_of(ref, x) for x in ("A6", "B6", "A7", "B7"))
        w = {"ref": ref}
        if a6 != b6 and "NC" not in (a6, b6):
            out.append(Finding("lessons.usb_c", "error", f"{ref}: A6 ({a6}) and B6 ({b6}) are both D+ and must be joined", w,
                               key=f"usbc:dp:{ref}"))
        if a7 != b7 and "NC" not in (a7, b7):
            out.append(Finding("lessons.usb_c", "error", f"{ref}: A7 ({a7}) and B7 ({b7}) are both D- and must be joined", w,
                               key=f"usbc:dm:{ref}"))
        if a6 and a6 == a7 and a6 != "NC":
            out.append(Finding("lessons.usb_c", "error", f"{ref}: D+ and D- are the same net ({a6})", w, key=f"usbc:short:{ref}"))
        for good, bad, name in ((a6, a7, "D+"), (a7, a6, "D-")):
            u = (good or "").upper()
            if name == "D+" and re.search(r"(D-|DM|_N$|N$|MINUS)", u) and not re.search(r"(D\+|DP|_P$)", u):
                out.append(Finding("lessons.usb_c", "error", f"{ref}: the D+ pins (A6/B6) are on {good}, which is named like D-",
                                   w, hint="D+ and D- are probably crossed between the connector and the host.",
                                   key=f"usbc:crossed:{ref}"))
        data = {n for n in (a6, b6, a7, b7) if n and n != "NC"}
        if data and not _esd_on(nl, data, ref):
            out.append(Finding("lessons.usb_c", "warning", f"{ref}: D+/D- ({', '.join(sorted(data))}) have no ESD protection",
                               w, hint="A plug carries static into the pins: add a low-capacitance TVS array (USBLC6-2SC6, "
                                       "TPD2E2U06, ESD9B) right at the connector.", key=f"usbc:esd:{ref}"))
        if {"A5", "B5"} <= pins:
            cc1, cc2 = nl.net_of(ref, "A5"), nl.net_of(ref, "B5")
            if cc1 == cc2 and cc1 not in ("NC", None):
                out.append(Finding("lessons.usb_c", "error", f"{ref}: CC1 and CC2 are tied together ({cc1})", w,
                                   hint="Give each CC pin its own 5.1 k to GND (sink) or connect both to a PD/CC controller.",
                                   key=f"usbc:cc:{ref}"))
            for pin, net in (("A5", cc1), ("B5", cc2)):
                if net in (None, "NC"):
                    out.append(Finding("lessons.usb_c", "warning", f"{ref}: {pin} (CC) is not connected -- a USB-C source "
                                       "gives no VBUS without the 5.1 k pull-down", w, key=f"usbc:ccfloat:{ref}:{pin}"))
    if not seen:
        raise NotApplicable("no USB-C receptacle")
    examined(ctx, plural(len(seen), "USB-C receptacle") + f" ({', '.join(seen[:4])})")
    return out


ESD_PART = re.compile(r"USBLC|TPD\d|ESD|PESD|SRV05|RCLAMP|TVS|SP05|SP3\d|IP4220|NUP\d|PRTR5V|D3V3|DT104|LXES|SMF05|CM1213|"
                      r"TPUSB|ESDA|ULC", re.I)


def _esd_on(nl, nets, conn):
    """Is there a TVS / ESD part on these nets (or one series resistor away)?"""
    near = set(nets)
    full = {nl.short(n): n for n in nl.nets}
    for n in list(nets):
        for r, x in nl.nets.get(full.get(n), []):
            if r.startswith("R") and len(nl.pins_of(r)) == 2:
                near |= {nl.net_of(r, y) for y in nl.pins_of(r)}
    for n in near:
        for r, x in nl.nets.get(full.get(n), []):
            if r == conn:
                continue
            p = nl.parts.get(r, {})
            txt = " ".join(str(p.get(k, "")) for k in ("value", "part", "footprint", "description"))
            if ESD_PART.search(txt) or (r.startswith(("D", "TVS")) and "tvs" in txt.lower()):
                return True
    return False


# --------------------------------------------------------------------------- I2C pull-ups
@check("lessons.i2c", "I2C buses have pull-ups", "Lessons", needs=("netlist",))
def i2c_pullups(ctx):
    """Every SDA / SCL net needs a pull-up resistor to a supply somewhere (on this board, or declared
    as external in tracewright.json checks.external_pullups)."""
    nl = ctx.netlist
    power = ctx.power_nets()
    ext = set(ctx.setting("checks.external_pullups", []) or [])
    out = []
    bus, external = [], []
    for full, nodes in nl.nets.items():
        s = nl.short(full)
        if not re.search(r"(^|_)(SDA|SCL)\d*($|_)|I2C\d*_?(SDA|SCL)", s, re.I):
            continue
        if s in ext:
            external.append(s)
            continue
        bus.append(s)
        ok = False
        for r, pin in nodes:
            if not r.startswith("R") or r.startswith("RN"):
                continue
            pins = nl.pins_of(r)
            other = [nl.net_of(r, x) for x in pins if x != pin]
            if any(o in power for o in other):
                ok = True
                break
        if not ok and len(nodes) > 1:
            conns = sorted({r for r, _ in nodes if r.startswith(("J", "P", "CN", "MOD", "A"))})
            hint = "Add 2.2k-10k to the bus supply, or list the net in checks.external_pullups."
            if conns:                        # e.g. a CM4 / CM5 has its own pull-ups on some buses
                hint = (f"The bus leaves the board through {', '.join(conns[:3])}: if its pull-ups are on the other side "
                        "(inside a module, or on another board), list the net in tracewright.json checks.external_pullups "
                        "and note where they are; otherwise add 2.2k-10k to the bus supply.")
            out.append(Finding("lessons.i2c", "warning", f"{s} has no pull-up resistor to a supply", {"net": s},
                               hint=hint, key=f"i2c:{s}"))
    if not bus and not external:
        raise NotApplicable("no I2C lines (no SDA / SCL nets)")
    examined(ctx, plural(len(bus), "I2C line") + (f", {len(external)} pulled up off the board" if external else ""))
    return out


# --------------------------------------------------------------------------- LEDs
@check("lessons.led", "LEDs have a current limit", "Lessons", needs=("netlist",))
def led_resistor(ctx):
    """An LED straight across a supply burns out: at least one side of it must be a private net that
    runs through a resistor (or reaches a driver pin), not a rail on both sides."""
    nl = ctx.netlist
    rails = ctx.power_nets() | ctx.ground_nets()
    out = []
    leds = []
    for ref, p in nl.parts.items():
        is_led = ref.startswith("LED") or (ref.startswith("D") and ("LED" in p["part"].upper() or "LED" in p["footprint"].upper()))
        if not is_led or p["dnp"]:
            continue
        leds.append(ref)
        pins = nl.pins_of(ref)
        nets = [nl.net_of(ref, x) for x in pins]
        if all(n in rails for n in nets):
            out.append(Finding("lessons.led", "error", f"{ref} sits straight across {' and '.join(nets)} with nothing to "
                               "limit its current", {"ref": ref}, key=f"led:rail:{ref}"))
            continue
        ok = False
        for x in pins:
            full = nl.pin.get((ref, x))
            if nl.short(full) in rails:
                continue
            for r, pin in nl.nets.get(full, []):
                if r != ref and r.startswith(("R", "U", "Q", "IC")):
                    ok = True
        if not ok:
            out.append(Finding("lessons.led", "warning", f"{ref} has no series resistor or driver on its private side",
                               {"ref": ref}, key=f"led:{ref}"))
    if not leds:
        raise NotApplicable("no LEDs")
    examined(ctx, plural(len(leds), "LED"))
    return out


# --------------------------------------------------------------------------- Raspberry Pi camera FFC
@check("lessons.rpi_ffc", "Raspberry Pi camera FFC numbering", "Lessons", needs=("netlist",))
def rpi_ffc(ctx):
    """The Raspberry Pi 15/22-pin CSI pinout puts GND on pin 1 and 3V3 on the last pin. Raspberry
    Pi's own FH12 footprint numbers the pads the opposite way to KiCad's stock one, so with the stock
    footprint 3V3 and GND trade places on the cable -- and ERC/DRC stay clean."""
    nl = ctx.netlist
    grounds, power = ctx.ground_nets(), ctx.power_nets()
    out = []
    ffc = []
    for ref, p in nl.parts.items():
        fpn = p["footprint"].upper()
        pins = nl.pins_of(ref)
        n = len([x for x in pins if x.isdigit()])
        if not (("FH12" in fpn or "FFC" in fpn or "CSI" in p["value"].upper() or "CAM" in p["value"].upper()) and n in (15, 22)):
            continue
        ffc.append(ref)
        first, last = nl.net_of(ref, "1"), nl.net_of(ref, str(n))
        if first in power and last in grounds:
            out.append(Finding("lessons.rpi_ffc", "error", f"{ref}: pin 1 is {first} and pin {n} is {last} -- reversed against "
                               "the Raspberry Pi camera pinout (pin 1 GND, last pin 3V3)", {"ref": ref},
                               hint="Use Raspberry Pi's CM4/CM5 IO board footprint numbering, or renumber the pads.",
                               key=f"ffc:{ref}"))
        elif first in grounds and last in power:
            out.append(Finding("lessons.rpi_ffc", "info", f"{ref}: pin 1 GND / pin {n} {last} matches the Raspberry Pi pinout; "
                               "also confirm the footprint's pad 1 is where the cable's pin 1 lands", {"ref": ref},
                               key=f"ffc:ok:{ref}"))
    if not ffc:
        raise NotApplicable("no 15- or 22-pin camera FFC connector")
    examined(ctx, plural(len(ffc), "camera connector"))
    return out


# --------------------------------------------------------------------------- crystals
@check("lessons.crystal", "Nothing between a crystal's pads", "Lessons", needs=("pcb",))
def crystal_gap(ctx):
    """Crystal data sheets forbid copper between and under the pads. A keepout that only stops
    tracks and vias still lets a GND pour flood the gap, so the filled copper is sampled too."""
    b = ctx.board
    out = []
    fills = [(z, l, pl) for z in b.zones if not z.is_rule_area for l, pls in z.fills.items() for pl in pls]
    xtals = []
    for fp in b.fp_list:
        lib = fp.lib_id.lower()
        if not (fp.ref.startswith(("Y", "X")) and ("crystal" in lib or "xtal" in lib or "osc" not in lib)):
            continue
        if "crystal" not in lib and "xtal" not in lib and not fp.value.lower().endswith(("khz", "mhz")):
            continue
        pads = [p for p in fp.pads if p.net]
        if len(pads) < 2:
            continue
        xtals.append(fp.ref)
        layer = "F.Cu" if fp.side == "F" else "B.Cu"
        a, c = pads[0], pads[1]
        pts = [(a.x + (c.x - a.x) * t, a.y + (c.y - a.y) * t) for t in (0.4, 0.5, 0.6)]
        pad_nets = {p.net for p in fp.pads}
        hit = None
        for z, l, pl in fills:
            if l != layer or z.net in (a.net, c.net):
                continue
            if any(geom.inside(q, pl) for q in pts):
                hit = f"{z.net} pour"
                break
        if not hit:
            for t in b.tracks:
                if t.layer != layer or t.net in pad_nets:
                    continue
                if any(geom.seg_point_dist(q, t.a, t.b) < t.w / 2 + 0.05 for q in pts):
                    hit = f"{t.net} track"
                    break
        if hit:
            out.append(Finding("lessons.crystal", "warning", f"{fp.ref}: {hit} runs between the crystal pads",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y},
                               hint="Add a rule area in the footprint (no tracks, vias or pour) over the pad gap.",
                               key=f"crystal:{fp.ref}"))
    if not xtals:
        raise NotApplicable("no crystals")
    examined(ctx, plural(len(xtals), "crystal") + f" ({', '.join(xtals[:4])})")
    return out


# --------------------------------------------------------------------------- exposed pads
EP_PKG = re.compile(r"QFN|DFN|SON|PowerPAD|HTSSOP|HSOP|_EP|EP\d|TDFN|WSON|VSON|HVQFN|TO-252|TO-263|DPAK|PowerSO", re.I)


@check("lessons.thermal_pad", "Exposed pads have thermal vias", "Lessons", needs=("pcb",))
def thermal_pad(ctx):
    """An exposed (thermal) pad on a power or ground net with no via to the inner planes carries
    no heat away; regulators and drivers overheat. Counts vias inside the pad outline."""
    b = ctx.board
    if len(b.copper) < 2:
        raise NotApplicable("a one-layer board: no other layer for thermal vias to reach")
    out = []
    eps = []
    for fp in b.fp_list:
        if not EP_PKG.search(fp.lib_id) or not fp.pads:
            continue
        big = max(fp.pads, key=lambda p: p.w * p.h)
        others = [p for p in fp.pads if p is not big]
        if not others or big.w * big.h < 3 * max(p.w * p.h for p in others) or not big.net:
            continue
        eps.append(fp.ref)
        n = sum(1 for v in b.vias if v.net == big.net and geom.inside((v.x, v.y), big.poly))
        pad_layer = "F.Cu" if fp.side == "F" else "B.Cu"
        other_copper = big.net in ctx.ground_nets() or any(
            z.net == big.net and any(l != pad_layer for l in z.layers) for z in b.zones if not z.is_rule_area)
        if n == 0 and other_copper:
            sev = "warning" if big.w * big.h >= 2.0 else "info"
            out.append(Finding("lessons.thermal_pad", sev, f"{fp.ref}: exposed pad ({big.w:.1f} x {big.h:.1f} mm, {big.net}) "
                               "has no thermal vias", {"ref": fp.ref, "x": big.x, "y": big.y},
                               hint="Add a grid of 0.3 mm vias (tented or plugged) into the pad to the ground plane.",
                               key=f"thermal:{fp.ref}"))
    if not eps:
        raise NotApplicable("no exposed pads")
    examined(ctx, plural(len(eps), "exposed pad") + f" ({', '.join(eps[:5])})")
    return out


# --------------------------------------------------------------------------- reset / enable pins
@check("lessons.control_pins", "Reset and enable pins are driven", "Lessons", needs=("netlist",))
def control_pins(ctx):
    """A floating reset or enable input makes a part start, or not, at random: those pins must be
    tied or driven (a no-connect flag on them is also a mistake)."""
    nl = ctx.netlist
    out = []
    pat = re.compile(r"^(~?\{?)(N?RST|N?RESET|RESETN|RST_?N|MCLR|EN|CE|CHIP_?EN|CHIP_?PU|RUN|SHDN|~\{SHDN\}|PWRKEY|nSLEEP)(\}?)$", re.I)
    seen = 0
    for (ref, pin), full in nl.pin.items():
        if not ref.startswith(("U", "IC")):
            continue
        name = nl.pin_name(ref, pin)
        if not any(pat.match(part.strip()) for part in re.split(r"[/,]", name or "")):
            continue                         # names carry alternate functions: "~{RESET}/PB5"
        if nl.pin_type(ref, pin) in ("output", "open_collector", "open_emitter", "power_out", "tri_state"):
            continue                         # a reset / power-good *output* may be left open
        seen += 1
        net = nl.short(full)
        others = [n for n in nl.nets.get(full, []) if n != (ref, pin)]
        if net == "NC" or not others:
            out.append(Finding("lessons.control_pins", "warning", f"{ref} pin {pin} ({name}) is not connected to anything",
                               {"ref": ref}, key=f"ctl:{ref}:{pin}"))
    if not seen:
        raise NotApplicable("no reset or enable inputs")
    examined(ctx, plural(seen, "reset or enable input"))
    return out


# --------------------------------------------------------------------------- mounting holes, test points
@check("lessons.mechanical", "Mounting holes and test access", "Lessons", needs=("pcb",))
def mechanical(ctx):
    """A board with no mounting holes or no test points is hard to fix in place and hard to bring
    up. Informational: some boards are meant to be held by connectors."""
    b = ctx.board
    out = []
    holes = [fp for fp in b.fp_list if fp.ref.startswith(("H", "MH")) or "mountinghole" in fp.lib_id.lower()]
    if not holes and b.outline:
        w, h = b.size()
        if w * h > 900:
            out.append(Finding("lessons.mechanical", "info", "no mounting holes", key="mech:holes"))
    tps = [fp for fp in b.fp_list if fp.ref.startswith("TP")]
    if not tps and len(b.fp_list) > 15:
        out.append(Finding("lessons.mechanical", "info", "no test points: add pads on the rails and key signals for bring-up",
                           key="mech:tp"))
    examined(ctx, f"{plural(len(holes), 'mounting hole')}, {plural(len(tps), 'test point')}")
    return out


# --------------------------------------------------------------------------- boot straps
# (pin, level for a normal flash boot, what the pin selects, "error" when the wrong level stops the boot)
STRAPS = {
    "esp32": [(0, "high", "boot mode (low = download)", "error"), (2, "low", "download mode needs it low or floating", "warning"),
              (5, "high", "SDIO slave timing", "info"), (12, "low", "flash voltage (high = 1.8 V: a 3.3 V flash will not "
                                                                 "boot)", "error"),
              (15, "high", "boot messages / SDIO timing", "info")],
    "s2": [(0, "high", "boot mode (low = download)", "error"), (45, "low", "flash voltage VDD_SPI (high = 1.8 V)", "error"),
           (46, "low", "boot mode (must be low with GPIO0 low to download)", "warning")],
    "s3": [(0, "high", "boot mode (low = download)", "error"), (3, None, "JTAG signal source", "info"),
           (45, "low", "flash voltage VDD_SPI (high = 1.8 V: a 3.3 V flash will not boot)", "error"),
           (46, "low", "boot mode / ROM messages (must be low to download)", "warning")],
    "c3": [(9, "high", "boot mode (low = download)", "error"), (8, "high", "must be high to download", "warning"),
           (2, "high", "must be high at boot", "warning")],
    "c6": [(9, "high", "boot mode (low = download)", "error"), (8, "high", "must be high to download", "warning"),
           (15, None, "JTAG signal source", "info")],
    "h2": [(9, "high", "boot mode (low = download)", "error"), (8, "high", "must be high to download", "warning")],
}


def _esp_family(part):
    t = " ".join(str(part.get(k, "")) for k in ("value", "part", "lib", "footprint")).upper()
    if "ESP32" not in t:
        return None
    for fam in ("S2", "S3", "C3", "C6", "H2"):
        if re.search(rf"ESP32-?{fam}", t):
            return fam.lower()
    if re.search(r"ESP32-?(C2|C5|C61|P4)", t):
        return None
    return "esp32"


def _gpio(name):
    for part in re.split(r"[/,\s]+", (name or "").upper()):
        m = re.match(r"^(?:GPIO|IO)(\d+)$", part)
        if m:
            return int(m.group(1))
        if part in ("MTDI",):
            return 12
        if part in ("MTDO",):
            return 15
    return None


def _pulls(nl, net_full, pin_ref, rails, grounds):
    """('up' / 'down', resistor) pulls on a net; and the other parts that load it."""
    ups, downs, loads = [], [], []
    for r, x in nl.nets.get(net_full, []):
        if r == pin_ref:
            continue
        pins = nl.pins_of(r)
        if r.startswith("R") and len(pins) == 2:
            other = [nl.net_of(r, y) for y in pins if y != x][0]
            (ups if other in rails else downs if other in grounds else loads).append(r)
        elif r.startswith(("SW", "TP", "J", "P", "CN", "C")) or r.startswith("#"):
            continue
        else:
            loads.append(r)
    return ups, downs, loads


@check("lessons.strapping", "Boot strap pins sit right at reset", "Lessons", needs=("netlist",))
def strapping(ctx):
    """Some pins are read once at reset to choose the boot mode or the flash voltage. An ESP32-S3
    with GPIO45 pulled up powers its 3.3 V flash at 1.8 V and never boots; GPIO0 pulled down always
    starts the downloader; a classic ESP32's GPIO12 pulled up does the same to its flash. Pulls to
    the wrong level are errors; a strap pin that drives other parts is a warning (they can pull it
    while the chip resets). The ESP32 EN pin needs its RC delay (a pull-up and a capacitor to
    ground) so the chip starts after the supply; an STM32's BOOT0 must be held low."""
    nl = ctx.netlist
    grounds, rails = ctx.ground_nets(), ctx.power_nets()
    out = []
    found = False
    chips = []
    for ref, part in sorted(nl.parts.items()):
        fam = _esp_family(part)
        pins = nl.pins_of(ref)
        if fam:
            found = True
            chips.append(ref)
            rules = {g: (lvl, what, sev) for g, lvl, what, sev in STRAPS[fam]}
            for x in pins:
                name = nl.pin_name(ref, x)
                g = _gpio(name)
                full = nl.pin.get((ref, x))
                if g is None or g not in rules or not full or nl.short(full) == "NC":
                    continue
                lvl, what, sev = rules[g]
                ups, downs, loads = _pulls(nl, full, ref, rails, grounds)
                net = nl.short(full)
                w = {"ref": ref, "net": net}
                if net in grounds or net in rails:
                    wrong = (lvl == "high" and net in grounds) or (lvl == "low" and net in rails)
                    if wrong:
                        out.append(Finding("lessons.strapping", sev, f"{ref} GPIO{g} ({what}) is tied to {net}", w,
                                           key=f"strap:tie:{ref}:{g}"))
                    continue
                if lvl == "low" and ups:
                    out.append(Finding("lessons.strapping", sev, f"{ref} GPIO{g} is pulled up by {', '.join(ups)}: it "
                                       f"selects {what}", w, hint="Remove the pull-up or move that function to another pin.",
                                       key=f"strap:up:{ref}:{g}"))
                elif lvl == "high" and downs:
                    out.append(Finding("lessons.strapping", sev, f"{ref} GPIO{g} is pulled down by {', '.join(downs)}: it "
                                       f"selects {what}", w, hint="Remove the pull-down or move that function to another pin.",
                                       key=f"strap:down:{ref}:{g}"))
                elif loads and sev != "info":
                    out.append(Finding("lessons.strapping", "warning", f"{ref} GPIO{g} is a strap pin ({what}) and also "
                                       f"drives {', '.join(sorted(loads)[:4])}: make sure nothing pulls it "
                                       f"{'low' if lvl == 'high' else 'high'} while {ref} resets", w,
                                       key=f"strap:load:{ref}:{g}"))
            en = [x for x in pins if re.match(r"^(EN|CHIP_PU|CHIP_EN)$", (nl.pin_name(ref, x) or "").upper())]
            for x in en:
                full = nl.pin.get((ref, x))
                if not full or nl.short(full) == "NC":
                    continue                                   # lessons.control_pins reports a floating EN
                caps = [r for r, y in nl.nets.get(full, []) if r.startswith("C") and not r.startswith("CN") and
                        any(nl.net_of(r, z) in grounds for z in nl.pins_of(r) if z != y)]
                ups, _, _ = _pulls(nl, full, ref, rails, grounds)
                if nl.short(full) in rails:
                    out.append(Finding("lessons.strapping", "warning", f"{ref} EN is tied straight to {nl.short(full)}: "
                                       "no reset delay", {"ref": ref}, hint="Espressif: 10k to 3V3 and 1uF to GND on EN, so "
                                       "the chip starts after the supply has settled.", key=f"strap:en:{ref}"))
                elif not caps or not ups:
                    miss = " and ".join(m for m, have in (("a pull-up", ups), ("a capacitor to ground", caps)) if not have)
                    out.append(Finding("lessons.strapping", "warning", f"{ref} EN has no {miss} (the RC reset delay)",
                                       {"ref": ref, "net": nl.short(full)},
                                       hint="Espressif: 10k to 3V3 and 1uF to GND on EN, so the chip starts after the "
                                            "supply has settled.", key=f"strap:enrc:{ref}"))
        for x in pins:                                          # STM32 and others: BOOT0
            if (nl.pin_name(ref, x) or "").upper().split("/")[-1] not in ("BOOT0", "PH3-BOOT0", "BOOT"):
                continue
            if not ref.startswith(("U", "IC")) or fam:
                continue
            found = True
            chips.append(ref)
            full = nl.pin.get((ref, x))
            net = nl.short(full) if full else "NC"
            if net == "NC" or len(nl.nets.get(full, [])) < 2:
                out.append(Finding("lessons.strapping", "error", f"{ref} BOOT0 floats: the chip may start its ROM "
                                   "bootloader instead of the program", {"ref": ref},
                                   hint="Pull BOOT0 to GND with 10k (a button or jumper to 3V3 for flashing).",
                                   key=f"strap:boot0:{ref}"))
                continue
            ups, downs, _ = _pulls(nl, full, ref, rails, grounds)
            if net in rails or (ups and not downs):
                out.append(Finding("lessons.strapping", "warning", f"{ref} BOOT0 is held high ({net}): it always starts "
                                   "the ROM bootloader", {"ref": ref, "net": net}, key=f"strap:boot0hi:{ref}"))
    if not found:
        raise NotApplicable("no ESP32 or chip with a BOOT0 pin")
    examined(ctx, "strap pins of " + ", ".join(sorted(set(chips))[:4]))
    return out


# --------------------------------------------------------------------------- open-drain lines
@check("lessons.open_drain", "Open-drain outputs have a pull-up", "Lessons", needs=("netlist",))
def open_drain(ctx):
    """An open-drain or open-collector output (an interrupt, a power-good, an alert) only pulls low:
    with nothing pulling it up, the line never goes high. A net whose driver is open-drain needs a
    resistor to a supply, a load to one (a relay coil or an LED), or an MCU input with its internal
    pull-up enabled (reported as a note: it relies on the firmware)."""
    nl = ctx.netlist
    grounds, rails = ctx.ground_nets(), ctx.power_nets()
    ext = set(ctx.setting("checks.external_pullups", []) or [])
    out = []
    seen = False
    lines = 0
    for full, nodes in sorted(nl.nets.items()):
        s = nl.short(full)
        if not s or s == "NC" or s in grounds or s in rails or s in ext:
            continue
        od = [(r, x) for r, x in nodes if nl.pin_type(r, x) in ("open_collector", "open_emitter")]
        if not od:
            continue
        seen = True
        lines += 1
        if any(not r.startswith(("U", "IC")) for r, _ in nodes):      # a resistor, coil, LED or connector is there
            continue
        mcu = [f"{r}.{nl.pin_name(r, x)}" for r, x in nodes if (r, x) not in od and
               re.match(r"^(GPIO|IO|P[A-K]\d|P\d|GP\d|RA\d|RB\d|RC\d)", nl.pin_name(r, x) or "", re.I)]
        drv = ", ".join(f"{r}.{nl.pin_name(r, x) or x}" for r, x in od[:3])
        if mcu:
            out.append(Finding("lessons.open_drain", "info", f"{s}: {drv} is open-drain and only {', '.join(mcu[:2])} "
                               "can pull it up (its internal pull-up must be on in the firmware)", {"net": s},
                               key=f"od:mcu:{s}"))
        else:
            out.append(Finding("lessons.open_drain", "warning", f"{s}: {drv} is open-drain and nothing pulls the line up",
                               {"net": s}, hint="Add a 4.7k-10k pull-up to the supply of the input that reads it.",
                               key=f"od:none:{s}"))
    if not seen:
        raise NotApplicable("no open-drain outputs in the netlist (their pins' types)")
    examined(ctx, plural(lines, "open-drain line"))
    return out
