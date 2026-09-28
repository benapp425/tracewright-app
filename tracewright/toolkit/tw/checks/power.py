"""Power: copper sized for the current, and decoupling next to every supply pin."""
import math, re, fnmatch, collections
from . import check, Finding, NotApplicable
from .. import geom

POWER_PIN = re.compile(r"^(VCC|VDD|AVDD|DVDD|VDDA|VDDIO|VDD_IO|VCCIO|VIN|PVIN|AVIN|VBAT|VS|V\+|VCC\d?|VDD\d?|VDDQ|VIO|"
                       r"VLOGIC|VREG|VSYS|VM|VBUS_IN|IOVDD|DVCC|AVCC)(_?\d*)?$", re.I)


def ipc2221_width(current, rise_c=10.0, copper_mm=0.035, internal=False):
    """Track width (mm) for a current (A): IPC-2221 I = k dT^0.44 A^0.725 (A in mil^2)."""
    k = 0.024 if internal else 0.048
    area_mil2 = (current / (k * rise_c ** 0.44)) ** (1 / 0.725)
    t_mil = copper_mm / 0.0254
    return area_mil2 / t_mil * 0.0254


def _currents(ctx):
    return ctx.setting("checks.currents", {}) or {}


def _current_for(net, table):
    short = net.rsplit("/", 1)[-1]
    for pat, amps in table.items():
        if fnmatch.fnmatchcase(short, pat) or fnmatch.fnmatchcase(net, pat):
            return float(amps)
    return None


def copper_thickness(ctx, layer):
    """tracewright.json fab.copper_mm.<layer>, else the board's own stackup, else the fab profile."""
    t = ctx.setting(f"fab.copper_mm.{layer}")
    if t:
        return float(t)
    if ctx.available("pcb"):
        t = ctx.board.copper_mm(layer)
        if t:
            return t
    outer = ctx.fab.get("outer_copper_mm", 0.035)
    inner = ctx.fab.get("inner_copper_mm", 0.0175)
    return outer if layer in ("F.Cu", "B.Cu") else inner


def _pads_for(board, spec, net):
    """'U101.2' -> [('U101', '2')]; 'U101' -> every pad of U101 on the net."""
    spec = str(spec)
    if "." in spec:
        r, n = spec.split(".", 1)
        return [(r, n)]
    fp = board.footprints.get(spec)
    return [(fp.ref, p.num) for p in fp.pads if p.net == net] if fp else []


def _terminals(board, nl, net):
    """Pads on the net that current flows through: not capacitors, pull resistors or test points."""
    out = []
    for p in board.pads():
        if p.net != net:
            continue
        pre = p.ref.rstrip("0123456789")
        if pre in ("C", "TP", "FID", "H", "MH"):
            continue
        if pre in ("U", "IC") and nl is not None and nl.pin_type(p.ref, p.num) not in ("power_in", "power_out"):
            continue                             # sense / feedback / enable inputs carry no load current
        if pre == "R":
            fp = board.footprints.get(p.ref)
            v = (fp.value if fp else "").lower().replace(" ", "")
            if not any(x in v for x in ("m", "0r", "0ohm")) or v.endswith("k"):
                continue                         # a pull-up / divider, not a shunt or jumper
        out.append((p.ref, p.num, p))
    return out


def _source(nl, terms):
    for r, n, p in terms:
        if nl and nl.pin_type(r, n) == "power_out":
            return (r, n)
    conn = [t for t in terms if t[0].rstrip("0123456789") in ("J", "P", "BT", "CN", "L", "F", "Q")]
    pool = conn or terms
    return max(pool, key=lambda t: t[2].w * t[2].h)[:2] if pool else None


def _check_path(ctx, g, net, src, dsts, amps, rise, label, sev="error", why=""):
    """The worst (narrowest) of the widest paths src -> each dst, against the IPC width for amps."""
    worst = None
    for dst in dsts:
        if tuple(dst) == tuple(src):
            continue
        w, path = g.widest_path(src, dst)
        if path is None:
            continue
        need_on = {}
        bad = []
        for info in path:
            if info[0] != "track":
                continue
            _, layer, a, b, tw_ = info
            need = need_on.setdefault(layer, ipc2221_width(amps, rise, copper_thickness(ctx, layer),
                                                             internal=layer not in ("F.Cu", "B.Cu")))
            if tw_ + 1e-6 < need:
                bad.append((tw_, need, layer, a, b))
        if not bad:
            continue
        # short necks into the end pads are normal (a wide trunk narrows for the last millimetre)
        body = [x for x in bad if geom.dist(x[3], x[4]) >= 1.0]
        if not body:
            continue
        x = min(body, key=lambda x: x[0] - x[1])
        if worst is None or x[0] - x[1] < worst[0][0] - worst[0][1]:
            worst = (x, dst)
    if worst:
        (tw_, need, layer, a, b), dst = worst
        hint = "Widen the track (or add a pour) along this path; necks under 1 mm into pads are exempt."
        if why:
            hint += " " + why
        return Finding("power.width", sev,
                       f"{label}: {amps:g} A from {src[0]}.{src[1]} to {dst[0]}.{dst[1]} must pass a {tw_:.2f} mm track on "
                       f"{layer}; {need:.2f} mm is needed for a {rise:g} C rise",
                       {"net": net, "layer": layer, "x": (a[0] + b[0]) / 2, "y": (a[1] + b[1]) / 2},
                       hint=hint, key=f"power.width:{net}:{src}:{dst}")
    return None


FUSE_A = re.compile(r"(\d+(?:\.\d+)?)\s*(mA|A)(?![a-z])", re.I)
SUPPRESSOR = re.compile(r"zener|tvs|esd|bzt|bzx|mmsz|smaj|smbj|smcj|sm6t|p6ke|1\.5ke|pesd|usblc|tpd\d", re.I)


def fuse_amps(part):
    """A fuse's rated (or PTC hold) current in A from its fields or value ('10A', '500mA', '1.1A hold')."""
    f = part.get("fields", {})
    for txt in (f.get("Current"), f.get("Rating"), f.get("I_hold"), f.get("Hold current"), part.get("value", "")):
        m = FUSE_A.search(str(txt or ""))
        if m:
            v = float(m.group(1))
            return v / 1000.0 if m.group(2).lower() == "ma" else v
    return None


def fuse_paths(board, nl, grounds):
    """[(fuse ref, fuse pad, net, other ref, amps)]: the copper a fuse's rated current must cross, from
    each fuse pad to the connectors, power switches, inductors, series diodes and other fuses on its
    net (a rail with many loads and no such part is left alone)."""
    out = []
    for ref, part in sorted(nl.parts.items()):
        if ref.rstrip("0123456789").upper() not in ("F", "FUSE", "PTC") or part.get("dnp"):
            continue
        amps = fuse_amps(part)
        fp = board.footprints.get(ref)
        if not amps or not fp:
            continue
        for pad in fp.pads:
            if not pad.net or nl.short(pad.net) in grounds:
                continue
            cands = set()
            for q in board.pads():
                if q.net != pad.net or q.ref == ref:
                    continue
                pre = q.ref.rstrip("0123456789").upper()
                if pre in ("J", "P", "CN", "X", "CON", "Q", "L", "F", "FUSE", "PTC", "SW", "TB", "BT"):
                    cands.add(q.ref)
                elif pre == "D":
                    dp = nl.parts.get(q.ref, {})
                    nets = {nl.net_of(q.ref, x) for x in nl.pins_of(q.ref)}
                    if not (nets & grounds) and not SUPPRESSOR.search(dp.get("value", "") + " " + dp.get("footprint", "")):
                        cands.add(q.ref)
            for other in sorted(cands):
                out.append((ref, pad.num, pad.net, other, amps))
    return out


@check("power.width", "Power copper sized for its current", "Power", needs=("pcb",))
def power_width(ctx):
    """Current paths through the copper. tracewright.json:
        checks.power_paths: [{"net": "+5V", "from": "U101.2", "to": "J201", "amps": 3}]   exact paths
        checks.currents:    {"VBAT": 7}      the whole net carries it (source -> every load pad)
    For each path the widest route through tracks, vias and pours is found and its narrowest track
    compared with the IPC-2221 width (10 C rise, the layer's copper weight). Use net-wide currents
    only for nets without light branches; a rail feeding many small loads needs power_paths.

    Without declarations the check does not pass blind: every fuse's rating is taken as a current its
    copper must carry (to the connectors, switches and series parts on its nets, at a 20 C rise; a
    warning, since the real load may be lower), and the rails whose current is neither declared nor
    bounded by a fuse are listed as unchecked -- a warning until they are declared or waived."""
    from ..copper import NetGraph
    b = ctx.board
    table = _currents(ctx)
    paths = ctx.setting("checks.power_paths", []) or []
    rise = float(ctx.setting("checks.temp_rise_c", 10.0))
    nl = ctx.netlist if ctx.available("netlist") else None
    out = []
    graphs = {}

    def graph(net):
        if net not in graphs:
            graphs[net] = NetGraph(b, net)
        return graphs[net]
    full = {n.rsplit("/", 1)[-1]: n for n in b.nets}
    for pth in paths:
        net = full.get(pth.get("net", ""), pth.get("net", ""))
        if net not in b.nets:
            out.append(Finding("power.width", "warning", f"power path net {pth.get('net')} is not on the board",
                               key=f"power.width:nonet:{pth.get('net')}"))
            continue
        srcs, dsts = _pads_for(b, pth.get("from", ""), net), _pads_for(b, pth.get("to", ""), net)
        if not srcs or not dsts:
            out.append(Finding("power.width", "warning", f"power path {pth}: no pads found on {net}", {"net": net},
                               key=f"power.width:nopads:{net}:{pth.get('from')}"))
            continue
        g = graph(net)
        best = max(srcs, key=lambda s: max((g.widest_path(s, d)[0] for d in dsts), default=0))
        conn = [d for d in dsts if g.widest_path(best, d)[1] is not None]
        if not conn:
            out.append(Finding("power.width", "error", f"{net}: no copper path from {pth.get('from')} to {pth.get('to')}",
                               {"net": net}, key=f"power.width:open:{net}:{pth.get('from')}:{pth.get('to')}"))
            continue
        f = _check_path(ctx, g, net, best, conn[:1] if len(conn) == 1 else conn, float(pth.get("amps", 0)), rise,
                        f"{net.rsplit('/', 1)[-1]}")
        if f:
            out.append(f)
    checked = set()
    for net in sorted(b.nets):
        amps = _current_for(net, table)
        if amps is None:
            continue
        checked.add(net.rsplit("/", 1)[-1])
        terms = _terminals(b, nl, net)
        src = _source(nl, terms)
        if not src:
            continue
        g = graph(net)
        f = _check_path(ctx, g, net, src, [(r, n) for r, n, _ in terms], amps, rise, net.rsplit("/", 1)[-1])
        if f:
            out.append(f)
    checked |= {full.get(pth.get("net", ""), pth.get("net", "")).rsplit("/", 1)[-1] for pth in paths}
    # fuses bound the current of the copper around them
    grounds = ctx.ground_nets() if nl is not None else set()
    fused = set()
    if nl is not None:
        frise = max(rise, float(ctx.setting("checks.fuse_temp_rise_c", 20.0)))
        for ref, pad, net, other, amps in fuse_paths(b, nl, grounds):
            short = net.rsplit("/", 1)[-1]
            fused.add(short)
            if short in checked:                      # a declared current wins over the fuse's rating
                continue
            dsts = [(other, p.num) for p in b.footprints[other].pads if p.net == net] if other in b.footprints else []
            g = graph(net)
            conn = [d for d in dsts if g.widest_path((ref, pad), d)[1] is not None]
            if not conn:
                continue
            f = _check_path(ctx, g, net, (ref, pad), conn, amps, frise, f"{short} (fused by {ref}, {amps:g} A)",
                            sev="warning", why=f"The current is {ref}'s rating: if the real load is lower, declare it "
                                               f"in checks.power_paths and this becomes an exact check.")
            if f:
                f.key = f"power.width:fuse:{ref}:{pad}:{other}"
                out.append(f)
    # rails nobody has sized: not a pass
    rails = sorted(ctx.power_nets()) if nl is not None else \
        sorted(n.rsplit("/", 1)[-1] for n in b.nets if re.match(r"^(\+|V)", n.rsplit("/", 1)[-1]))
    routed = {t.net.rsplit("/", 1)[-1] for t in b.tracks} | {z.net.rsplit("/", 1)[-1] for z in b.zones if not z.is_rule_area}
    open_ = [r for r in rails if r not in checked and r not in fused and r in routed]
    if open_:
        out.append(Finding("power.width", "warning",
                           f"no current declared for {len(open_)} rail{'s' if len(open_) > 1 else ''} "
                           f"({', '.join(open_[:8])}{' ...' if len(open_) > 8 else ''}): their copper widths are unchecked",
                           {"net": open_[0]},
                           hint='Declare what each carries, e.g. "checks": {"currents": {"' + open_[0] + '": 0.5}} or '
                                '{"power_paths": [{"net": "' + open_[0] + '", "from": "U1.2", "to": "J1", "amps": 2}]} in '
                                'tracewright.json; waive this finding if the rails are known to be light.',
                           key="power.width:undeclared"))
    return out


def _cap_between(nl, net, grounds):
    """Capacitors with one pin on `net` and the other on a ground net."""
    out = []
    for ref, p in nl.parts.items():
        if not ref.startswith("C") or ref.startswith("CN") or p["dnp"]:
            continue
        pins = nl.pins_of(ref)
        if len(pins) != 2:
            continue
        n1, n2 = nl.net_of(ref, pins[0]), nl.net_of(ref, pins[1])
        if (n1 == net and n2 in grounds) or (n2 == net and n1 in grounds):
            out.append(ref)
    return out


def power_pins(nl, grounds):
    """[(ic ref, pin, net)] of supply-input pins on ICs."""
    out = []
    for (ref, pin), full in nl.pin.items():
        if not ref[:1] in ("U", "I") or ref.startswith("#"):
            continue
        net = nl.short(full)
        if not net or net == "NC" or net in grounds:
            continue
        name = nl.pin_name(ref, pin)
        typ = nl.pin_type(ref, pin)
        if typ == "power_in" or POWER_PIN.match(name or ""):
            out.append((ref, pin, net))
    return out


@check("power.decoupling", "Decoupling capacitor beside every supply pin", "Power", needs=("netlist",))
def power_decoupling(ctx):
    """Each IC supply pin (power-in type or a supply name) needs a capacitor to ground on its net;
    with a board, the nearest one must sit within checks.decoupling_mm (default 5 mm) of the pin."""
    nl = ctx.netlist
    grounds = ctx.ground_nets()
    limit = float(ctx.setting("checks.decoupling_mm", 5.0))
    b = ctx.board if ctx.available("pcb") else None
    out = []
    seen = set()
    by_ic = collections.defaultdict(list)
    for ref, pin, net in power_pins(nl, grounds):
        by_ic[(ref, net)].append(pin)
    for (ref, net), pins in sorted(by_ic.items()):
        caps = _cap_between(nl, net, grounds)
        if not caps:
            out.append(Finding("power.decoupling", "warning", f"{ref} {net} ({', '.join(pins[:4])}) has no capacitor to ground",
                               {"ref": ref, "net": net}, key=f"decoup:none:{ref}:{net}"))
            continue
        if not b or ref not in b.footprints:
            continue
        fp = b.footprints[ref]
        best = None
        for pin in pins:
            pp = fp.pad(pin)
            if not pp:
                continue
            for c in caps:
                cf = b.footprints.get(c)
                if not cf:
                    continue
                for cp in cf.pads:
                    if cp.net.rsplit("/", 1)[-1] != net:
                        continue
                    d = geom.dist((pp.x, pp.y), (cp.x, cp.y))
                    if best is None or d < best[0]:
                        best = (d, pin, c)
        if best and best[0] > limit and (ref, net) not in seen:
            seen.add((ref, net))
            out.append(Finding("power.decoupling", "warning",
                               f"{ref} {net}: nearest capacitor {best[2]} is {best[0]:.1f} mm from pin {best[1]} (limit {limit:g} mm)",
                               {"ref": ref, "net": net, "x": fp.x, "y": fp.y},
                               hint="Place the small capacitor right at the pin, on the same side, with a short ground return.",
                               key=f"decoup:far:{ref}:{net}"))
    return out


# --------------------------------------------------------------------------- inductive loads
RELAY_TXT = re.compile(r"relay|G5LE|G5V|G6[A-Z]|G5Q|G2R|HF\d{2}F|HFD\d|SRD-|JQC|TQ2|IM0\d|V23\d|RT\d{6}|ALQ\d|HK19F|"
                       r"HK4100|AZ\d{3}|FTR-|JW\d|DS2E|EC2-|TX2-|SRA-", re.I)
SSR_TXT = re.compile(r"G3VM|AQY|AQW|AQV|CPC1\d|TLP\d{3}|LCA\d{3}|PVT\d|PVN\d|ASSR|solid.?state|photo.?mos|\bSSR\b|optomos", re.I)
CLAMP_ARRAY = re.compile(r"ULN2\d0\d|ULQ2\d0\d|ULN28\d\d|ULQ28\d\d|MC141[3-6]|TBD62\d{3}|TPL7407|KID65\d{3}|SN7546[89]|"
                         r"MIC298[12]", re.I)
SELF_CLAMP = re.compile(r"MAX482[0-2]|MAX4896|DRV880[3-6]|DRV8860|DRV110|DRV103|DRV120|TPIC6[ABC]?\d{3}|NCV77\d\d|NCV7240|"
                        r"L9822|TLE72\d\d|VNQ\d|IPS\d|BTS\d|TPS[124]H|BSP7\d", re.I)
INDUCTIVE = re.compile(r"VALVE|SOLENOID|(^|[_\s])SOL(\d|$|[_\s])|MOTOR|(^|_)MOT(\d|$|_)|PUMP|COIL|(^|[_\s])FAN(\d|$|[_\s])|"
                       r"BUZZ|INJECT|MAGNET|(^|[_\s])LOCK|BRAKE|CLUTCH|RELAY|HORN|(^|[_\s])BELL", re.I)


def _txt(part):
    return " ".join(str(part.get(k, "")) for k in ("value", "part", "lib", "footprint", "description"))


def _diode_ends(nl, ref):
    """(cathode net, anode net) of a two-pin diode (by pin name, else KiCad's 1 = K, 2 = A), or None."""
    pins = nl.pins_of(ref)
    if len(pins) != 2:
        return None
    names = {x: (nl.pin_name(ref, x) or "").upper() for x in pins}
    k = next((x for x in pins if names[x] in ("K", "C", "CATHODE", "-")), None)
    a = next((x for x in pins if names[x] in ("A", "ANODE", "+")), None)
    if names[pins[0]] in ("A1", "A2") or names[pins[1]] in ("A1", "A2"):
        return ("bidir", nl.net_of(ref, pins[0]), nl.net_of(ref, pins[1]))
    if not k or not a:
        k, a = ("1", "2") if set(pins) == {"1", "2"} else (None, None)
    if not k:
        return None
    return (nl.net_of(ref, k), nl.net_of(ref, a))


def _is_led(ref, part):
    return ref.startswith("LED") or "LED" in _txt(part).upper()


def _switch_pins(nl, net_full):
    """[(ref, pin, kind)] of the pins on a net that switch a load: transistor drains/collectors, IC
    open-collector / output pins."""
    out = []
    for r, x in nl.nets.get(net_full, []):
        name = (nl.pin_name(r, x) or "").upper()
        typ = nl.pin_type(r, x)
        if r.startswith("Q") and name in ("D", "C", "DRAIN", "COLLECTOR"):
            out.append((r, x, "transistor"))
        elif r[:1] in ("U", "I") and (typ in ("open_collector", "open_emitter", "output", "power_out") or
                                      re.match(r"^(OUT|O)\d*[AB]?$|^OUT", name)):
            out.append((r, x, "ic"))
    return out


@check("power.flyback", "Relay coils and inductive loads have a flyback path", "Power", needs=("netlist",))
def power_flyback(ctx):
    """Switching off a relay coil (or a valve, solenoid or motor) makes a voltage spike that kills the
    transistor driving it and arcs the contacts. For each relay the driver node (the net between
    its coil and a transistor or driver IC) must have a clamp: a diode back to the coil supply
    (cathode on the supply), a TVS, or a driver array with its COM pin on the coil supply (ULN2003,
    ULN2803, TPL7407...). A diode the wrong way round shorts the supply when the driver turns on.
    Connector outputs switched by a relay or transistor whose name says the load is inductive
    (VALVE, SOLENOID, MOTOR, PUMP, FAN...) need a freewheel diode across the load."""
    nl = ctx.netlist
    grounds, rails = ctx.ground_nets(), ctx.power_nets()
    full = {nl.short(n): n for n in nl.nets}
    out = []
    relays = [r for r, p in sorted(nl.parts.items()) if not p.get("dnp") and not SSR_TXT.search(_txt(p)) and
              (r.rstrip("0123456789").upper() in ("K", "RL", "RY", "RLY") or RELAY_TXT.search(_txt(p))) and len(nl.pins_of(r)) >= 3]
    if not relays and not any(INDUCTIVE.search(nl.short(n) or "") for n in nl.nets):
        raise NotApplicable("no relays or inductive loads on this board")
    diodes = {r: _diode_ends(nl, r) for r, p in nl.parts.items() if r.startswith(("D", "TVS", "Z")) and not _is_led(r, p)}
    contact_nets = set()
    for k in relays:
        pins = nl.pins_of(k)
        nets = {x: nl.net_of(k, x) for x in pins}
        drivers = [(x, n) for x, n in nets.items() if n and n != "NC" and _switch_pins(nl, full.get(n))]
        if not drivers:
            continue
        coil = set()
        for x, n in drivers:
            coil.add(x)
            sw = _switch_pins(nl, full[n])
            ok, why = False, None
            supply = {m for y, m in nets.items() if y != x and m in rails | grounds}
            for d, ends in diodes.items():
                if not ends:
                    continue
                if ends[0] == "bidir":
                    if n in ends[1:] and (set(ends[1:]) - {n}) & (supply | grounds):
                        ok = True
                    continue
                cat, an = ends
                if n not in (cat, an):
                    continue
                other = an if cat == n else cat
                if other not in supply and other not in grounds:
                    continue
                coil |= {y for y, m in nets.items() if m == other}
                hi = other if other in rails else n            # the side at the higher potential while on
                if (other in grounds and cat == n) or (other in rails and cat == other):
                    ok = True
                elif other in rails or other in grounds:
                    why = f"{d} is the wrong way round across {k}'s coil: it shorts {hi} when the driver turns on"
            for r, pin, kind in sw:
                part = nl.parts.get(r, {})
                if kind == "ic" and CLAMP_ARRAY.search(_txt(part)):
                    com = [y for y in nl.pins_of(r) if re.match(r"^(COM|COMMON|K|CLAMP|VCLAMP|CD\+?)$", (nl.pin_name(r, y) or "").upper())]
                    cnets = {nl.net_of(r, y) for y in com}
                    if cnets & supply:
                        ok = True
                        coil |= {y for y, m in nets.items() if m in cnets}
                    elif not com or cnets <= {"NC", None, ""}:
                        why = f"{r}'s COM pin is not connected, so its clamp diodes do not protect {k}"
                    else:
                        why = f"{r}'s COM pin is on {', '.join(sorted(c for c in cnets if c))}, not {k}'s coil supply " \
                              f"({', '.join(sorted(supply))}), so its clamp diodes do not protect {k}"
                elif kind == "ic" and SELF_CLAMP.search(_txt(part)):
                    ok = True
            if why and not ok:
                out.append(Finding("power.flyback", "error", why, {"ref": k, "net": n}, key=f"flyback:bad:{k}:{n}"))
            elif not ok:
                drv = ", ".join(sorted({r for r, _, _ in sw}))
                unknown_ic = all(kind == "ic" for _, _, kind in sw)
                out.append(Finding("power.flyback", "warning" if unknown_ic else "error",
                                   f"{k}'s coil ({n}, driven by {drv}) has no flyback diode" +
                                   (" -- unless the driver IC clamps it internally" if unknown_ic else ""),
                                   {"ref": k, "net": n},
                                   hint="Add a diode across the coil, cathode on the coil supply (1N4148W or SS14), or "
                                        "use a driver with clamp diodes and wire its COM pin to the coil supply.",
                                   key=f"flyback:none:{k}:{n}"))
        contact_nets |= {m for y, m in nets.items() if y not in coil and m and m != "NC" and m not in grounds}
    # connector outputs a relay or transistor switches
    for n, nodes in nl.nets_short().items():
        if not n or n == "NC" or n in grounds or n in rails:
            continue
        conns = sorted({r for r, _ in nodes if r.startswith(("J", "P", "CN", "X"))})
        if not conns:
            continue
        near = {n}
        for r, x in nodes:                       # through a series fuse / resistor / ferrite
            if r.startswith(("F", "R", "FB")) and len(nl.pins_of(r)) == 2:
                near |= {nl.net_of(r, y) for y in nl.pins_of(r)}
        switched = bool(near & contact_nets) or any(_switch_pins(nl, full.get(m)) for m in near if full.get(m))
        if not switched:
            continue
        label = " ".join([n] + [nl.parts[c]["value"] for c in conns] + [nl.parts[c].get("description", "") for c in conns])
        if not INDUCTIVE.search(label):
            continue
        clamped = False
        for d, ends in diodes.items():
            if ends and ends[0] != "bidir" and (set(ends) & near) and (set(ends) & (grounds | rails)):
                clamped = True
        if not clamped:
            out.append(Finding("power.flyback", "warning",
                               f"{n} ({', '.join(conns)}) switches an inductive load with no freewheel diode across it",
                               {"net": n, "ref": conns[0]},
                               hint="Put a diode across the load at the connector (cathode to the positive side): SS34 or "
                                    "similar, rated for the load current.", key=f"flyback:out:{n}"))
    return out


# --------------------------------------------------------------------------- capacitor voltage ratings
VOLT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*V(?:DC)?(?![a-z])", re.I)


def rail_voltage(name):
    """A rail's voltage from its name (+3V3, V3V3 3.3; P12V, VIN_12V 12; VBUS 5), or None."""
    s = name.upper()
    m = re.search(r"(?<![0-9.])(\d{1,2})V(\d{1,2})(?![0-9])", s)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(?<![0-9.])(\d{1,3}(?:\.\d+)?)V(?![0-9A-Z])", s) or re.search(r"(?:^|[_+P])(\d{1,3}(?:\.\d+)?)V", s)
    if m:
        return float(m.group(1))
    if re.search(r"(^|_)VBUS($|_)|USB.*(5V|VBUS)", s):
        return 5.0
    return None


def rail_voltages(ctx):
    """{rail: volts}: tracewright.json checks.rail_voltages, the rail's name, else the highest rail it is
    fed from through a fuse, ferrite, 0-ohm link or series diode. USB VBUS counts as 20 V when a USB-PD
    sink controller is on the board."""
    nl = ctx.netlist
    rails, grounds = ctx.power_nets(), ctx.ground_nets()
    pd = any(re.search(r"CH224|STUSB4500|FUSB302|TPS6598|IP2721|HUSB238|AP33772|CYPD|PD\s?SINK", _txt(p), re.I)
             for p in nl.parts.values())
    volts = {}
    for r in rails:
        v = rail_voltage(r)
        if v is not None and re.search(r"VBUS", r, re.I) and pd:
            v = 20.0
        if v is not None:
            volts[r] = v
    volts.update({k: float(v) for k, v in (ctx.setting("checks.rail_voltages", {}) or {}).items()})
    links = []                                            # (from, to): the voltage that can reach `to`
    for ref, part in nl.parts.items():
        pins = nl.pins_of(ref)
        if len(pins) != 2 or part.get("dnp"):
            continue
        pre = ref.rstrip("0123456789").upper()
        v = part.get("value", "").lower().replace(" ", "")
        a, b = nl.net_of(ref, pins[0]), nl.net_of(ref, pins[1])
        if not a or not b or {a, b} & grounds or "NC" in (a, b):
            continue
        if pre in ("F", "FUSE", "PTC", "FB") or (pre == "R" and v in ("0", "0r", "0ohm")) or \
                (pre == "L" and ("bead" in v or "ferrite" in part.get("footprint", "").lower())):
            links += [(a, b), (b, a)]
        elif pre == "D" and not _is_led(ref, part) and not SUPPRESSOR.search(_txt(part)):
            ends = _diode_ends(nl, ref)
            if ends and ends[0] != "bidir":
                links.append((ends[1], ends[0]))          # a diode passes its anode's voltage to its cathode only
    for ref, part in nl.parts.items():                    # a power switch passes its input on
        if ref.startswith("Q"):
            ends = {(nl.pin_name(ref, x) or "").upper(): nl.net_of(ref, x) for x in nl.pins_of(ref)}
            if ends.get("S") in rails and ends.get("D") in rails:
                links += [(ends["S"], ends["D"]), (ends["D"], ends["S"])]
    named = set(volts)
    for _ in range(8):
        for x, y in links:
            if x in volts and y in rails and y not in named and volts[x] > volts.get(y, -1):
                volts[y] = volts[x]
    return volts


def cap_rating(part, detail=None):
    """(volts, where it came from) of a capacitor's voltage rating, or (None, None)."""
    f = part.get("fields", {})
    for k in ("Voltage", "Voltage Rating", "Rated Voltage", "Voltage - Rated", "VOLTAGE", "Rating", "V"):
        m = VOLT.search(str(f.get(k, "")))
        if m:
            return float(m.group(1)), f"its {k} field"
    m = VOLT.search(part.get("value", ""))
    if m:
        return float(m.group(1)), "its value"
    if detail:
        for k, v in (detail.get("params") or {}).items():
            if "voltage" in str(k).lower():
                m = VOLT.search(str(v))
                if m:
                    return float(m.group(1)), f"LCSC ({detail.get('lcsc')})"
        m = VOLT.search(str(detail.get("name") or detail.get("description") or ""))
        if m:
            return float(m.group(1)), f"LCSC ({detail.get('lcsc')})"
    return None, None


def cap_kind(part):
    t = _txt(part).lower()
    if "tantal" in t or "_tant" in t:
        return "tantalum", 2.0
    if "cp_" in t or "elec" in t or "polymer" in t or "alum" in t or "radial" in t:
        return "electrolytic", 1.25
    return "ceramic", 1.5


@check("power.cap_voltage", "Capacitors rated above their rail", "Power", needs=("netlist",))
def power_cap_voltage(ctx):
    """Every capacitor from a rail to ground needs a voltage rating above the rail, with margin: a
    ceramic loses most of its capacitance near its rating (DC bias) and fails when a hot-plug or
    load dump overshoots, so 1.5x the rail (electrolytic 1.25x, tantalum 2x). The rail's voltage comes
    from its name (V3V3, P12V, VIN_12V, VBUS; 20 V with a USB-PD sink), tracewright.json
    checks.rail_voltages, or what feeds it; the rating from the part's fields or value, else LCSC's
    data for its code (looked up and cached). A capacitor on a 10 V+ rail with no known rating is
    reported: nothing says it is safe."""
    from .bom import lcsc_of, placed_parts
    nl = ctx.netlist
    grounds = ctx.ground_nets()
    volts = rail_voltages(ctx)
    if not volts:
        raise NotApplicable("no rail with a known voltage (name it like 3V3 / 12V, or set checks.rail_voltages)")
    lookups = None
    if ctx.available("network") and ctx.setting("fab.house", "jlcpcb") == "jlcpcb":
        import time
        from ..jlc import Parts
        lookups = Parts(ctx.p.root, deadline=time.time() + float(ctx.setting("checks.lookup_budget_s", 90) or 90) / 2,
                        stop=ctx.stop)
    out, unknown = [], []
    for ref, part in sorted(placed_parts(ctx).items()):
        if not ref.startswith("C") or ref.startswith(("CN", "CON")):
            continue
        pins = nl.pins_of(ref)
        if len(pins) != 2:
            continue
        a, b = nl.net_of(ref, pins[0]), nl.net_of(ref, pins[1])
        rail = a if b in grounds else b if a in grounds else None
        if rail not in volts:
            continue
        v = volts[rail]
        rating, src = cap_rating(part)
        if rating is None and lookups is not None:
            code = lcsc_of(part["fields"], ctx)
            if code:
                try:
                    rating, src = cap_rating(part, lookups.detail(code, max_age_h=None))
                except Exception:
                    rating = None
        if rating is None:
            if v >= 10:
                unknown.append((ref, rail, v))
            continue
        kind, margin = cap_kind(part)
        if rating < v * 1.0001:
            out.append(Finding("power.cap_voltage", "error", f"{ref} ({part['value']}) is rated {rating:g} V (from {src}) "
                               f"but sits on {rail} ({v:g} V)", {"ref": ref, "net": rail},
                               hint="Use a part rated for at least 1.5x the rail.", key=f"capv:low:{ref}"))
        elif rating < v * margin:
            out.append(Finding("power.cap_voltage", "warning", f"{ref} ({part['value']}, {kind}) is rated {rating:g} V (from "
                               f"{src}) on {rail} ({v:g} V): under the {margin:g}x margin", {"ref": ref, "net": rail},
                               hint=f"A {kind} capacitor near its rating loses capacitance and fails on overshoot; choose "
                                    f"{v * margin:.0f} V or more.", key=f"capv:margin:{ref}"))
    if unknown:
        refs = ", ".join(f"{r} ({n})" for r, n, _ in unknown[:10])
        out.append(Finding("power.cap_voltage", "warning",
                           f"voltage rating unknown for {len(unknown)} capacitor{'s' if len(unknown) > 1 else ''} on 10 V+ "
                           f"rails: {refs}{' ...' if len(unknown) > 10 else ''}", {"ref": unknown[0][0], "net": unknown[0][1]},
                           hint="Put the rating in the value (\"10uF 25V\") or a Voltage field, or give each an LCSC code "
                                "so it can be looked up.", key="capv:unknown"))
    return out


# --------------------------------------------------------------------------- switch inputs at power-up
MCU_PIN = re.compile(r"^(GPIO|IO|P[A-K]\d|P\d|PA|PB|PC|PD|D\d|A\d|RA\d|RB\d|RC\d|GP\d|PIO)", re.I)


@check("power.gates", "Transistor gates and bases do not float at power-up", "Power", needs=("netlist",))
def power_gates(ctx):
    """A microcontroller's pins float while it resets, boots or is being flashed. A MOSFET gate (or a
    transistor base) driven only by such a pin floats with it, and the load it switches (a valve, a
    heater, a sensor rail) turns on by itself. Each gate / base needs a resistor to its source /
    emitter (or to the rail that keeps it off), directly or behind its series resistor."""
    nl = ctx.netlist
    grounds, rails = ctx.ground_nets(), ctx.power_nets()
    qs = [r for r in sorted(nl.parts) if r.startswith("Q") and not nl.parts[r].get("dnp")]
    if not qs:
        raise NotApplicable("no discrete transistors")
    full = {nl.short(n): n for n in nl.nets}
    out = []

    def resistors_on(net):
        res = []
        for r, x in nl.nets.get(full.get(net), []):
            if r.startswith("R") and not r.startswith("RN") and len(nl.pins_of(r)) == 2:
                other = [nl.net_of(r, y) for y in nl.pins_of(r) if y != x]
                res.append((r, other[0] if other else None))
        return res
    for q in qs:
        roles = {(nl.pin_name(q, x) or "").upper(): x for x in nl.pins_of(q)}
        ctl_pin = roles.get("G") or roles.get("GATE") or roles.get("B") or roles.get("BASE")
        if not ctl_pin:
            continue
        ref_pin = roles.get("S") or roles.get("SOURCE") or roles.get("E") or roles.get("EMITTER")
        ctl, src = nl.net_of(q, ctl_pin), nl.net_of(q, ref_pin) if ref_pin else None
        what = "gate" if ctl_pin in (roles.get("G"), roles.get("GATE")) else "base"
        if ctl in (None, "NC"):
            out.append(Finding("power.gates", "error", f"{q}'s {what} is not connected", {"ref": q}, key=f"gate:nc:{q}"))
            continue
        if ctl in rails or ctl in grounds:
            continue
        hold = set(grounds) | set(rails) | ({src} if src else set())

        def held(net, depth=0):
            for r, other in resistors_on(net):
                if other in hold:
                    return True
                if depth == 0 and other and other not in (net,) and held(other, 1):
                    return True
            return False
        if held(ctl):
            continue
        nets = {ctl} | {o for _, o in resistors_on(ctl) if o}
        mcu = sorted({f"{r}.{nl.pin_name(r, x) or x}" for m in nets for r, x in nl.nets.get(full.get(m), [])
                      if r[:1] in ("U", "I", "M", "A") and MCU_PIN.match(nl.pin_name(r, x) or "") and r != q})
        if not mcu:
            continue
        out.append(Finding("power.gates", "warning",
                           f"{q}'s {what} ({ctl}) is driven by {', '.join(mcu[:3])} with nothing holding it while that pin "
                           f"floats (reset, boot, flashing), so {q} can switch on by itself",
                           {"ref": q, "net": ctl},
                           hint=f"Add a pull resistor (10k-100k) from the {what} to {src or 'its source'}, the side that keeps "
                                f"{q} off.", key=f"gate:float:{q}"))
    return out
