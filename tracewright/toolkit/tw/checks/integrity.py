"""Schematic integrity against the real parts: every symbol pin reaches a pad, pin functions match the
part's own pinout (LCSC / EasyEDA, the library JLC places from), footprints match the part's package,
and ICs on different supply rails are not wired together without level shifting.

A library symbol drawn for one vendor's regulator and used with another's (SOT-23 LDOs come in six
pin orders), a narrow SOIC footprint under a wide-body part, a 5 V output into a 3.3 V input: ERC and
DRC pass all of them. Part data comes from the project's sourcing cache (the BOM lookup and the
pick-and-place check fill it); a part the data does not cover is reported as not verified, never as a
pass."""
import re, time
from . import check, Finding, NotApplicable

# ----------------------------------------------------------------------------- pin functions
# Names that mean the same terminal, by class. Anything else compares by its own tokens (PB3, IO5, CC1).
ALIAS = {
    "VI": "VIN", "IN": "VIN", "INPUT": "VIN", "PVIN": "VIN", "AVIN": "VIN", "VIN": "VIN",
    "VO": "VOUT", "OUT": "VOUT", "OUTPUT": "VOUT", "VOUT": "VOUT",
    "GND": "GND", "VSS": "GND", "GROUND": "GND", "AGND": "GND", "DGND": "GND", "PGND": "GND", "SGND": "GND",
    "EP": "GND", "EPAD": "GND", "0V": "GND", "GNDPAD": "GND",
    "VCC": "VCC", "VDD": "VCC", "VS": "VCC", "V+": "VCC", "VCCIO": "VCC", "VDDIO": "VCC", "AVDD": "VCC",
    "DVDD": "VCC", "AVCC": "VCC", "DVCC": "VCC", "IOVDD": "VCC", "VBAT": "VBAT",
    "D+": "DP", "DP": "DP", "DP1": "DP", "DP2": "DP", "USBDP": "DP", "USB_DP": "DP", "D_P": "DP",
    "D-": "DN", "DN": "DN", "DM": "DN", "DN1": "DN", "DN2": "DN", "USBDM": "DN", "USB_DM": "DN", "D_N": "DN",
    "G": "G", "GATE": "G", "S": "S", "SOURCE": "S", "D": "D", "DRAIN": "D",
    "B": "B", "BASE": "B", "E": "E", "EMITTER": "E", "C": "C", "COLLECTOR": "C",
    "A": "A", "ANODE": "A", "K": "K", "KA": "K", "CATHODE": "K",
    "EN": "EN", "ENABLE": "EN", "CE": "EN", "SHDN": "EN", "ON/OFF": "EN",
    "ADJ": "ADJ", "FB": "FB", "SW": "SW", "LX": "SW", "PH": "SW", "BST": "BST", "BOOT": "BST", "CB": "BST",
}
MECH = {"EH", "MP", "SH", "SHIELD", "MOUNT", "MH", "NC", "DNC", "N/C", "TAB", "PAD", "SHELL", "CASE"}
# classes whose swap breaks or burns the part: a mismatch between two of these is an error
CRITICAL = {"VIN", "VOUT", "GND", "VCC", "G", "S", "D", "B", "E", "C", "A", "K", "SW"}
# names vendors use for the same terminal across classes: a Darlington array's common emitter is its
# ground (ULN2003 pin 8 "E"), and a supply input is VIN on one part's sheet and VCC on another's
EQUIVALENT = [{"GND", "E"}, {"VIN", "VCC"}]


def _tokens(name):
    """The words a pin name is made of, each mapped to its class: '~{RESET}/PB5' -> {'RESET', 'PB5'},
    'PB3(PCINT3/XTAL1)' -> {'PB3', 'PCINT3', 'XTAL1'}, 'D+' -> {'DP'}, 'VO' -> {'VOUT'}."""
    s = re.sub(r"~\{([^}]*)\}", r"\1", str(name or "")).upper().strip()
    if not s or re.fullmatch(r"(PIN)?[_\s]*\d+", s):
        return set()
    if s in ALIAS:
        return {ALIAS[s]}
    out = set()
    for t in re.split(r"[/(),;|\s]+", s):
        if not t:
            continue
        if t in ALIAS:
            out.add(ALIAS[t])
            continue
        for u in re.split(r"(?<=[A-Z0-9])-(?=[A-Z0-9])|_", t):       # PA0-WKUP, IO_5 (D- and V- stay whole)
            if not u or re.fullmatch(r"\d+", u):
                continue
            u = re.sub(r"^GPIO(\d+)$", r"IO\1", u)
            out.add(ALIAS.get(u, u))
    return out


def compare_pin(ours, theirs):
    """'match' | 'mismatch' | 'critical' | 'unknown' for two names of the same pin."""
    a, b = _tokens(ours), _tokens(theirs)
    if not a or not b or a <= MECH or b <= MECH:
        return "unknown"
    if a & b or any(a & e and b & e and (a & e) != (b & e) for e in EQUIVALENT):
        return "match"
    if (a & CRITICAL) and (b & CRITICAL):
        return "critical"
    # one channel under two naming schemes: I1 / 1B / IN1, O1 / 1C / OUT1
    na = re.findall(r"\d+", re.sub(r"~\{([^}]*)\}", r"\1", str(ours)))
    nb = re.findall(r"\d+", re.sub(r"~\{([^}]*)\}", r"\1", str(theirs)))
    if len(na) == 1 and na == nb and not (a & CRITICAL) and not (b & CRITICAL):
        return "match"
    return "mismatch"


def _lcsc(part, fp=None):
    """The part's LCSC code (netlist fields, else the board footprint's), or ''."""
    for src in (part.get("fields") or {}, (fp.fields if fp is not None else {}) or {}):
        for k, v in src.items():
            if k.strip().upper() in ("LCSC", "LCSC PART", "LCSC_PART", "JLCPCB PART", "JLC") and str(v).strip():
                m = re.match(r"\s*(C\d{2,})", str(v).upper())
                if m:
                    return m.group(1)
    return ""


def _parts_db(ctx):
    from tw.jlc import Parts
    budget = float(ctx.setting("checks.part_data_s", 60) or 60)
    return Parts(ctx.p.root, deadline=time.time() + budget, stop=ctx.stop)


def _assembled(nl, ref, part):
    return not part.get("dnp") and part.get("in_bom", True) and not ref.startswith(("#", "H", "MH", "TP", "FID", "LOGO"))


# ----------------------------------------------------------------------------- sch.pinout
@check("sch.pinout", "Symbol pins match the real part's pinout", "Schematic", needs=("netlist",), timeout=180)
def sch_pinout(ctx):
    """Two ways a connection silently goes missing or lands on the wrong terminal. (1) A symbol pin
    with a net but no pad of that number in the footprint: the net never reaches the board, and DRC
    cannot see a pad that does not exist. (2) The symbol's pin functions against the part's own pinout
    (the LCSC / EasyEDA library JLC places from): a regulator symbol with GND on pin 1 used with a part
    whose pin 1 is VIN, a MOSFET drawn G-D-S on a G-S-D part. Pins compare by function (VO = VOUT,
    VSS = GND, D+ = DP), so libraries that number differently but agree on function pass. Two-pin
    parts are left to the polarity checks."""
    nl = ctx.netlist
    board = ctx.board if ctx.available("pcb") else None
    out, unverified = [], []
    # (1) symbol pins without pads
    if board is not None:
        for ref, part in sorted(nl.parts.items()):
            fp = board.footprints.get(ref)
            if fp is None or not _assembled(nl, ref, part):
                continue
            pads = {p.num for p in fp.pads}
            for pin in nl.pins_of(ref):
                net = nl.net_of(ref, pin)
                if not net or net == "NC" or pin in pads:
                    continue
                out.append(Finding("sch.pinout", "error",
                                   f"{ref} pin {pin} ({nl.pin_name(ref, pin) or 'unnamed'}) is on {net} but footprint "
                                   f"{fp.lib_id.split(':')[-1]} has no pad {pin}: the connection never reaches the board",
                                   {"ref": ref}, hint="Pick the footprint whose pad numbers match the symbol (or fix the "
                                   "symbol's pin numbers); an exposed pad usually needs its own pin in the symbol.",
                                   key=f"pinout:nopad:{ref}:{pin}"))
    # (2) pin functions against the part's own pinout
    db = None
    from tw.jlc import LookupFailed
    for ref, part in sorted(nl.parts.items()):
        pins = nl.pins_of(ref)
        if len(pins) < 3 or not _assembled(nl, ref, part):
            continue
        code = _lcsc(part, board.footprints.get(ref) if board is not None else None)
        if not code:
            continue
        if db is None:
            db = _parts_db(ctx)
        try:
            _, _, names = db.jlc_footprint(code)
        except LookupFailed as e:
            unverified.append(f"{ref} ({e})")
            continue
        if not names:
            unverified.append(f"{ref} (no pinout for {code})")
            continue
        bad, crit, known = [], [], 0
        for pin in pins:
            if pin not in names:
                continue
            r = compare_pin(nl.pin_name(ref, pin), names[pin])
            if r == "unknown":
                continue
            known += 1
            if r == "critical":
                crit.append((pin, nl.pin_name(ref, pin), names[pin]))
            elif r == "mismatch":
                bad.append((pin, nl.pin_name(ref, pin), names[pin]))
        if not known:
            if any(_tokens(n) - MECH for n in names.values()):
                unverified.append(f"{ref} (pin names do not say)")
            continue                        # numbered pins only (a connector): nothing to compare
        if crit:
            desc = "; ".join(f"pin {p} is {a} here but {b} on {code}" for p, a, b in crit[:4])
            out.append(Finding("sch.pinout", "error", f"{ref} ({part.get('value', '')}): {desc}", {"ref": ref},
                               hint="The symbol's pinout is another part's. Use a symbol drawn for this exact part "
                                    "(its data sheet's pin table), or choose a part with this pinout.",
                               key=f"pinout:crit:{ref}"))
        elif bad and len(bad) > max(1, known // 5):
            desc = "; ".join(f"pin {p}: {a or '?'} vs {b}" for p, a, b in bad[:4])
            out.append(Finding("sch.pinout", "warning", f"{ref} ({part.get('value', '')}): {len(bad)} of {known} pin names "
                               f"differ from {code}'s pinout ({desc})", {"ref": ref},
                               hint="Compare the symbol with the data sheet's pin table: a naming difference is "
                                    "fine, a different pin order is not.", key=f"pinout:names:{ref}"))
    if unverified:
        out.append(Finding("sch.pinout", "info", f"pinout not verified for {len(unverified)} part"
                           f"{'s' if len(unverified) != 1 else ''}: {', '.join(unverified[:6])}"
                           f"{' ...' if len(unverified) > 6 else ''}", key="pinout:unverified"))
    return out


# ----------------------------------------------------------------------------- bom.package
_PASSIVE = {(0.4, 0.2): "01005", (0.6, 0.3): "0201", (1.0, 0.5): "0402", (1.6, 0.8): "0603", (2.0, 1.25): "0805",
            (3.2, 1.6): "1206", (3.2, 2.5): "1210", (4.5, 3.2): "1812", (5.0, 2.5): "2010", (6.4, 3.2): "2512"}
_FAMILY = re.compile(r"(TSOT-23(?:-\d+)?|SOT-23(?:-\d+)?|SOT-223(?:-\d+)?|SOT-89(?:-\d+)?|SOT-323(?:-\d+)?|SOT-363|SOT-563|"
                     r"SOT-883|SC-70(?:-\d+)?|SOIC-\d+|SOP-\d+|SSOP-\d+|TSSOP-\d+|MSOP-\d+|VSSOP-\d+|HTSSOP-\d+|"
                     r"W?QFN-\d+|VQFN-\d+|UQFN-\d+|W?DFN-\d+|WSON-\d+|SON-\d+|LQFP-\d+|TQFP-\d+|QFP-\d+|LGA-\d+|BGA-\d+|"
                     r"TO-252(?:-\d+)?|TO-263(?:-\d+)?|TO-220(?:-\d+)?|TO-92|SOD-123F?|SOD-323F?|SOD-523|SOD-923|"
                     r"SMAF?|SMBF?|SMC|DO-214A[ABC])", re.I)


def _family(s):
    m = _FAMILY.search(s or "")
    if not m:
        return None
    f = m.group(1).upper()
    # one family under the names vendors use: JLC's SOP-8 is a SOIC-8, TI's VSSOP-8 an MSOP-8, WSON a DFN
    f = re.sub(r"^(?:W|V|U)(QFN|DFN)", r"\1", f)
    f = re.sub(r"^(?:W|V|U)?SON-", "DFN-", f)
    f = re.sub(r"^SOP-", "SOIC-", f)
    f = re.sub(r"^VSSOP-", "MSOP-", f)
    f = re.sub(r"^HTSSOP-", "TSSOP-", f)
    f = re.sub(r"^(?:L|T)QFP-", "QFP-", f)
    f = {"DO-214AC": "SMA", "DO-214AA": "SMB", "DO-214AB": "SMC", "SMAF": "SMA", "SMBF": "SMB",
         "SOD-123F": "SOD-123", "SOD-323F": "SOD-323"}.get(f, f)
    f = re.sub(r"^(SOT-23|TSOT-23)$", r"\1-3", f)                   # SOT-23 = SOT-23-3
    f = re.sub(r"^(SOT-223|SOT-89|TO-252|TO-263|TO-220)(-\d+)?$", r"\1", f)    # tab counted or not: same family
    f = f.replace("TSOT-23", "SOT-23")
    return f


def package_of_footprint(lib_id):
    """(family, body {w, l} or None, passive size code or None) from a KiCad footprint name."""
    name = (lib_id or "").split(":")[-1]
    m = re.search(r"(?:^|_)(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)(?:_|$)", name)
    size = m.group(1) if m and re.match(r"(R|C|L|LED|D|F|FB|CP)_", name) else None
    body = None
    m = re.search(r"_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm", name)
    if m:
        body = sorted((float(m.group(1)), float(m.group(2))))
    return _family(name), body, size


def package_of_jlc(title):
    """(family, body or None, passive size code or None) from an EasyEDA package title
    ('SOIC-8_L5.3-W5.3-P1.27-LS8.0-BL', 'C0805', 'LED-SMD_L1.6-W0.8-R-RD')."""
    t = title or ""
    size = None
    m = re.fullmatch(r"[RCLF]?(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512)", t.strip(), re.I)
    if m:
        size = m.group(1)
    body = None
    m = re.search(r"L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)", t)
    if m:
        body = sorted((float(m.group(1)), float(m.group(2))))
        if size is None and not _family(t):
            for (l, w), code in _PASSIVE.items():
                if abs(body[1] - l) <= 0.15 and abs(body[0] - w) <= 0.15:
                    size = code
    return _family(t), body, size


@check("bom.package", "Footprints match the parts' packages", "Parts & BOM", needs=("netlist", "pcb"), timeout=180)
def bom_package(ctx):
    """The footprint must be the package the ordered part comes in. An ATtiny85-20SU is the wide 208-mil
    SOIC-8 (5.3 mm body); on the common 3.9 mm SOIC-8 footprint its leads miss the pads. SOT-23-5 and
    SOT-23-6, QFN-20 and QFN-24, 0603 and 0805 are just as easy to swap. Each assembled part with an
    LCSC code is compared with the package of JLC's own footprint: family and pin count, body size
    within 0.3 mm, chip size for passives."""
    nl, board = ctx.netlist, ctx.board
    db = None
    from tw.jlc import LookupFailed
    out, unverified = [], []
    for ref, part in sorted(nl.parts.items()):
        fp = board.footprints.get(ref)
        if fp is None or not _assembled(nl, ref, part):
            continue
        code = _lcsc(part, fp)
        if not code:
            continue
        if db is None:
            db = _parts_db(ctx)
        try:
            jpads, title, _ = db.jlc_footprint(code)
        except LookupFailed as e:
            unverified.append(f"{ref} ({e})")
            continue
        if not title:
            unverified.append(f"{ref} (no package for {code})")
            continue
        fam, body, size = package_of_footprint(fp.lib_id)
        jfam, jbody, jsize = package_of_jlc(title)
        ours = fp.lib_id.split(":")[-1]
        why = None
        if size and jsize and size != jsize:
            why = f"{size} footprint, but {code} is {jsize}"
        elif fam and jfam and fam != jfam:
            why = f"{fam} footprint, but {code} is {jfam}"
        elif fam and jfam and body and jbody and max(abs(a - b) for a, b in zip(body, jbody)) > 0.3:
            why = (f"{fam} body {body[0]:g} x {body[1]:g} mm, but {code} is {jbody[0]:g} x {jbody[1]:g} mm "
                   f"({title.split('_')[0]})")
        elif not fam and not size:
            nums = {p.num for p in fp.pads if p.num}
            if ref[:1] in ("U", "Q") and jpads and len(nums) != len(jpads) and len(nums) and \
                    abs(len(nums) - len(jpads)) not in (1,):             # an exposed pad counted or not
                why = f"{len(nums)} pads on {ours}, {len(jpads)} on {code}'s footprint ({title})"
            elif not jfam and not jsize:
                continue                                                # names say nothing either way
        if why:
            out.append(Finding("bom.package", "error", f"{ref} ({part.get('value', '')}): {why}", {"ref": ref},
                               hint="Change the footprint to the part's package (or the part to one in this "
                                    "package). The data sheet's package drawing settles it.",
                               key=f"package:{ref}"))
    if unverified:
        out.append(Finding("bom.package", "info", f"package not verified for {len(unverified)} part"
                           f"{'s' if len(unverified) != 1 else ''}: {', '.join(unverified[:6])}"
                           f"{' ...' if len(unverified) > 6 else ''}", key="package:unverified"))
    return out


# ----------------------------------------------------------------------------- power.domains
DRIVER = ("output", "bidirectional", "tri_state", "power_out")
RECEIVER = ("input", "bidirectional", "tri_state")
IO_RAIL = re.compile(r"^(VDDIO|VCCIO|IOVDD|VIO|VDD_IO|VCC_IO|OVDD|DVDD_IO)", re.I)
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*V?\s*(?:~|to|-|…)\s*(\d+(?:\.\d+)?)\s*V", re.I)
SUPPLY_PARAMS = ("Operating Voltage", "Supply Voltage", "Voltage - Supply", "Supply Voltage Range", "VCC",
                 "Working Voltage", "Operating Supply Voltage")


def supply_range(detail):
    """(min, max) volts from an LCSC detail's parameters, or None."""
    params = (detail or {}).get("params") or {}
    for k in SUPPLY_PARAMS:
        for pk, v in params.items():
            if pk and pk.strip().lower() == k.lower() and v:
                m = _RANGE.search(str(v))
                if m:
                    lo, hi = sorted((float(m.group(1)), float(m.group(2))))
                    return lo, hi
    return None


def ic_rails(nl, grounds, power):
    """{ic ref: (io rail, {rails})}: the supply rails each IC's power pins sit on, and the one its logic
    runs at (a VDDIO / VCCIO pin's rail, else the only rail). Regulators and parts with a power output
    are left out: their input rail is not a logic level."""
    out = {}
    for ref, part in nl.parts.items():
        if ref[:1] not in ("U", "I", "M") or part.get("dnp"):
            continue
        rails, io = set(), None
        power_out = False
        for pin in nl.pins_of(ref):
            net, typ, name = nl.net_of(ref, pin), nl.pin_type(ref, pin), (nl.pin_name(ref, pin) or "").upper()
            if typ == "power_out" or name in ("VOUT", "VO", "OUT", "SW", "LX"):
                power_out = True
            if not net or net == "NC" or net in grounds:
                continue
            if typ == "power_in" and net in power:
                rails.add(net)
                if IO_RAIL.match(name):
                    io = net
        if power_out or not rails:
            continue
        if io is None and len(rails) == 1:
            io = next(iter(rails))
        out[ref] = (io, rails)
    return out


@check("power.domains", "Parts on different supply rails are not wired together unshifted", "Power", needs=("netlist",))
def power_domains(ctx):
    """Every IC's logic runs at its supply rail. A 5 V push-pull output wired straight to a 3.3 V part's
    input drives it past its absolute maximum (the input's protection diode conducts into the 3.3 V
    rail); a 3.3 V output into a 5 V CMOS input may not reach its high threshold. Open-drain lines are
    fine when they are pulled up to the lower rail, so the pull-up rail is checked instead. With LCSC
    part data in the sourcing cache, each IC's supply rail is also checked against its rated supply
    range."""
    from .power import rail_voltages
    nl = ctx.netlist
    grounds, power = ctx.ground_nets(), ctx.power_nets()
    volts = rail_voltages(ctx)
    ics = ic_rails(nl, grounds, power)
    if len(ics) < 1:
        raise NotApplicable("no ICs with supply pins")
    level = {ref: volts.get(io) for ref, (io, _) in ics.items() if io and volts.get(io) is not None}
    out = []
    by_net = nl.nets_short()
    for net, nodes in sorted(by_net.items()):
        if not net or net == "NC" or net in grounds or net in power:
            continue
        ends = [(r, p, nl.pin_type(r, p)) for r, p in nodes if r in level]
        vs = sorted({level[r] for r, _, _ in ends})
        if len(vs) < 2 or vs[-1] - vs[0] < 0.6:
            continue
        hi = [(r, p) for r, p, t in ends if level[r] == vs[-1] and t in DRIVER]
        lo = [(r, p) for r, p, t in ends if level[r] == vs[0] and t in RECEIVER]
        if hi and lo:
            (hr, hp), (lr, lp) = hi[0], lo[0]
            out.append(Finding("power.domains", "warning",
                               f"{net}: {hr} drives it at {vs[-1]:g} V and {lr} (a {vs[0]:g} V part) reads it on pin {lp}",
                               {"net": net, "ref": lr},
                               hint="Add a level shifter or a divider, or run both parts from one rail (unless the "
                                    "data sheet says the input is tolerant of the higher voltage).",
                               key=f"domains:{net}"))
            continue
        hi_in = [(r, p) for r, p, t in ends if level[r] == vs[-1] and t in RECEIVER]
        lo_out = [(r, p) for r, p, t in ends if level[r] == vs[0] and t in ("output", "tri_state")]
        if hi_in and lo_out:
            out.append(Finding("power.domains", "info",
                               f"{net}: {lo_out[0][0]} ({vs[0]:g} V) drives {hi_in[0][0]} ({vs[-1]:g} V): check the "
                               f"{vs[-1]:g} V input's high threshold", {"net": net}, key=f"domains:low:{net}"))
    # open-drain lines and I2C: the pull-up rail must not be above any part on the line
    for net, nodes in sorted(by_net.items()):
        if not net or net in grounds or net in power:
            continue
        od = any(nl.pin_type(r, p) in ("open_collector", "open_emitter") for r, p in nodes) or \
            re.search(r"(^|_)(SDA|SCL)(\d|_|$)", net, re.I)
        if not od:
            continue
        parts_on = [r for r, _ in nodes if r in level]
        if not parts_on:
            continue
        low = min(level[r] for r in parts_on)
        for r, p in nodes:
            if not r.startswith("R") or r.startswith("RN"):
                continue
            pins = nl.pins_of(r)
            if len(pins) != 2:
                continue
            other = nl.net_of(r, pins[1] if pins[0] == p else pins[0])
            v = volts.get(other)
            if v is not None and v - low >= 0.6:
                who = min(parts_on, key=lambda x: level[x])
                out.append(Finding("power.domains", "warning",
                                   f"{net} is pulled up to {other} ({v:g} V) by {r}, but {who} on it runs at {low:g} V",
                                   {"net": net, "ref": r}, hint=f"Pull it up to the {low:g} V rail, or add a bidirectional "
                                   "level shifter (a MOSFET pair for I2C).", key=f"domains:pull:{net}"))
                break
    # rated supply ranges (from the sourcing cache or a lookup within the budget)
    if not ctx.offline:
        from tw.jlc import LookupFailed
        db = None
        for ref, (io, rails) in sorted(ics.items()):
            code = _lcsc(nl.parts[ref])
            if not code:
                continue
            if db is None:
                db = _parts_db(ctx)
            try:
                rng = supply_range(db.detail(code))
            except LookupFailed:
                continue
            if not rng:
                continue
            for rail in sorted(rails):
                v = volts.get(rail)
                if v is not None and v > rng[1] + 1e-6:
                    out.append(Finding("power.domains", "error",
                                       f"{ref} ({nl.parts[ref].get('value', '')}) is supplied from {rail} ({v:g} V) but is "
                                       f"rated {rng[0]:g}-{rng[1]:g} V", {"ref": ref},
                                       hint="Supply it from a rail inside its range.", key=f"domains:rating:{ref}:{rail}"))
    return out
