"""Layout quality: protection parts where they protect, and the routing scored the way a reviewer reads
it -- detours, needless vias, dangling copper, tracks squeezed between fine-pitch pads. The score is
also what the router is benchmarked on."""
import math, re, collections, statistics
from . import check, Finding, NotApplicable, examined, plural
from .. import geom

ESD = re.compile(r"tvs|esd|usblc|pesd|tpd\d|smaj|smbj|smcj|sm712|prtr|srv05|rclamp|sp05\d|nup\d|lesd|cdsot|sesd|"
                 r"ip4220|sp3012|tpd4e|sm6t|p6ke", re.I)
CONNECTOR = ("J", "P", "CN", "CON", "USB", "X")


def _short(net):
    return net.rsplit("/", 1)[-1] if net else ""


def _is_connector(fp):
    pre = fp.ref.rstrip("0123456789").upper()
    if pre in ("X",) and ("crystal" in fp.lib_id.lower() or re.search(r"\d\s*(k|m)hz", (fp.value or "").lower())):
        return False
    return pre in CONNECTOR or "connector" in fp.lib_id.lower()


# ----------------------------------------------------------------------------- pcb.esd
@check("pcb.esd", "Protection parts sit at the connectors they protect", "Placement", needs=("pcb",))
def pcb_esd(ctx):
    """An ESD or TVS diode clamps a strike only if it meets it first: the path from the connector pin to
    the diode must be short (every millimetre is inductance the strike sees before the clamp), and no
    other part should hang on the line between them. Each protection part's pad must sit within
    checks.esd_mm (default 6 mm, pad center to pad center) of the connector pad on its net."""
    b = ctx.board
    prot = [fp for fp in b.fp_list if ESD.search(" ".join((fp.value or "", fp.lib_id or "", fp.fields.get("MPN", "") if fp.fields else "")))]
    if not prot:
        raise NotApplicable("no ESD or TVS parts")
    grounds = set(ctx.ground_nets()) if ctx.available("netlist") else set()
    # off-board connectors only: a module's board-to-board socket (a CM5's 100-pin header) is not where a
    # strike comes in
    conns = [fp for fp in b.fp_list if _is_connector(fp) and len({p.num for p in fp.pads}) <= 40]
    reach = float(ctx.setting("checks.esd_mm", 8.0))
    reach_power = float(ctx.setting("checks.tvs_power_mm", 15.0))
    rails = set(ctx.power_nets()) if ctx.available("netlist") else set()
    out, seen = [], set()
    for fp in prot:
        for p in fp.pads:
            if not p.net or _short(p.net) in grounds or (fp.ref, p.net) in seen:
                continue
            targets = [(q, c) for c in conns for q in c.pads if q.net == p.net]
            if not targets:
                continue
            seen.add((fp.ref, p.net))
            q, c = min(targets, key=lambda qc: geom.dist((p.x, p.y), (qc[0].x, qc[0].y)))
            d = min(geom.dist((pp.x, pp.y), (q.x, q.y)) for pp in fp.pads if pp.net == p.net)
            lim = reach_power if _short(p.net) in rails else reach
            if d > lim:
                out.append(Finding("pcb.esd", "warning",
                                   f"{fp.ref} protects {_short(p.net)} from {d:.1f} mm away from {c.ref} pin {q.num}",
                                   {"ref": fp.ref, "net": p.net},
                                   hint=f"Place {fp.ref} within {lim:g} mm of the connector, before anything else on the "
                                        "line, with its ground straight into the ground pour.", key=f"esd:{fp.ref}:{p.net}"))
                continue
            closer = [o for o in b.fp_list if o is not fp and o is not c and not _is_connector(o)
                      for op in o.pads if op.net == p.net and geom.dist((op.x, op.y), (q.x, q.y)) + 0.5 < d]
            if closer:
                out.append(Finding("pcb.esd", "info", f"{closer[0].ref} sits on {_short(p.net)} between {c.ref} and its "
                                   f"protection {fp.ref}", {"ref": closer[0].ref, "net": p.net},
                                   key=f"esd:order:{fp.ref}:{p.net}"))
    examined(ctx, plural(len(prot), "protection part") + ", " + plural(len(conns), "connector"))
    return out


# ----------------------------------------------------------------------------- route.quality
def _mst(pts):
    if len(pts) < 2:
        return 0.0
    inside, total = {0}, 0.0
    best = {i: geom.dist(pts[0], pts[i]) for i in range(1, len(pts))}
    while best:
        j = min(best, key=best.get)
        total += best.pop(j)
        inside.add(j)
        for i in best:
            best[i] = min(best[i], geom.dist(pts[j], pts[i]))
    return total


def _connected_end(p, layer, net, t, b, idx):
    """Is a track end connected to something of its net (pad, via, another track, its own pour)?"""
    x, y = p
    for q in idx["pads"].get(net, []):
        if (layer in q.layers or any(l == "*.Cu" for l in q.layers) or q.drill) and \
                (geom.inside(p, q.poly) or geom.dist(p, (q.x, q.y)) < 0.02 or geom.poly_dist(p, q.poly) < t.w / 2 + 0.02):
            return True
    for v in idx["vias"].get(net, []):
        if math.hypot(v.x - x, v.y - y) <= v.d / 2 + 0.02:
            return True
    for o in idx["tracks"].get((net, layer), []):
        if o is t:
            continue
        if geom.dist(p, o.a) < 0.02 or geom.dist(p, o.b) < 0.02 or geom.seg_point_dist(p, o.a, o.b) < max(0.02, o.w / 2):
            return True
    for pl in idx["fills"].get((net, layer), []):
        if geom.inside(p, pl):
            return True
    return False


def score(b, planes=()):
    """{net: {length, mst, vias, pads}} for routed nets, and the board's totals: the scorecard."""
    L = collections.defaultdict(float)
    vias = collections.Counter(v.net for v in b.vias)
    for t in b.tracks:
        L[t.net] += t.length()
    pads = collections.defaultdict(list)
    for fp in b.fp_list:
        for q in fp.pads:
            if q.net:
                pads[q.net].append((q.x, q.y))
    poured = {z.net for z in b.zones if not z.is_rule_area and z.fills}
    nets = {}
    for n, length in L.items():
        if not n or n in planes or n in poured:
            continue
        nets[n] = {"length": length, "mst": _mst(pads[n]), "vias": vias.get(n, 0), "pads": len(pads[n])}
    detours = [d["length"] / d["mst"] for d in nets.values() if d["mst"] > 1.0]
    total = {"nets": len(nets), "length": sum(d["length"] for d in nets.values()), "vias": sum(vias.values()),
             "signal_vias": sum(d["vias"] for d in nets.values()),
             "detour_median": statistics.median(detours) if detours else None,
             "detour_worst": max(detours) if detours else None}
    return nets, total


@check("route.quality", "Routing quality: detours, vias, dangling copper, squeezed tracks", "Routing", needs=("pcb",))
def route_quality(ctx):
    """How a reviewer reads the copper. A net routed more than checks.max_detour (default 2x) its
    shortest possible length, and 10 mm longer, wandered. A two-pin net with three or more vias changed
    layer for no reason. A track end that touches nothing of its net (no pad, via, track or its own pour)
    is left-over copper and an antenna. A track squeezed between two neighbouring pads of a fine-pitch
    part (0.65 mm pitch or less) invites solder bridges. The board's totals (length, vias, median detour)
    are reported as a note: they are the numbers routing is benchmarked on."""
    b = ctx.board
    if not b.tracks:
        raise NotApplicable("no tracks")
    try:
        from .signal import plane_nets
        planes = plane_nets(ctx, b)
    except Exception:
        planes = set()
    nets, total = score(b, planes)
    limit = float(ctx.setting("checks.max_detour", 2.0))
    out, wander, hops = [], [], []
    for n, d in sorted(nets.items()):
        ratio = d["length"] / d["mst"] if d["mst"] > 1.0 else 0
        if ratio > limit and d["length"] - d["mst"] > 10:
            if ratio > 1.5 * limit and d["length"] - d["mst"] > 20:
                out.append(Finding("route.quality", "warning",
                                   f"{_short(n)} is routed {ratio:.1f}x its shortest path ({d['length']:.0f} mm for "
                                   f"{d['mst']:.0f} mm)", {"net": n}, hint="Reroute it directly (move parts closer if its "
                                   "path is blocked).", key=f"quality:detour:{n}"))
            else:
                wander.append((ratio, n))
        if d["pads"] == 2 and d["vias"] >= 4:
            if d["vias"] >= 6:
                out.append(Finding("route.quality", "warning", f"{_short(n)} is a two-pin net with {d['vias']} vias", {"net": n},
                                   hint="A two-pin net needs at most two layer changes.", key=f"quality:vias:{n}"))
            else:
                hops.append(n)
    if wander:
        wander.sort(reverse=True)
        out.append(Finding("route.quality", "info", f"{len(wander)} net{'s' if len(wander) != 1 else ''} routed more than "
                           f"{limit:g}x their shortest path: " + ", ".join(f"{_short(n)} {r:.1f}x" for r, n in wander[:6]) +
                           (" ..." if len(wander) > 6 else ""), {"net": wander[0][1]}, key="quality:detours"))
    if hops:
        out.append(Finding("route.quality", "info", f"{len(hops)} two-pin net{'s' if len(hops) != 1 else ''} with 4 or 5 vias: "
                           + ", ".join(_short(n) for n in hops[:8]) + (" ..." if len(hops) > 8 else ""),
                           {"net": hops[0]}, key="quality:hops"))
    # dangling track ends
    idx = {"pads": collections.defaultdict(list), "vias": collections.defaultdict(list),
           "tracks": collections.defaultdict(list), "fills": collections.defaultdict(list)}
    for q in b.pads():
        idx["pads"][q.net].append(q)
    for v in b.vias:
        idx["vias"][v.net].append(v)
    for t in b.tracks:
        idx["tracks"][(t.net, t.layer)].append(t)
    for z in b.zones:
        if not z.is_rule_area:
            for layer, pls in z.fills.items():
                idx["fills"][(z.net, layer)] += pls
    dangling = collections.defaultdict(list)
    for t in b.tracks:
        if not t.net:
            continue
        for p in (t.a, t.b):
            if not _connected_end(p, t.layer, t.net, t, b, idx):
                dangling[t.net].append(p)
    for n, pts in sorted(dangling.items()):
        x, y = pts[0]
        out.append(Finding("route.quality", "warning", f"{len(pts)} dangling track end{'s' if len(pts) != 1 else ''} on "
                           f"{_short(n)} (at {x:.1f}, {y:.1f})", {"net": n, "x": x, "y": y},
                           hint="Delete the loose piece, or finish the connection.", key=f"quality:dangling:{n}"))
    # tracks between neighbouring fine-pitch pads
    squeezed = {}
    for fp in b.fp_list:
        smd = [p for p in fp.pads if p.kind == "smd" and p.num]
        for i, p in enumerate(smd):
            for q in smd[i + 1:]:
                gap = geom.dist((p.x, p.y), (q.x, q.y))
                if gap > 0.65 or gap < 0.2 or not (set(p.layers) & set(q.layers)):
                    continue
                mx, my = (p.x + q.x) / 2, (p.y + q.y) / 2
                layer = next((l for l in p.layers if l.endswith(".Cu")), None)
                if layer is None:
                    continue
                for key_, ts in idx["tracks"].items():
                    if key_[1] != layer or key_[0] in (p.net, q.net):
                        continue
                    for t in ts:
                        if geom.segments_cross(t.a, t.b, (p.x, p.y), (q.x, q.y)):
                            squeezed.setdefault((fp.ref, t.net), (p.num, q.num, mx, my))
    for (ref, net), (a, c, x, y) in sorted(squeezed.items()):
        out.append(Finding("route.quality", "warning", f"{_short(net)} squeezes between {ref} pads {a} and {c}",
                           {"ref": ref, "net": net, "x": x, "y": y},
                           hint="Route around the part: a track between fine-pitch pads invites a solder bridge.",
                           key=f"quality:squeeze:{ref}:{net}"))
    med = f"{total['detour_median']:.2f}x" if total["detour_median"] else "-"
    out.append(Finding("route.quality", "info", f"scorecard: {total['nets']} routed signal nets, {total['length']:.0f} mm of track, "
                       f"{total['signal_vias']} signal vias, median detour {med}", key="quality:score"))
    examined(ctx, plural(len(nets), "routed net"))
    return out
