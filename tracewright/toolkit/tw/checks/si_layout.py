"""Signal integrity, the layout side: return vias where fast signals change layer, stubs and
series terminations, length-matched groups, and noisy nets kept away from analog lines and crystals."""
import math, re, fnmatch, collections
from . import check, Finding, NotApplicable, examined, plural
from .. import geom
from .signal import fast_nets, plane_nets, usb_speed, coupling, tokens, ctx_grounds

NOISY = {"SW", "LX", "PH", "PWM", "SWITCH"}
ANALOG = {"ADC", "AIN", "ANALOG", "SENSE", "SNS", "VREF", "AREF", "REF", "THERM", "NTC", "MIC", "AUDIO", "AUX", "PT100",
          "STRAIN", "BRIDGE", "LDR", "POT", "VSENSE", "ISENSE", "CT"}


def _short(net):
    return net.rsplit("/", 1)[-1] if net else ""


# ----------------------------------------------------------------------------- si.layer_change
@check("si.layer_change", "Fast signals that change layer have a return via beside them", "High-speed", needs=("pcb",))
def si_layer_change(ctx):
    """When a fast signal changes layer, its return current must change reference plane with it. With no
    ground (or plane) via next to the signal via, the return current detours to the nearest one it can
    find, the loop opens up, and the via becomes an antenna. Each via on a fast net needs a via of a
    plane net within checks.return_via_mm (default 2 mm). Low- and full-speed USB edges are slow enough
    that a missing return via is only noted."""
    b = ctx.board
    fast = fast_nets(ctx, b)
    if not fast:
        raise NotApplicable("no fast nets")
    planes = plane_nets(ctx, b)
    ref_vias = [(v.x, v.y) for v in b.vias if v.net in planes] + [(p.x, p.y) for p in b.pads() if p.net in planes and p.drill]
    reach = float(ctx.setting("checks.return_via_mm", 2.0))
    slow_usb = usb_speed(ctx, [n for n, why in fast.items() if "usb" in why]) in ("fs", None)
    lonely = collections.defaultdict(list)
    for v in b.vias:
        if v.net not in fast:
            continue
        d = min((math.hypot(v.x - x, v.y - y) for x, y in ref_vias), default=None)
        if d is None or d > reach:
            lonely[v.net].append((v.x, v.y, d))
    out = []
    for net, vs in sorted(lonely.items()):
        why = fast[net]
        sev = "info" if ("usb" in why and slow_usb) else "warning"
        x, y, d = vs[0]
        near = f"nearest {d:.1f} mm" if d is not None else "no plane via on the board"
        out.append(Finding("si.layer_change", sev,
                           f"{_short(net)} ({why}) changes layer {len(vs)} time{'s' if len(vs) != 1 else ''} with no ground via "
                           f"within {reach:g} mm ({near})", {"net": net, "x": x, "y": y},
                           hint="Place a ground via right beside each signal via (for a pair, one beside each, "
                                "symmetrically), or keep the net on one layer.", key=f"layer_change:{net}"))
    examined(ctx, plural(len(fast), "fast net"))
    return out


# ----------------------------------------------------------------------------- si.stubs
def _branches(tracks, stops):
    """[(branch point, [branch lengths])] for the T-junctions of one net's straight tracks. `stops`: points
    (pads, vias) a branch ends at."""
    ends = collections.defaultdict(list)                    # rounded point -> [(track index, which end)]
    segs = []
    for t in tracks:
        if t.mid:
            continue
        segs.append((t.layer, t.a, t.b))
    # split segments at T points (an end lying on another segment)
    pts = [(l, a) for l, a, _ in segs] + [(l, c) for l, _, c in segs]
    split = []
    for l, a, c in segs:
        cuts = [a, c]
        for pl, p in pts:
            if pl == l and geom.seg_point_dist(p, a, c) < 0.01 and geom.dist(p, a) > 0.01 and geom.dist(p, c) > 0.01:
                cuts.append(p)
        cuts.sort(key=lambda p: geom.dist(p, a))
        split += [(l, p, q) for p, q in zip(cuts, cuts[1:]) if geom.dist(p, q) > 1e-4]
    key = lambda l, p: (l, round(p[0], 2), round(p[1], 2))
    adj = collections.defaultdict(list)
    for l, p, q in split:
        adj[key(l, p)].append((key(l, q), geom.dist(p, q)))
        adj[key(l, q)].append((key(l, p), geom.dist(p, q)))

    def stopped(k):
        return any(math.hypot(k[1] - x, k[2] - y) <= r for x, y, r in stops)
    out = []
    for k, nb in adj.items():
        if len(nb) < 3 or stopped(k):
            continue
        lens = []
        for first, L0 in nb:
            prev, cur, total = k, first, L0
            for _ in range(500):
                nxt = [(m, d) for m, d in adj[cur] if m != prev]
                if len(nxt) != 1 or stopped(cur):
                    break
                prev, (cur, d) = cur, nxt[0]
                total += d
            lens.append(total)
        out.append(((k[1], k[2]), sorted(lens)))
    return out


@check("si.stubs", "Fast signals have no stubs, and series terminations sit at the driver", "High-speed", needs=("pcb",))
def si_stubs(ctx):
    """A fast signal should run from its driver to its load as one line. A T-branch leaves a stub whose
    reflection rings on every edge; past checks.max_stub_mm (default 2 mm) it is reported. A series
    termination resistor on a clock works only right at the driver: within checks.series_r_mm (default
    5 mm) of the output pin that drives it."""
    b = ctx.board
    fast = fast_nets(ctx, b)
    if not fast:
        raise NotApplicable("no fast nets")
    max_stub = float(ctx.setting("checks.max_stub_mm", 2.0))
    slow_usb = usb_speed(ctx, [n for n, why in fast.items() if "usb" in why]) in ("fs", None)
    out = []
    by_net = collections.defaultdict(list)
    for t in b.tracks:
        if t.net in fast:
            by_net[t.net].append(t)
    for net, ts in sorted(by_net.items()):
        stops = [(p.x, p.y, max(p.w, p.h) / 2) for p in b.pads() if p.net == net] + \
                [(v.x, v.y, v.d / 2) for v in b.vias if v.net == net]
        worst = None
        for (x, y), lens in _branches(ts, stops):
            stub = lens[0]
            if stub > max_stub and (worst is None or stub > worst[0]):
                worst = (stub, x, y)
        if worst:
            sev = "info" if ("usb" in fast[net] and slow_usb) else "warning"
            out.append(Finding("si.stubs", sev, f"{_short(net)} ({fast[net]}) has a {worst[0]:.1f} mm stub at "
                               f"({worst[1]:.1f}, {worst[2]:.1f})", {"net": net, "x": worst[1], "y": worst[2]},
                               hint="Route the net as a daisy chain through its loads, or shorten the branch below "
                                    f"{max_stub:g} mm.", key=f"stubs:{net}"))
    # series terminations on clocks
    if ctx.available("netlist"):
        nl = ctx.netlist
        reach = float(ctx.setting("checks.series_r_mm", 5.0))
        clocks = {_short(n) for n, why in fast.items() if why == "clock"}
        for ref, part in sorted(nl.parts.items()):
            pins = nl.pins_of(ref)
            if not ref.startswith("R") or len(pins) != 2 or ref not in b.footprints:
                continue
            nets = [nl.net_of(ref, p) for p in pins]
            for i in (0, 1):
                if nets[i] not in clocks:
                    continue
                src_net = nets[1 - i]
                drivers = [(r, p) for r, p in nl.nets_short().get(src_net, [])
                           if r != ref and nl.pin_type(r, p) in ("output", "tri_state", "bidirectional") and r[:1] in ("U", "I", "M", "X", "Y")]
                if len(drivers) != 1:
                    continue
                dr, dp = drivers[0]
                if dr not in b.footprints:
                    continue
                a, c = b.footprints[ref].pad(pins[1 - i]), b.footprints[dr].pad(dp)
                if a is None or c is None:
                    continue
                d = geom.dist((a.x, a.y), (c.x, c.y))
                if d > reach:
                    out.append(Finding("si.stubs", "warning", f"{ref}, the series termination of {nets[i]}, is {d:.1f} mm from "
                                       f"its driver {dr} pin {dp}", {"ref": ref, "net": nets[i]},
                                       hint=f"Move {ref} within {reach:g} mm of the driver's pin: a series termination "
                                            "works only at the source.", key=f"stubs:series:{ref}"))
    examined(ctx, plural(len(fast), "fast net"))
    return out


# ----------------------------------------------------------------------------- si.length_groups
AUTO_GROUPS = [
    ("SDIO", r"(^|_)(SD|SDIO|SDMMC|EMMC)\d?_(D[0-7]|DAT[0-7]|CMD|CLK)$", 2.5),
    ("camera (DVP)", r"(^|_)(CAM|DVP|CSI)\d?_(D[0-9]|D1[0-5]|PCLK)$", 5.0),
    ("RGMII TX", r"(^|_)RGMII_(TXD[0-3]|TX_?CTL|TX_?CLK|TX_?EN)$", 2.5),
    ("RGMII RX", r"(^|_)RGMII_(RXD[0-3]|RX_?CTL|RX_?CLK|RX_?DV)$", 2.5),
]


def _length(b, net):
    return sum(t.length() for t in b.tracks if t.net == net)


@check("si.length_groups", "Length-matched groups are matched", "High-speed", needs=("pcb",))
def si_length_groups(ctx):
    """Parallel buses clocked together (SDIO, a parallel camera port, RGMII) need their lines matched in
    length, or the data arrives outside the clock's window. Groups come from tracewright.json
    checks.length_groups: [{"name": "SDIO", "nets": ["SD_D*", "SD_CMD", "SD_CLK"], "tolerance_mm": 2.5}]
    and from bus names found on the board (reported as notes until declared). Each routed group's
    longest and shortest line must differ by no more than its tolerance."""
    b = ctx.board
    groups = []
    for g in ctx.setting("checks.length_groups", []) or []:
        pats = g.get("nets", [])
        nets = sorted(n for n in b.nets if any(fnmatch.fnmatchcase(_short(n), p) or fnmatch.fnmatchcase(n, p) for p in pats))
        groups.append((g.get("name", "group"), nets, float(g.get("tolerance_mm", 2.5)), "warning"))
    declared = {n for _, nets, _, _ in groups for n in nets}
    for name, pat, tol in AUTO_GROUPS:
        rx = re.compile(pat, re.I)
        nets = sorted(n for n in b.nets if rx.search(_short(n)) and n not in declared)
        if len(nets) >= 3:
            groups.append((name, nets, tol, "info"))
    if not groups:
        raise NotApplicable("no length-matched groups")
    out = []
    for name, nets, tol, sev in groups:
        lens = {n: _length(b, n) for n in nets}
        routed = {n: L for n, L in lens.items() if L > 0}
        if len(routed) < 2:
            continue
        hi = max(routed, key=routed.get)
        lo = min(routed, key=routed.get)
        skew = routed[hi] - routed[lo]
        if skew > tol:
            out.append(Finding("si.length_groups", sev,
                               f"{name}: {_short(hi)} is {routed[hi]:.1f} mm and {_short(lo)} {routed[lo]:.1f} mm, a skew of "
                               f"{skew:.1f} mm (tolerance {tol:g} mm)", {"net": hi},
                               hint="Lengthen the short lines with serpentines (or reroute the long one); declare the "
                                    "group in checks.length_groups to make this exact.", key=f"length_groups:{name}"))
    examined(ctx, plural(len(groups), "length-matched group"))
    return out


# ----------------------------------------------------------------------------- si.noise
def _is_noisy(net, fast):
    toks = set(tokens(net))
    return bool(toks & NOISY) or fast.get(net) == "clock"


def _is_analog(net):
    return bool(set(tokens(net)) & ANALOG)


@check("si.noise", "Noisy nets keep away from analog lines and crystals", "High-speed", needs=("pcb",))
def si_noise(ctx):
    """A switch node, a PWM line or a clock run beside an analog input puts its edges straight into the
    measurement; across a crystal's pads it pulls the oscillator. Noisy tracks (switch nodes, PWM, clocks)
    must keep checks.noise_clearance_mm (default 1 mm, edge to edge) from analog tracks (ADC, sense,
    reference, thermistor, microphone lines) where they run side by side for more than 3 mm, and from
    the pads of every crystal they do not belong to."""
    b = ctx.board
    fast = fast_nets(ctx, b)
    noisy = [t for t in b.tracks if t.net and _is_noisy(t.net, fast)]
    analog = [t for t in b.tracks if t.net and _is_analog(t.net) and not _is_noisy(t.net, fast)]
    crystals = [fp for fp in b.fp_list if fp.ref[:1] in ("Y", "X") and
                ("crystal" in fp.lib_id.lower() or "oscillator" in fp.lib_id.lower() or
                 re.search(r"\d\s*(k|m)hz", (fp.value or "").lower()))]
    if not noisy or not (analog or crystals):
        raise NotApplicable("no noisy nets near analog lines or crystals")
    cl = float(ctx.setting("checks.noise_clearance_mm", 1.0))
    out, seen = [], set()
    for t in noisy:
        for o in analog:
            if o.layer != t.layer or o.net == t.net or (t.net, o.net) in seen:
                continue
            c = coupling(t, o)
            if not c:
                continue
            run, dist = c
            gap = dist - (t.w + o.w) / 2
            if run > 3.0 and gap < cl:
                seen.add((t.net, o.net))
                out.append(Finding("si.noise", "warning",
                                   f"{_short(o.net)} (analog) runs {run:.0f} mm beside {_short(t.net)} with {max(gap, 0):.2f} mm "
                                   f"between them", {"net": o.net, "x": (o.a[0] + o.b[0]) / 2, "y": (o.a[1] + o.b[1]) / 2},
                                   hint="Move the analog line away (or put ground between them); route noisy nets on the "
                                        "far side of the board from analog inputs.", key=f"noise:{t.net}:{o.net}"))
        for fp in crystals:
            own = {p.net for p in fp.pads}
            if t.net in own:
                continue
            for p in fp.pads:
                if t.layer not in p.layers:
                    continue
                d = min(geom.seg_point_dist(v, t.a, t.b) for v in p.poly) - t.w / 2
                if d < cl and (t.net, fp.ref) not in seen:
                    seen.add((t.net, fp.ref))
                    out.append(Finding("si.noise", "warning", f"{_short(t.net)} passes {max(d, 0):.2f} mm from crystal "
                                       f"{fp.ref}'s pad {p.num}", {"ref": fp.ref, "net": t.net},
                                       hint="Keep noisy and fast tracks away from the crystal (and off the layer under it).",
                                       key=f"noise:xtal:{t.net}:{fp.ref}"))
    examined(ctx, plural(len({t.net for t in noisy}), "noisy net") + " against " + ", ".join(x for x in (plural(len({t.net for t in analog}), "analog line") if analog else "", plural(len(crystals), "crystal") if crystals else "") if x))
    return out
