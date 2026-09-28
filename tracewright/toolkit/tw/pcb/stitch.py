"""Pour island repair: after a fill, a pour can split into islands that only reach the rest of their
net through nothing (tracks cut them off). Each island that is not part of the net's main copper
gets a via at its roomiest point that also lands in the main pour on another layer; refill; repeat.
"""
import math
from .. import geom
from ..board import Board


def _dist_to_edges(p, poly):
    n = len(poly)
    return min(geom.seg_point_dist(p, poly[i], poly[(i + 1) % n]) for i in range(n))


def _best_point(poly, need, step=0.2):
    """A point inside `poly` at least `need` from its boundary (the roomiest on a grid), or None."""
    x0, y0, x1, y1 = geom.bbox(poly)
    best = None
    y = y0 + step / 2
    while y < y1:
        x = x0 + step / 2
        while x < x1:
            if geom.inside((x, y), poly):
                d = _dist_to_edges((x, y), poly)
                if d >= need and (best is None or d > best[0]):
                    best = (d, (x, y))
            x += step
        y += step
    return best[1] if best else None


def islands(b, net):
    """[(layer, polygon)] fill islands of the net and a component id per island (vias, THT pads and
    tracks join them); returns (islands, comp, main_component_id)."""
    isl = []
    for z in b.zones:
        if z.is_rule_area or z.net != net:
            continue
        for layer, polys in z.fills.items():
            for pl in polys:
                if len(pl) >= 3:
                    isl.append((layer, pl, geom.bbox(pl)))
    parent = list(range(len(isl)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def containing(layer, p):
        return [k for k, (l, pl, bx) in enumerate(isl) if l == layer and bx[0] <= p[0] <= bx[2] and bx[1] <= p[1] <= bx[3]
                and geom.inside(p, pl)]
    joiners = [((v.x, v.y), b.copper) for v in b.vias if v.net == net]
    joiners += [((p.x, p.y), [l for l in p.layers if l.endswith(".Cu")]) for p in b.pads() if p.net == net and p.drill]
    for pos, layers in joiners:
        ks = [k for l in layers for k in containing(l, pos)]
        for k in ks[1:]:
            union(ks[0], k)
    for t in b.tracks:
        if t.net != net:
            continue
        ks = containing(t.layer, t.a) + containing(t.layer, t.b)
        for k in ks[1:]:
            union(ks[0], k)
    # SMD pads join the islands on their layer that they touch (a pad sitting across a split)
    for p in b.pads():
        if p.net != net or p.drill:
            continue
        for l in p.layers:
            if l.endswith(".Cu"):
                ks = containing(l, (p.x, p.y))
                for k in ks[1:]:
                    union(ks[0], k)
    comp = [find(k) for k in range(len(isl))]
    area = {}
    for k, (l, pl, bx) in enumerate(isl):
        area[comp[k]] = area.get(comp[k], 0.0) + abs(geom.area(pl))
    main = max(area, key=area.get) if area else None
    return isl, comp, main


def plan(b, net, via_d=0.6, via_drill=0.3, clearance=0.25):
    """Vias that join every stray island to the main copper: [{'net', 'x', 'y', 'd', 'drill'}]."""
    isl, comp, main = islands(b, net)
    if main is None:
        return []
    need = via_d / 2 + clearance
    mains = [(l, pl) for k, (l, pl, bx) in enumerate(isl) if comp[k] == main]
    out, done = [], set()
    for k, (l, pl, bx) in enumerate(isl):
        if comp[k] == main or comp[k] in done:
            continue
        if abs(geom.area(pl)) < 1.0:                # a scrap too small to matter (island removal takes it)
            continue
        # candidate points in the island, roomiest first, that also sit well inside main copper elsewhere
        pt = None
        x0, y0, x1, y1 = bx
        cands = []
        step = 0.25
        y = y0 + step / 2
        while y < y1:
            x = x0 + step / 2
            while x < x1:
                if geom.inside((x, y), pl):
                    d = _dist_to_edges((x, y), pl)
                    if d >= need:
                        cands.append((-d, (x, y)))
                x += step
            y += step
        cands.sort()
        for _, q in cands[:200]:
            for ml, mpl in mains:
                if ml != l and geom.inside(q, mpl) and _dist_to_edges(q, mpl) >= need:
                    pt = q
                    break
            if pt:
                break
        if pt:
            out.append({"net": net, "x": round(pt[0], 3), "y": round(pt[1], 3), "d": via_d, "drill": via_drill})
            done.add(comp[k])
    return out


def repair(project, net="GND", via_d=0.6, via_drill=0.3, passes=3, live="auto"):
    """Fill, then stitch stray islands and refill, up to `passes` times. Returns the vias added."""
    from . import client
    added = []
    client.apply(project, [{"op": "fill"}], live=live)
    for _ in range(passes):
        b = Board.load(project.pcb)
        if net not in b.nets:
            full = [n for n in b.nets if n.rsplit("/", 1)[-1] == net]
            if not full:
                break
            net = full[0]
        vias = plan(b, net, via_d, via_drill)
        if not vias:
            break
        client.apply(project, [{"op": "vias", "items": vias}, {"op": "fill"}], live=live)
        added += vias
    return added
