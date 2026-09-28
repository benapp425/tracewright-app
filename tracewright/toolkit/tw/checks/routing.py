"""Routing workmanship (differential pairs and other high-speed checks are in signal.py)."""
import math, re, collections
from . import check, Finding
from .. import geom


def _ends(tracks):
    """{(net, layer, rounded point): [track, ...]} for track end points."""
    ends = collections.defaultdict(list)
    for t in tracks:
        for p in (t.a, t.b):
            ends[(t.net, t.layer, round(p[0], 3), round(p[1], 3))].append(t)
    return ends


@check("route.style", "Routing workmanship", "Routing", needs=("pcb",))
def route_style(ctx):
    """Acute corners (acid traps), zero-length and tiny segments, vias inside SMD pads (solder
    wicks down the hole), and how much of the copper is off the 0/45/90-degree grid."""
    b = ctx.board
    out = []
    ends = _ends([t for t in b.tracks if not t.mid])
    for (net, layer, x, y), ts in ends.items():
        if len(ts) != 2:
            continue
        t1, t2 = ts
        p = (x, y)
        a = t1.b if geom.dist(t1.a, p) < 1e-3 else t1.a
        c = t2.b if geom.dist(t2.a, p) < 1e-3 else t2.a
        ang = geom.angle_between(a, p, c)
        if ang < 85.0 and geom.dist(a, p) > 0.05 and geom.dist(c, p) > 0.05:
            out.append(Finding("route.style", "warning", f"acute {ang:.0f} degree corner on {net or 'no net'} ({layer})",
                               {"net": net, "layer": layer, "x": x, "y": y}, key=f"route:acute:{net}:{layer}:{x}:{y}"))
    tiny = [t for t in b.tracks if not t.mid and geom.dist(t.a, t.b) < 0.005]
    if tiny:
        out.append(Finding("route.style", "info", f"{len(tiny)} zero-length track segments", key="route:zero"))
    smd = []
    for fp in b.fp_list:
        for p in fp.pads:
            if p.kind == "smd" and p.net:
                smd.append(p)
    for v in b.vias:
        for p in smd:
            if abs(p.x - v.x) > 3 or abs(p.y - v.y) > 3:
                continue
            if p.net == v.net and ("F.Cu" in p.layers or "B.Cu" in p.layers) and geom.inside((v.x, v.y), p.poly):
                if not ctx.setting("routing.via_in_pad_ok", False):
                    out.append(Finding("route.style", "warning", f"via inside SMD pad {p.ref}.{p.num} ({v.net})",
                                       {"ref": p.ref, "net": v.net, "x": v.x, "y": v.y},
                                       hint="Move the via beside the pad, or order filled/capped vias (via-in-pad).",
                                       key=f"route:viapad:{p.ref}:{p.num}"))
                break
    straight = [t for t in b.tracks if not t.mid and geom.dist(t.a, t.b) >= 0.2]
    off = [t for t in straight if not geom.octilinear(t.a, t.b)]
    if straight and len(off) / len(straight) > 0.05:
        nets = collections.Counter(t.net for t in off)
        out.append(Finding("route.style", "info", f"{len(off)} of {len(straight)} segments are not at 0/45/90 degrees "
                           f"(most on {', '.join(n for n, _ in nets.most_common(3))})", key="route:angles"))
    return out


@check("pcb.netclasses", "Net class patterns reach their nets", "Routing", needs=("pcb",))
def netclasses(ctx):
    """A net class's rules (width, clearance, pair geometry) apply only to the nets it gets. A pattern
    that matches no net (a renamed net, a typo) leaves those nets on the Default rules without a word,
    and DRC checks them against the wrong numbers. Nets are matched as KiCad does (explicit
    assignment, the first matching pattern, the schematic's own net class)."""
    import fnmatch, re as _re
    pro = ctx.pro
    nets = [n for n in ctx.board.nets if n]
    if not pro.patterns and len(pro.classes) <= 1:
        from . import NotApplicable
        raise NotApplicable("no net classes besides Default")
    nl = ctx.netlist if ctx.available("netlist") else None
    nl_class = {k.rsplit("/", 1)[-1]: v for k, v in (nl.net_class.items() if nl else [])}

    def match(pat, n):
        if pat.startswith("/") and pat.endswith("/") and len(pat) > 2:
            try:
                return _re.search(pat[1:-1], n) is not None
            except _re.error:
                return False
        return fnmatch.fnmatchcase(n, pat) or fnmatch.fnmatchcase(n.rsplit("/", 1)[-1], pat)
    out = []
    members = collections.Counter(pro.class_of(n, nl_class.get(n.rsplit("/", 1)[-1])) for n in nets)
    for pat, cls in pro.patterns:
        if not any(match(pat, n) for n in nets):
            out.append(Finding("pcb.netclasses", "warning", f"net class {cls}'s pattern '{pat}' matches no net",
                               {"type": "netclass"},
                               hint="Correct the pattern in the Rules tab (it shows what each pattern matches), or remove it.",
                               key=f"netclass:pattern:{cls}:{pat}"))
        if cls not in pro.classes:
            out.append(Finding("pcb.netclasses", "error", f"pattern '{pat}' assigns net class {cls}, which does not exist",
                               {"type": "netclass"}, key=f"netclass:missing:{cls}"))
    for name in pro.classes:
        if name != "Default" and not members.get(name):
            out.append(Finding("pcb.netclasses", "warning", f"net class {name} has no nets, so its rules never apply",
                               {"type": "netclass"}, key=f"netclass:empty:{name}"))
    return out
