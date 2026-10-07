"""The ratsnest: what is still unconnected on the board, worked out from the copper itself (not the last DRC), so
the editor shows it the moment a track is drawn or deleted.

For each net, its copper is grouped into islands -- pads, track segments, vias and pour fills that touch on a
shared layer -- and the islands are joined by the shortest links between them (a minimum spanning tree over the
islands, as KiCad draws its ratsnest). Each link runs between two points of the islands it joins: pad centres,
via centres or track ends.

    ratsnest(board)  ->  [[x1, y1, x2, y2, net], ...]
"""
import math
from . import geom

GRID = 2.0                                  # mm: the spatial hash's cell for joining items


def _inside_many(pts, poly):
    """Which of the points lie inside the polygon (even-odd), all at once."""
    import numpy as np
    if not pts:
        return []
    P = np.asarray(pts, dtype=float)
    V = np.asarray(poly, dtype=float)
    x, y = P[:, 0][:, None], P[:, 1][:, None]
    x0, y0 = V[:, 0][None, :], V[:, 1][None, :]
    x1, y1 = np.roll(V[:, 0], -1)[None, :], np.roll(V[:, 1], -1)[None, :]
    cond = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    hit = cond & (x < xs)
    return list((hit.sum(axis=1) % 2) == 1)


_CACHE = {}                                  # board path -> {net: (signature, links)}


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, i):
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _cells(box):
    x0, y0, x1, y1 = box
    for i in range(int(math.floor(x0 / GRID)), int(math.floor(x1 / GRID)) + 1):
        for j in range(int(math.floor(y0 / GRID)), int(math.floor(y1 / GRID)) + 1):
            yield (i, j)


def _seg_dist(p, a, b):
    return geom.seg_point_dist(p, a, b)


def _copper_layers(layers, copper):
    out = set()
    for l in layers or []:
        if l == "*.Cu":
            out.update(copper)
        elif l.endswith(".Cu"):
            out.add(l)
    return out


def ratsnest(b):
    copper = list(b.copper)
    by_net = {}
    def add(net, item):
        if net and not net.startswith("unconnected-"):
            by_net.setdefault(net, []).append(item)
    for fp in b.fp_list:
        for pd in fp.pads:
            if not pd.net:
                continue
            ls = _copper_layers(pd.layers, copper)
            if not ls:
                continue
            polys = [pl for pl in (pd.polys or []) if len(pl) >= 3]
            box = geom.bbox([q for pl in polys for q in pl]) if polys else (pd.x - pd.w / 2, pd.y - pd.h / 2, pd.x + pd.w / 2, pd.y + pd.h / 2)
            jk = fp.jumper_key(pd.num)                    # joined inside the footprint by KiCad, or None
            add(pd.net, ("pad", ls, (pd.x, pd.y), polys, (fp.ref, jk, min(pd.w, pd.h) / 2), box))
    for t in b.tracks:
        a, c = tuple(t.a), tuple(t.b)
        r = t.w / 2
        box = (min(a[0], c[0]) - r, min(a[1], c[1]) - r, max(a[0], c[0]) + r, max(a[1], c[1]) + r)
        add(t.net, ("track", {t.layer}, a, c, r, box))
    for v in b.vias:
        ls = set(v.span(copper))
        r = v.d / 2
        add(v.net, ("via", ls, (v.x, v.y), r, (v.x - r, v.y - r, v.x + r, v.y + r)))
    for z in b.zones:
        if z.is_rule_area or not z.net:
            continue
        for layer, polys in (z.fills or {}).items():
            for pl in polys:
                if len(pl) >= 3:
                    cells = {}                          # the outline's corners by grid cell: spokes reaching into a pad
                    for v in pl:
                        cells.setdefault((int(math.floor(v[0] / GRID)), int(math.floor(v[1] / GRID))), []).append(v)
                    add(z.net, ("fill", {layer}, pl, cells, geom.bbox(pl)))
    out = []
    cache = _CACHE.setdefault(getattr(b, "path", None) or id(b), {})
    for net, items in by_net.items():
        if sum(1 for it in items if it[0] == "pad") < 2:
            continue
        sig = hash(tuple(_sig(it) for it in items))
        hitc = cache.get(net)
        if hitc and hitc[0] == sig:                     # this net's copper did not change: its links neither
            out.extend(hitc[1])
            continue
        links = _net_links(net, items)
        cache[net] = (sig, links)
        out.extend(links)
    return out


def _sig(it):
    k = it[0]
    if k == "fill":
        pl = it[2]
        return (k, tuple(it[1]), len(pl), pl[0], pl[len(pl) // 2])
    if k == "pad":
        return (k, tuple(sorted(it[1])), it[2], it[4], it[5])
    return (k, tuple(sorted(it[1])), it[2:-1])


def _net_links(net, items):
    out = []
    if True:
        uf = _UF(len(items))
        solid = [i for i, it in enumerate(items) if it[0] != "fill"]
        fills = [i for i, it in enumerate(items) if it[0] == "fill"]
        grid = {}
        for i in solid:
            for c in _cells(items[i][-1]):
                grid.setdefault(c, []).append(i)
        seen = set()
        for cell, idx in grid.items():
            for ii in range(len(idx)):
                for jj in range(ii + 1, len(idx)):
                    i, j = idx[ii], idx[jj]
                    if (i, j) in seen:
                        continue
                    seen.add((i, j))
                    if _touch(items[i], items[j]):
                        uf.union(i, j)
        same = {}                                        # pads the footprint joins (jumpers): KiCad counts them as one
        for i in solid:
            if items[i][0] == "pad" and items[i][4][1] is not None:
                same.setdefault(items[i][4][:2], []).append(i)
        for grp in same.values():
            for i in grp[1:]:
                uf.union(grp[0], i)
        for fi in fills:                                 # each fill polygon: what lies in it, or what its spokes reach
            f = items[fi]
            fb = f[-1]
            cand = [i for i in solid if (items[i][1] & f[1]) and _box_meet(items[i][-1], fb, 0.02)]
            if not cand:
                continue
            pts, owner = [], []
            for i in cand:
                it = items[i]
                if it[0] == "track":
                    pts += [it[2], it[3]]
                    owner += [i, i]
                else:
                    pts.append(it[2])
                    owner.append(i)
            for i, inside in zip(owner, _inside_many(pts, f[2])):
                if inside:
                    uf.union(i, fi)
            for i in cand:                               # thermal spokes: the fill's corners reaching into a pad or via
                if uf.find(i) == uf.find(fi):
                    continue
                it = items[i]
                if it[0] == "pad" and any(_pt_in_pad(v, it, 0.0) for v in _corners_in(f, it[-1])):
                    uf.union(i, fi)
                elif it[0] == "via" and any(math.dist(v, it[2]) <= it[3] + 1e-3 for v in _corners_in(f, it[4])):
                    uf.union(i, fi)
        groups = {}
        for i in range(len(items)):
            groups.setdefault(uf.find(i), []).append(i)
        # only islands with a pad (or a via) need joining; a stray fill or track stub is not an unconnected pin
        isl = [g for g in groups.values() if any(items[i][0] in ("pad", "via") for i in g)]
        if len(isl) < 2:
            return out
        anchors = [[_anchor(items[i]) for i in g if items[i][0] != "fill"] or [_anchor(items[g[0]])] for g in isl]
        anchors = [[p for a in al for p in a] for al in anchors]
        # Prim's tree over the islands, by the closest pair of anchors
        n = len(isl)
        best = [(math.inf, None, None)] * n
        used = [False] * n
        used[0] = True
        def relax(k):
            for m in range(n):
                if used[m]:
                    continue
                d, pa, pb = _closest(anchors[k], anchors[m])
                if d < best[m][0]:
                    best[m] = (d, pa, pb)
        relax(0)
        for _ in range(n - 1):
            m = min((i for i in range(n) if not used[i]), key=lambda i: best[i][0])
            d, pa, pb = best[m]
            used[m] = True
            if pa is not None:
                out.append([round(pa[0], 4), round(pa[1], 4), round(pb[0], 4), round(pb[1], 4), net])
            relax(m)
    return out


def _anchor(it):
    k = it[0]
    if k == "pad":
        return [it[2]]
    if k == "track":
        return [it[2], it[3]]
    if k == "via":
        return [it[2]]
    pl = it[2]
    return [pl[0]]


def _closest(A, B):
    best = (math.inf, None, None)
    for a in A:
        for b in B:
            d = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
            if d < best[0]:
                best = (d, a, b)
    return (math.sqrt(best[0]), best[1], best[2]) if best[1] is not None else best


def _box_meet(a, b, m=0.0):
    return a[0] <= b[2] + m and b[0] <= a[2] + m and a[1] <= b[3] + m and b[1] <= a[3] + m


def _touch(p, q):
    """Do two items of one net touch on a shared copper layer?"""
    if not (p[1] & q[1]) or not _box_meet(p[-1], q[-1], 0.02):
        return False
    if p[0] > q[0]:                                   # order the pair: fill < pad < track < via
        p, q = q, p
    a, b = p[0], q[0]
    if a == "pad" and b == "pad":
        if p[4][1] is not None and p[4][:2] == q[4][:2]:  # pads the footprint joins (jumpers)
            return True
        return any(geom.inside(pt, pl) for pl in q[3] for pt in [p[2]]) or any(geom.inside(q[2], pl) for pl in p[3])
    if a == "pad" and b == "track":
        r = q[4]
        if any(_pt_in_pad(e, p, r) for e in (q[2], q[3])):
            return True
        # a track running over the pad without ending in it
        if _seg_dist(p[2], q[2], q[3]) <= r + p[4][2] + 1e-3:
            return True
        return any(_seg_dist(v, q[2], q[3]) <= r + 1e-3 for pl in p[3] for v in pl)
    if a == "pad" and b == "via":
        return _pt_in_pad(q[2], p, q[3])
    if a == "track" and b == "track":
        return min(_seg_dist(p[2], q[2], q[3]), _seg_dist(p[3], q[2], q[3]), _seg_dist(q[2], p[2], p[3]), _seg_dist(q[3], p[2], p[3])) \
            <= max(p[4], q[4]) + 1e-3
    if a == "track" and b == "via":
        return min(math.dist(p[2], q[2]), math.dist(p[3], q[2]), _seg_dist(q[2], p[2], p[3])) <= q[3] + 1e-3
    if a == "via" and b == "via":
        return math.dist(p[2], q[2]) <= p[3] + q[3]
    if a == "fill":
        pl = p[2]
        if b == "pad":                                    # solid: the pad inside the fill; thermal: a spoke reaches in
            if geom.inside(q[2], pl) or any(geom.inside(v, pl) for poly in q[3] for v in poly):
                return True
            return any(_pt_in_pad(v, q, 0.0) for v in _corners_in(p, q[-1]))
        if b == "track":
            return geom.inside(q[2], pl) or geom.inside(q[3], pl)
        if b == "via":
            if geom.inside(q[2], pl):
                return True
            x, y = q[2]
            return any(math.dist(v, (x, y)) <= q[3] + 1e-3 for v in _corners_in(p, q[4]))
        if b == "fill":
            return False                                  # two fill polygons of one zone do not touch by themselves
    return False


def _corners_in(fill, box):
    """The fill outline's corners inside a box (from its grid cells)."""
    cells = fill[3]
    out = []
    for c in _cells(box):
        for v in cells.get(c, ()):
            if box[0] - 1e-3 <= v[0] <= box[2] + 1e-3 and box[1] - 1e-3 <= v[1] <= box[3] + 1e-3:
                out.append(v)
    return out


def _pt_in_pad(pt, pad, r):
    if any(geom.inside(pt, pl) for pl in pad[3]):
        return True
    x0, y0, x1, y1 = pad[5]
    if not pad[3]:
        return x0 - r <= pt[0] <= x1 + r and y0 - r <= pt[1] <= y1 + r
    return any(_seg_dist(pt, pl[i], pl[(i + 1) % len(pl)]) <= r + 1e-3 for pl in pad[3] for i in range(len(pl)))
