"""Routability: can the nets be routed in the room and the layers the board has? Read before the router spends its
time on a board that cannot work, and again after each placement change.

    rep = estimate(project, board=None)
        {"verdict": "fine" | "tight" | "impossible", "lines": [...], "fixes": [...],
         "escapes": [{ref, need, have, spare}], "cuts": [{axis, at, crossing, capacity, use}], "demand": {...}}

Three readings, each the way a designer sizes a board:
  - escapes: each ball-grid part's rings against the signal layers (tw.escape). A part that needs every layer the
    board has is tight: the escape has no room to give where a capacitor or a via sits in its way.
  - cut lines: lines across the whole board, every 1 mm in x and in y. The nets that must cross a line (their spanning
    trees' edges that do) against the tracks that fit across it: its length less the pads, holes and keep-outs on it,
    at the board's track pitch, on every signal layer. Above 70% of a line is tight, above 100% it cannot be done.
  - demand: the nets' total length (spanning trees, plus a third for detours) at the track pitch, against the board's
    routing area on all its signal layers; above 45% the router struggles, above 70% it fails.
"""
import math, collections

from . import geom

CUT_TIGHT, CUT_FULL = 0.7, 1.0
DEMAND_TIGHT, DEMAND_FULL = 0.45, 0.7
DETOUR = 1.33


def _mst_edges(pts):
    """The spanning tree of a net's pads (Prim): [(a, b)]."""
    pts = list(dict.fromkeys(pts))
    if len(pts) < 2:
        return []
    inside, out = {0}, []
    best = {i: (geom.dist(pts[0], pts[i]), 0) for i in range(1, len(pts))}
    while best:
        i = min(best, key=lambda k: best[k][0])
        d, j = best.pop(i)
        out.append((pts[j], pts[i]))
        inside.add(i)
        for k in best:
            dk = geom.dist(pts[i], pts[k])
            if dk < best[k][0]:
                best[k] = (dk, i)
    return out


def _signal_layers(project, b):
    from . import stackup
    return stackup.routing_layers(stackup.get(getattr(project, "cfg", None)), b.copper)


def estimate(project, board=None):
    from .board import Board
    from . import escape
    b = board or Board.load(project.pcb)
    if not b.outline:
        return {"verdict": "unknown", "lines": ["no board outline yet"], "fixes": [], "escapes": [], "cuts": [], "demand": {}}
    layers = _signal_layers(project, b)
    nl = len(layers)
    rules = escape._rules(project, b)
    pitch = rules["w_class"] + rules["s_class"]
    outer = max(b.outline, key=lambda o: abs(geom.area(o)))
    x0, y0, x1, y1 = geom.bbox(outer)
    lines, fixes = [], []
    worst = "fine"

    def rate(level):
        nonlocal worst
        order = {"fine": 0, "tight": 1, "impossible": 2}
        if order[level] > order[worst]:
            worst = level
    # 1. escapes
    esc = []
    for r in escape.plan(project, b):
        if r.get("kind") != "array":
            continue
        spare = r["have"] - r["need"]
        esc.append({"ref": r["ref"], "need": r["need"], "have": r["have"], "spare": spare, "method": r["method"]})
        if r["method"] is None or spare < 0:
            rate("impossible")
            lines.append(f"{r['ref']}: its balls need {r['need']} signal layers to get out, the board has {r['have']}")
            fixes.append(f"{r['need'] + 1 if r['need'] + 1 <= 12 else r['need']} or more copper layers with {r['need']} signal layers, or HDI (vias in the pads)")
        elif spare == 0:
            rate("tight")
            lines.append(f"{r['ref']}: its balls need all {r['have']} signal layers the board has, none to spare (a part under it "
                         "or a via in the way leaves a ball with nowhere to go)")
            fixes.append("one more signal layer gives the escape room to spare")
    # 2. cut lines
    nets = collections.defaultdict(list)
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net and not p.net.startswith("unconnected-"):
                nets[p.net].append((p.x, p.y))
    planes = {z.net for z in b.zones if z.net and not z.is_rule_area}
    edges = [e for n, pts in nets.items() if n not in planes for e in _mst_edges(pts)]
    blockers = []                                      # (x0, y0, x1, y1, layers or None for every layer)
    for fp in b.fp_list:
        for p in fp.pads:
            bx = geom.bbox(p.poly) if p.poly else (p.x - p.w / 2, p.y - p.h / 2, p.x + p.w / 2, p.y + p.h / 2)
            ls = None if p.drill else [l for l in p.layers if l in layers]
            blockers.append((bx[0] - rules["s_class"], bx[1] - rules["s_class"], bx[2] + rules["s_class"], bx[3] + rules["s_class"], ls))
    for fp in b.fp_list:                               # a ball field is full once broken out: vias and escapes on every layer
        if escape.grid(fp):
            xs = [p.x for p in fp.pads]; ys = [p.y for p in fp.pads]
            blockers.append((min(xs), min(ys), max(xs), max(ys), None))
    for z in b.zones:
        if z.is_rule_area and z.outline and not z.name.startswith("TW "):
            bx = geom.bbox([q for pl in z.outline for q in pl])
            blockers.append((*bx, [l for l in z.layers if l in layers]))
    cuts = []
    for axis, lo, hi, a0, a1 in (("x", x0, x1, y0, y1), ("y", y0, y1, x0, x1)):
        k = math.floor(lo) + 1.0
        while k < hi:
            crossing = 0
            for a, c in edges:
                p, q = (a[0], c[0]) if axis == "x" else (a[1], c[1])
                if min(p, q) < k <= max(p, q):
                    crossing += 1
            cap = 0
            span = a1 - a0
            for l in layers:
                taken = []
                for bx0, by0, bx1, by1, ls in blockers:
                    if ls is not None and l not in ls:
                        continue
                    lo_, hi_ = (bx0, bx1) if axis == "x" else (by0, by1)
                    if lo_ <= k <= hi_:
                        taken.append((by0, by1) if axis == "x" else (bx0, bx1))
                taken.sort()
                used, cur = 0.0, None
                for s0, s1 in taken:
                    s0, s1 = max(s0, a0), min(s1, a1)
                    if s1 <= s0:
                        continue
                    if cur and s0 <= cur[1]:
                        cur = (cur[0], max(cur[1], s1))
                    else:
                        if cur:
                            used += cur[1] - cur[0]
                        cur = (s0, s1)
                if cur:
                    used += cur[1] - cur[0]
                cap += max(0, int((span - used) / pitch))
            use = crossing / cap if cap else (math.inf if crossing else 0.0)
            cuts.append({"axis": axis, "at": round(k, 2), "crossing": crossing, "capacity": cap, "use": round(use, 3)})
            k += 1.0
    hot = sorted(cuts, key=lambda c: -c["use"])[:3]
    for c in hot:
        where = f"the line x = {c['at']:g}" if c["axis"] == "x" else f"the line y = {c['at']:g}"
        if c["use"] > CUT_FULL:
            rate("impossible")
            lines.append(f"{c['crossing']} nets must cross {where}, only {c['capacity']} tracks fit across it on {nl} signal layers")
        elif c["use"] > CUT_TIGHT:
            rate("tight")
            lines.append(f"{c['crossing']} nets cross {where}: {round(100 * c['use'])}% of the {c['capacity']} tracks that fit")
    if any(c["use"] > CUT_TIGHT for c in hot):
        fixes.append("more signal layers, a larger board along the crowded line, or the parts moved so fewer nets cross it")
    # 3. demand
    need_mm2 = sum(geom.dist(a, c) for a, c in edges) * DETOUR * pitch
    area = (x1 - x0) * (y1 - y0) * nl
    taken = sum((bx1 - bx0) * (by1 - by0) * (len(ls) if ls is not None else nl)
                for bx0, by0, bx1, by1, ls in blockers)
    room = max(1e-6, area - taken)
    dem = need_mm2 / room
    if dem > DEMAND_FULL:
        rate("impossible")
        lines.append(f"the nets need about {need_mm2:.0f} mm² of track, {round(100 * dem)}% of the {room:.0f} mm² free on {nl} signal layers")
        fixes.append("a larger board or more signal layers")
    elif dem > DEMAND_TIGHT:
        rate("tight")
        lines.append(f"the nets need about {round(100 * dem)}% of the free area on {nl} signal layers: dense for the router")
    if not lines:
        lines.append(f"the busiest line across the board is {round(100 * hot[0]['use']) if hot else 0}% full, the tracks need "
                     f"{round(100 * dem)}% of the free area on {nl} signal layers"
                     + (f"; {', '.join(e['ref'] + ' escapes with ' + str(e['spare']) + ' layer' + ('s' if e['spare'] != 1 else '') + ' to spare' for e in esc)}" if esc else ""))
    return {"verdict": worst, "lines": lines, "fixes": list(dict.fromkeys(fixes)), "escapes": esc, "cuts": hot,
            "demand": {"need_mm2": round(need_mm2), "free_mm2": round(room), "use": round(dem, 3), "signal_layers": nl,
                       "pitch": pitch}}
