"""Power, the layout side: regulators with their capacitors, regulator heat, voltage drop and via
capacity along the current paths, switching converters laid out around their hot loop, and pours that
are connected and stitched."""
import math, re, collections
from . import check, Finding, NotApplicable
from .. import geom
from .power import (ipc2221_width, _currents, _current_for, copper_thickness, _cap_between, rail_voltages,
                    _pads_for, _terminals, _source)

RHO_CU = 1.72e-5                      # ohm mm (copper at 20 C)
PLATING_MM = 0.020                    # via barrel plating (JLC: about 18-25 um)
SW_NAME = re.compile(r"^(SW\d?|LX\d?|PH|SWITCH)$", re.I)
IN_NAME = re.compile(r"^(VIN|VI|IN|PVIN|AVIN|VIN\d|INPUT)$", re.I)
OUT_NAME = re.compile(r"^(VOUT|VO|OUT|OUTPUT|VOUT\d)$", re.I)
FB_NAME = re.compile(r"^(FB|VFB|FB\d|ADJ|VSENSE|SENSE)$", re.I)
# junction-to-ambient (C/W) on a modest copper area; checks.theta_ja overrides per part
THETA_JA = [("SOT-23", 220.0), ("SC-70", 300.0), ("SOT-89", 110.0), ("SOT-223", 60.0), ("TO-252", 50.0),
            ("TO-263", 40.0), ("TO-220", 50.0), ("SOIC", 120.0), ("MSOP", 160.0), ("DFN", 60.0), ("QFN", 45.0)]


def pad_gap(p, q):
    """Edge-to-edge distance between two pads (0 when they touch)."""
    if geom.inside((p.x, p.y), q.poly) or geom.inside((q.x, q.y), p.poly):
        return 0.0
    d = min(min(geom.poly_dist(v, q.poly) for v in p.poly), min(geom.poly_dist(v, p.poly) for v in q.poly))
    return max(0.0, d)


def _is_inductor(ref, part):
    v = (part.get("value", "") + " " + part.get("footprint", "")).lower()
    return ref.startswith("L") and "bead" not in v and "ferrite" not in v and not ref.startswith("LED")


def regulators(nl, grounds):
    """[{ref, kind: linear | switching, ins, outs, sw, fb}] -- pins as (pin, net)."""
    out = []
    for ref, part in sorted(nl.parts.items()):
        if ref[:1] not in ("U", "I") or part.get("dnp"):
            continue
        ins, outs, sw, fb = [], [], [], []
        for pin in nl.pins_of(ref):
            net, name, typ = nl.net_of(ref, pin), (nl.pin_name(ref, pin) or "").strip(), nl.pin_type(ref, pin)
            name = re.sub(r"~\{([^}]*)\}", r"\1", name).split("/")[0]
            if not net or net == "NC" or net in grounds:
                continue
            if SW_NAME.match(name):
                sw.append((pin, net))
            elif FB_NAME.match(name):
                fb.append((pin, net))
            elif typ == "power_out" or OUT_NAME.match(name):
                outs.append((pin, net))
            elif IN_NAME.match(name) or (typ == "power_in" and name.upper().startswith("VIN")):
                ins.append((pin, net))
        if sw:
            has_l = any(_is_inductor(r, nl.parts.get(r, {})) for _, n in sw for r, _p in nl.nets_short().get(n, []))
            if has_l and ins:
                out.append({"ref": ref, "kind": "switching", "ins": ins, "outs": outs, "sw": sw, "fb": fb})
            continue
        if ins and outs:
            out.append({"ref": ref, "kind": "linear", "ins": ins, "outs": outs, "sw": [], "fb": fb})
    return out


def _farads(value):
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([pnuµm])(\d*)", str(value or ""))
    if not m:
        return None
    x = float(m.group(1) + ("." + m.group(3) if m.group(3) else ""))
    return x * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}[m.group(2)]


def _nearest_cap(board, nl, grounds, ref, pin, net):
    """(gap mm, cap ref) of the nearest capacitor from `net` to ground, measured from the pin's pad."""
    caps = _cap_between(nl, net, grounds)
    fp = board.footprints.get(ref)
    pad = fp.pad(pin) if fp else None
    best = None
    if pad is None:
        return None, caps
    for c in caps:
        cf = board.footprints.get(c)
        if cf is None:
            continue
        for q in cf.pads:
            if q.net.rsplit("/", 1)[-1] == net:
                g = pad_gap(pad, q)
                if best is None or g < best[0]:
                    best = (g, c)
    return best, caps


# ----------------------------------------------------------------------------- power.regulators
@check("power.regulators", "Regulators have their input and output capacitors", "Power", needs=("netlist",))
def power_regulators(ctx):
    """A regulator is stable and quiet only with the capacitors its data sheet asks for, at its pins: an
    LDO without an output capacitor oscillates, and a converter without one at its input rings its supply
    on every edge. For each regulator (a part with a power output, or VIN and VOUT pins) there must be a
    capacitor from its input and from its output to ground; with a board, the nearest one within
    checks.regulator_cap_mm (default 10 mm, pad edge to pad edge). The output rail's total capacitance is
    compared with checks.min_output_uF (default 1 uF)."""
    nl = ctx.netlist
    grounds = ctx.ground_nets()
    regs = regulators(nl, grounds)
    if not regs:
        raise NotApplicable("no regulators")
    board = ctx.board if ctx.available("pcb") else None
    reach = float(ctx.setting("checks.regulator_cap_mm", 10.0))
    min_uf = float(ctx.setting("checks.min_output_uF", 1.0))
    out = []
    for r in regs:
        ref = r["ref"]
        sides = [("input", p, n) for p, n in r["ins"]] + ([("output", p, n) for p, n in r["outs"]] if r["kind"] == "linear" else [])
        for side, pin, net in sides:
            caps = _cap_between(nl, net, grounds)
            if not caps:
                out.append(Finding("power.regulators", "error", f"{ref} has no capacitor on its {side} ({net})",
                                   {"ref": ref, "net": net}, hint=f"Add the {side} capacitor its data sheet specifies, "
                                   "right at the pin.", key=f"regulators:none:{ref}:{side}:{net}"))
                continue
            if board is not None:
                best, _ = _nearest_cap(board, nl, grounds, ref, pin, net)
                if best and best[0] > reach:
                    out.append(Finding("power.regulators", "warning",
                                       f"{ref}'s nearest {side} capacitor ({best[1]}) is {best[0]:.1f} mm from its pin "
                                       f"{pin} ({net})", {"ref": ref, "net": net},
                                       hint=f"Place it within {reach:g} mm of the pin, with a short return to the "
                                            "regulator's ground.", key=f"regulators:far:{ref}:{side}:{net}"))
        if r["kind"] == "linear":
            for pin, net in r["outs"]:
                total = sum(_farads(nl.parts[c].get("value", "")) or 0.0 for c in _cap_between(nl, net, grounds))
                if 0 < total < min_uf * 1e-6:
                    out.append(Finding("power.regulators", "warning",
                                       f"{ref}'s output ({net}) has {total * 1e6:.2g} uF in total, below {min_uf:g} uF",
                                       {"ref": ref, "net": net}, hint="Check the data sheet's minimum output capacitance "
                                       "(and its ESR range): too little makes an LDO oscillate.",
                                       key=f"regulators:bulk:{ref}:{net}"))
    return out


# ----------------------------------------------------------------------------- power.thermal
def theta_ja(ctx, ref, fp_lib):
    over = (ctx.setting("checks.theta_ja", {}) or {}).get(ref)
    if over:
        return float(over), "declared"
    from .integrity import package_of_footprint
    fam = package_of_footprint(fp_lib)[0] or ""
    name = (fp_lib or "").upper()
    for key, t in THETA_JA:
        if fam.startswith(key) or key in name:
            return t, key
    return None, None


def _rail_current(ctx, net):
    amps = _current_for(net, _currents(ctx))
    if amps is not None:
        return amps
    paths = [p for p in (ctx.setting("checks.power_paths", []) or []) if str(p.get("net", "")).rsplit("/", 1)[-1] == net]
    return sum(float(p.get("amps", 0)) for p in paths) if paths else None


@check("power.thermal", "Linear regulators stay below their maximum temperature", "Power", needs=("netlist",))
def power_thermal(ctx):
    """A linear regulator burns the whole voltage it drops: P = (Vin - Vout) x I. Its junction sits at
    ambient + P x theta-JA. A SOT-23 LDO dropping 12 V to 3.3 V at 100 mA runs at about 215 C: it shuts
    down or dies. The current is the output rail's (tracewright.json checks.currents or
    checks.power_paths); theta-JA comes from the package on a modest copper area (checks.theta_ja
    overrides it per part), ambient from checks.ambient_c (25 C) and the limit from checks.tj_max_c
    (125 C). A regulator whose load current is not declared is reported as not verified."""
    nl = ctx.netlist
    grounds = ctx.ground_nets()
    lin = [r for r in regulators(nl, grounds) if r["kind"] == "linear"]
    if not lin:
        raise NotApplicable("no linear regulators")
    volts = rail_voltages(ctx)
    ta = float(ctx.setting("checks.ambient_c", 25.0))
    tj_max = float(ctx.setting("checks.tj_max_c", 125.0))
    board = ctx.board if ctx.available("pcb") else None
    out, unverified = [], []
    for r in lin:
        ref = r["ref"]
        vin = max((volts.get(n) for _, n in r["ins"] if volts.get(n) is not None), default=None)
        outs = [(n, volts.get(n)) for _, n in r["outs"] if volts.get(n) is not None]
        if vin is None or not outs:
            unverified.append(f"{ref} (rail voltages unknown)")
            continue
        net, vout = max(outs, key=lambda x: x[1])
        amps = _rail_current(ctx, net)
        if amps is None:
            unverified.append(f"{ref} (no current declared for {net})")
            continue
        lib = board.footprints[ref].lib_id if board is not None and ref in board.footprints else nl.parts[ref].get("footprint", "")
        th, pkg = theta_ja(ctx, ref, lib)
        if th is None:
            unverified.append(f"{ref} (unknown package thermal resistance: set checks.theta_ja)")
            continue
        p = max(0.0, vin - vout) * amps
        tj = ta + p * th
        if tj > tj_max:
            sev = "error"
        elif tj > tj_max - 25:
            sev = "warning"
        else:
            continue
        out.append(Finding("power.thermal", sev,
                           f"{ref} drops {vin:g} V to {vout:g} V at {amps:g} A: {p:.2f} W, about {tj:.0f} C at the junction "
                           f"({pkg}, {th:g} C/W, {ta:g} C ambient)", {"ref": ref, "net": net},
                           hint="Use a switching regulator, a bigger package on more copper (thermal vias under the tab), "
                                "or drop part of the voltage elsewhere.", key=f"thermal:{ref}"))
    if unverified:
        out.append(Finding("power.thermal", "info", f"temperature not verified for {', '.join(unverified[:6])}"
                           f"{' ...' if len(unverified) > 6 else ''}", key="thermal:unverified",
                           hint='Declare the load, e.g. "checks": {"currents": {"+3V3": 0.3}} in tracewright.json.'))
    return out


# ----------------------------------------------------------------------------- power.drop
def _via_amps(drill, rise=10.0):
    """IPC-2221 current for a via barrel (drill mm) at a temperature rise."""
    area_mil2 = math.pi * (drill / 0.0254) * (PLATING_MM / 0.0254)
    return 0.048 * rise ** 0.44 * area_mil2 ** 0.725


def _path_drop(ctx, board, net, path, amps):
    """(ohms, [(x, y, drill) of each via on the path]) along a widest path."""
    ohms, vias = 0.0, []
    for info in path:
        if info[0] == "track":
            _, layer, a, b, w = info
            ohms += RHO_CU * geom.dist(a, b) / (w * copper_thickness(ctx, layer))
        elif info[0] == "via":
            _, x, y = info
            v = min(board.vias, key=lambda v: (v.x - x) ** 2 + (v.y - y) ** 2) if board.vias else None
            if v is not None:
                h = sum(float(l.get("thickness") or 0) for l in board.stackup if l.get("type") in ("core", "prepreg")) or 1.6
                ohms += RHO_CU * h / (math.pi * v.drill * PLATING_MM)
                vias.append((v.x, v.y, v.drill))
    return ohms, vias


@check("power.drop", "Supply paths keep their voltage drop small", "Power", needs=("pcb", "netlist"))
def power_drop(ctx):
    """Along every declared current path (checks.power_paths, checks.currents) the copper's resistance
    (tracks by length, width and copper weight; vias by their barrel) times the current is the voltage
    the load loses. More than checks.max_drop_pct of the rail (default 3 %) is a warning, more than 10 %
    an error. Where a path changes layer, the vias beside each other must carry the current together at
    a 10 C rise (IPC-2221 on the plated barrel). Pours count as lossless, so a path through a pour is
    judged by its tracks and vias."""
    from ..copper import NetGraph
    b, nl = ctx.board, ctx.netlist
    volts = rail_voltages(ctx)
    table = _currents(ctx)
    paths = list(ctx.setting("checks.power_paths", []) or [])
    full = {n.rsplit("/", 1)[-1]: n for n in b.nets}
    jobs = []
    for pth in paths:
        net = full.get(pth.get("net", ""), pth.get("net", ""))
        srcs, dsts = _pads_for(b, pth.get("from", ""), net), _pads_for(b, pth.get("to", ""), net)
        if srcs and dsts:
            jobs.append((net, srcs[0], dsts, float(pth.get("amps", 0))))
    for net in sorted(b.nets):
        amps = _current_for(net, table)
        if amps is None:
            continue
        terms = _terminals(b, nl, net)
        src = _source(nl, terms)
        if src:
            jobs.append((net, src, [(r, n) for r, n, _ in terms if (r, n) != tuple(src)], amps))
    if not jobs:
        raise NotApplicable("no currents declared (checks.currents / checks.power_paths)")
    limit = float(ctx.setting("checks.max_drop_pct", 3.0))
    out, graphs = [], {}
    for net, src, dsts, amps in jobs:
        g = graphs.get(net) or graphs.setdefault(net, NetGraph(b, net))
        short = net.rsplit("/", 1)[-1]
        v = volts.get(short)
        worst = None
        for dst in dsts:
            _, path = g.widest_path(tuple(src), tuple(dst))
            if not path:
                continue
            ohms, vias = _path_drop(ctx, b, net, path, amps)
            if worst is None or ohms > worst[0]:
                worst = (ohms, vias, dst)
        if worst is None:
            continue
        ohms, vias, dst = worst
        drop = ohms * amps
        if v:
            pct = 100 * drop / v
            if pct > limit:
                out.append(Finding("power.drop", "error" if pct > 10 else "warning",
                                   f"{short}: {amps:g} A from {src[0]}.{src[1]} to {dst[0]}.{dst[1]} loses {drop * 1000:.0f} mV "
                                   f"({pct:.1f} % of {v:g} V) in {ohms * 1000:.0f} mOhm of copper", {"net": net},
                                   hint="Widen the tracks on this path, pour the rail, or add parallel vias.",
                                   key=f"drop:{short}:{src[0]}.{src[1]}:{dst[0]}.{dst[1]}"))
        for x, y, drill in vias:
            near = [w for w in b.vias if w.net == net and math.hypot(w.x - x, w.y - y) <= 3.0]
            cap = sum(_via_amps(w.drill) for w in near)
            if amps > cap:
                out.append(Finding("power.drop", "warning",
                                   f"{short}: {amps:g} A changes layer at ({x:.1f}, {y:.1f}) through {len(near)} via"
                                   f"{'s' if len(near) != 1 else ''} (about {cap:.1f} A at a 10 C rise)",
                                   {"net": net, "x": x, "y": y},
                                   hint=f"Put {math.ceil(amps / max(_via_amps(drill), 1e-6))} or more vias side by side "
                                        "where the current changes layer.", key=f"drop:vias:{short}:{x:.1f}:{y:.1f}"))
                break
    return out


# ----------------------------------------------------------------------------- power.switcher
@check("power.switcher", "Switching converters are laid out around their hot loop", "Power", needs=("pcb", "netlist"))
def power_switcher(ctx):
    """A converter switches amps in nanoseconds; the loop from its input capacitor through the switch
    and back is what radiates and rings. Its input capacitor must sit at the VIN pin (checks.
    switcher_cap_mm, default 3 mm, pad edge to pad edge), the switch node's copper must stay small
    (checks.sw_area_mm2, default 30 mm2 of track and pour), and the feedback line, the most sensitive
    net on the board, must keep checks.fb_clearance_mm (default 1 mm) from the switch node's copper."""
    nl, b = ctx.netlist, ctx.board
    grounds = ctx.ground_nets()
    sws = [r for r in regulators(nl, grounds) if r["kind"] == "switching"]
    if not sws:
        raise NotApplicable("no switching converters")
    reach = float(ctx.setting("checks.switcher_cap_mm", 3.0))
    max_area = float(ctx.setting("checks.sw_area_mm2", 30.0))
    fb_cl = float(ctx.setting("checks.fb_clearance_mm", 1.0))
    full = {n.rsplit("/", 1)[-1]: n for n in b.nets}
    rails = set(ctx.power_nets())
    out = []
    for r in sws:
        ref = r["ref"]
        for pin, net in r["ins"]:
            best, caps = _nearest_cap(b, nl, grounds, ref, pin, net)
            if not caps:
                out.append(Finding("power.switcher", "error", f"{ref} has no input capacitor on {net}", {"ref": ref},
                                   key=f"switcher:nocap:{ref}:{net}"))
            elif best and best[0] > reach:
                out.append(Finding("power.switcher", "error" if best[0] > 3 * reach else "warning",
                                   f"{ref}'s input capacitor ({best[1]}) is {best[0]:.1f} mm from its VIN pin {pin}: the hot "
                                   f"loop is that long", {"ref": ref, "net": net},
                                   hint=f"Put the input capacitor within {reach:g} mm of VIN, its ground pad right by the "
                                        "converter's ground, on the same layer.", key=f"switcher:far:{ref}:{pin}"))
        for pin, net in r["sw"]:
            fn = full.get(net, net)
            area = sum(t.length() * t.w for t in b.tracks if t.net == fn)
            for z in b.zones:
                if z.net == fn and not z.is_rule_area:
                    area += sum(abs(geom.area(pl)) for pls in z.fills.values() for pl in pls)
            if area > max_area:
                out.append(Finding("power.switcher", "warning", f"{ref}'s switch node {net} has {area:.0f} mm2 of copper",
                                   {"ref": ref, "net": net}, hint="Keep the switch node short and small: the inductor right "
                                   "at the SW pin, no pour on it.", key=f"switcher:area:{ref}:{net}"))
            sw_segs = [(t.layer, t.a, t.b, t.w) for t in b.tracks if t.net == fn]
            sw_pads = [p for p in b.pads() if p.net == fn]
            for fpin, fnet in r["fb"]:
                if fnet in rails:                  # feedback taken straight from the output rail: nothing to keep apart
                    continue
                ff = full.get(fnet, fnet)
                close = None
                for t in b.tracks:
                    if t.net != ff:
                        continue
                    for layer, a, c, w in sw_segs:
                        if layer == t.layer:
                            d = geom.seg_seg_dist(t.a, t.b, a, c) - (t.w + w) / 2
                            if close is None or d < close[0]:
                                close = (d, (t.a[0] + t.b[0]) / 2, (t.a[1] + t.b[1]) / 2)
                    for p in sw_pads:
                        if t.layer in p.layers:
                            d = min(geom.seg_point_dist(v, t.a, t.b) for v in p.poly) - t.w / 2
                            if close is None or d < close[0]:
                                close = (d, p.x, p.y)
                if close and close[0] < fb_cl:
                    out.append(Finding("power.switcher", "warning",
                                       f"{ref}'s feedback line {fnet} passes {max(close[0], 0):.2f} mm from the switch node {net}",
                                       {"ref": ref, "net": fnet, "x": close[1], "y": close[2]},
                                       hint="Route the feedback away from the SW node and the inductor, from the output "
                                            "capacitor, on the quiet side.", key=f"switcher:fb:{ref}:{fnet}"))
    return out


# ----------------------------------------------------------------------------- power.pours
def _touches(poly, bx, pts):
    return any(bx[0] - 0.1 <= x <= bx[2] + 0.1 and bx[1] - 0.1 <= y <= bx[3] + 0.1 and
               (geom.inside((x, y), poly) or geom.poly_dist((x, y), poly) < 0.1) for x, y in pts)


def _pad_points(p):
    """A pad's center, corners and edge midpoints (thermal spokes land on the edge midpoints)."""
    pl = list(p.poly)
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(pl, pl[1:] + pl[:1])]
    return [(p.x, p.y)] + pl + mids


@check("power.pours", "Pours are connected and stitched", "Power", needs=("pcb",))
def power_pours(ctx):
    """A filled island that touches nothing of its own net is dead copper: it floats, couples noise and
    fails some fab reviews (KiCad removes islands only when the zone is set to). Where one ground is poured
    on both outer layers, stitching vias tie the two together: at any point of the overlap the nearest
    stitch should be within checks.stitch_mm (default 15 mm); a ground whose overlap is mostly farther is
    a poor return path. Pads carrying a declared current of checks.spoke_current_a (default 3 A) or more
    into a pour through thermal spokes alone get too little copper."""
    b = ctx.board
    zones = [z for z in b.zones if not z.is_rule_area and z.net and z.fills]
    if not zones:
        raise NotApplicable("no filled pours")
    grounds = set(ctx.ground_nets()) if ctx.available("netlist") else \
        {n for n in b.nets if n.rsplit("/", 1)[-1].upper().startswith("GND")}
    out = []
    things = collections.defaultdict(lambda: collections.defaultdict(list))      # net -> layer -> points
    for p in b.pads():
        for l in p.layers:
            if l.endswith(".Cu") or l == "*.Cu":
                for layer in (b.copper if l == "*.Cu" else [l]):
                    things[p.net][layer] += _pad_points(p)
    for v in b.vias:
        for layer in b.copper:
            things[v.net][layer].append((v.x, v.y))
    for t in b.tracks:
        things[t.net][t.layer] += [t.a, t.b]
    islands = collections.defaultdict(list)
    for z in zones:
        for layer, polys in z.fills.items():
            for pl in polys:
                if not _touches(pl, geom.bbox(pl), things[z.net][layer]):
                    islands[(z.net, layer)].append(pl)
    for (net, layer), pls in sorted(islands.items()):
        big = max(pls, key=lambda pl: abs(geom.area(pl)))
        x0, y0, x1, y1 = geom.bbox(big)
        out.append(Finding("power.pours", "warning",
                           f"{len(pls)} island{'s' if len(pls) != 1 else ''} of {net.rsplit('/', 1)[-1]} on {layer} "
                           f"{'touch' if len(pls) != 1 else 'touches'} nothing of {'their' if len(pls) != 1 else 'its'} net "
                           f"({'largest ' if len(pls) != 1 else ''}{abs(geom.area(big)):.1f} mm2)", {"net": net, "layer": layer,
                                                                                    "x": (x0 + x1) / 2, "y": (y0 + y1) / 2},
                           hint="Set the zone to remove islands, or stitch them with a via.", key=f"pours:island:{net}:{layer}"))
    # stitching between two outer ground pours
    step = 2.0
    reach = float(ctx.setting("checks.stitch_mm", 15.0))
    for gnd in sorted({z.net for z in zones if z.net.rsplit("/", 1)[-1] in grounds}):
        polys = collections.defaultdict(list)
        for z in zones:
            if z.net == gnd:
                for layer, pls in z.fills.items():
                    polys[layer] += pls
        outer = [l for l in ("F.Cu", "B.Cu") if polys.get(l)]
        if len(outer) < 2:
            continue
        stitches = [(v.x, v.y) for v in b.vias if v.net == gnd] + \
                   [(p.x, p.y) for p in b.pads() if p.net == gnd and p.drill]
        if not stitches:
            out.append(Finding("power.pours", "warning", f"{gnd.rsplit('/', 1)[-1]} is poured on both sides with no via tying "
                               "them together", {"net": gnd}, key=f"pours:nostitch:{gnd}"))
            continue
        bx = geom.bbox_union([geom.bbox(pl) for pl in polys["F.Cu"]])
        boxed = {l: [(geom.bbox(pl), pl) for pl in polys[l]] for l in outer}

        def poured(layer, x, y):
            return any(q[0] <= x <= q[2] and q[1] <= y <= q[3] and geom.inside((x, y), pl) for q, pl in boxed[layer])
        n_all = far = 0
        worst = None
        x = bx[0] + step / 2
        while x < bx[2]:
            y = bx[1] + step / 2
            while y < bx[3]:
                if poured("F.Cu", x, y) and poured("B.Cu", x, y):
                    n_all += 1
                    d = min(math.hypot(x - sx, y - sy) for sx, sy in stitches)
                    if d > reach:
                        far += 1
                        if worst is None or d > worst[0]:
                            worst = (d, x, y)
                y += step
            x += step
        if n_all and far / n_all > 0.10:
            out.append(Finding("power.pours", "warning",
                               f"{100 * far / n_all:.0f} % of the {gnd.rsplit('/', 1)[-1]} overlap is more than {reach:g} mm from a "
                               f"stitching via (up to {worst[0]:.0f} mm)", {"net": gnd, "x": worst[1], "y": worst[2]},
                               hint=f"Add ground vias so no point of the pour is farther than {reach:g} mm from one "
                                    "(closer along fast signals and board edges).", key=f"pours:stitch:{gnd}"))
    # high currents through thermal spokes
    amps_at = collections.defaultdict(float)
    for pth in ctx.setting("checks.power_paths", []) or []:
        net = str(pth.get("net", ""))
        for spec in (pth.get("from", ""), pth.get("to", "")):
            for r, n in _pads_for(b, spec, next((x for x in b.nets if x.rsplit("/", 1)[-1] == net), net)):
                amps_at[(r, n)] = max(amps_at[(r, n)], float(pth.get("amps", 0)))
    lim = float(ctx.setting("checks.spoke_current_a", 3.0))
    for (r, n), amps in sorted(amps_at.items()):
        if amps < lim or r not in b.footprints:
            continue
        pad = b.footprints[r].pad(n)
        if pad is None:
            continue
        tracks = [t for t in b.tracks if t.net == pad.net and (geom.inside(t.a, pad.poly) or geom.inside(t.b, pad.poly))]
        spokes = [z for z in zones if z.net == pad.net and z.connect in ("", "thru_hole_only")
                  and any(geom.inside((pad.x, pad.y), pl) or geom.poly_dist((pad.x, pad.y), pl) < max(pad.w, pad.h)
                          for layer, pls in z.fills.items() if layer in pad.layers for pl in pls)]
        if spokes and not tracks:
            out.append(Finding("power.pours", "warning", f"{r}.{n} carries {amps:g} A into the {pad.net.rsplit('/', 1)[-1]} pour "
                               "through thermal-relief spokes only", {"ref": r, "net": pad.net},
                               hint="Connect this pad to the pour solid (zone or pad setting), or add a wide track.",
                               key=f"pours:spokes:{r}.{n}"))
    return out
