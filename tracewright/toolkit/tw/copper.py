"""Copper connectivity of one net as a graph (tracks, vias, pads, pours), for current-path checks.

    g = NetGraph(board, "+5V")
    width, path = g.widest_path(("U101", "2"), ("J201", "1"))   # bottleneck width (mm) and the edges

Nodes are track end points, via centres, pads and filled-pour islands; edges carry the copper width
the current must pass (a pour or a via counts as wide). A track end on the middle of another track
(a T) splits that track. The widest path is the route whose narrowest piece is as wide as possible,
i.e. the narrowest point the current cannot avoid.
"""
import heapq, math
from . import geom

WIDE = 1e6


class NetGraph:
    def __init__(self, board, net):
        self.b = board
        self.net = net
        self.adj = {}
        self.pads = {}
        self._build()

    def _node(self, key):
        self.adj.setdefault(key, [])
        return key

    def _edge(self, a, b, w, info):
        self.adj[self._node(a)].append((b, w, info))
        self.adj[self._node(b)].append((a, w, info))

    def _build(self):
        b, net = self.b, self.net
        tracks = [t for t in b.tracks if t.net == net]
        vias = [v for v in b.vias if v.net == net]
        pads = [p for p in b.pads() if p.net == net]
        pts = {}

        def key(layer, p):
            k = (layer, round(p[0], 3), round(p[1], 3))
            pts[k] = p
            return k
        # split tracks at T-junctions (end points of other tracks lying on them)
        ends = [(t.layer, t.a) for t in tracks] + [(t.layer, t.b) for t in tracks] + \
               [(l, (v.x, v.y)) for v in vias for l in b.copper]
        for t in tracks:
            cuts = [t.a, t.b]
            if not t.mid:
                for l, p in ends:
                    if l == t.layer and geom.seg_point_dist(p, t.a, t.b) < max(0.01, t.w * 0.2) and \
                            geom.dist(p, t.a) > 0.01 and geom.dist(p, t.b) > 0.01:
                        cuts.append(p)
                cuts.sort(key=lambda p: geom.dist(p, t.a))
            for p, q in zip(cuts, cuts[1:]):
                self._edge(key(t.layer, p), key(t.layer, q), t.w, ("track", t.layer, p, q, t.w))
        for v in vias:
            layers = v.span(b.copper)
            ks = [key(l, (v.x, v.y)) for l in layers]
            for k in ks[1:]:
                self._edge(ks[0], k, WIDE, ("via", v.x, v.y))
            for l in layers:
                self._attach_point(key(l, (v.x, v.y)), l, (v.x, v.y), tracks, v.d / 2)
        for p in pads:
            pk = ("pad", p.ref, p.num, round(p.x, 3), round(p.y, 3))
            self._node(pk)
            self.pads.setdefault((p.ref, p.num), []).append(pk)
            for l in p.layers:
                if not l.endswith(".Cu"):
                    continue
                for t in tracks:
                    if t.layer != l:
                        continue
                    for e in (t.a, t.b):
                        if geom.inside(e, p.poly) or geom.dist(e, (p.x, p.y)) < 0.01:
                            self._edge(pk, key(l, e), WIDE, ("pad", p.ref, p.num))
                if p.drill:
                    self._edge(pk, key(l, (p.x, p.y)), WIDE, ("pad", p.ref, p.num))
        # pours: every filled island touching a node joins it
        for z in b.zones:
            if z.is_rule_area or z.net != net:
                continue
            for layer, polys in z.fills.items():
                for i, poly in enumerate(polys):
                    zk = ("zone", z.name or z.net, layer, i)
                    bx = geom.bbox(poly)
                    hit = False
                    for k, p in list(pts.items()):
                        if k[0] == layer and bx[0] <= p[0] <= bx[2] and bx[1] <= p[1] <= bx[3] and geom.inside(p, poly):
                            self._edge(zk, k, WIDE, ("pour", layer))
                            hit = True
                    for pd in pads:
                        if layer in pd.layers and bx[0] <= pd.x <= bx[2] and bx[1] <= pd.y <= bx[3] and \
                                geom.inside((pd.x, pd.y), poly):
                            self._edge(zk, ("pad", pd.ref, pd.num, round(pd.x, 3), round(pd.y, 3)), WIDE, ("pour", layer))
                            hit = True
                    if not hit:
                        self._node(zk)

    def _attach_point(self, k, layer, p, tracks, r):
        for t in tracks:
            if t.layer != layer:
                continue
            for e in (t.a, t.b):
                if geom.dist(e, p) <= r and geom.dist(e, p) > 1e-3:
                    self._edge(k, (layer, round(e[0], 3), round(e[1], 3)), WIDE, ("via-pad",))

    def widest_path(self, src, dst):
        """(bottleneck width mm, [edge info]) between two pads given as (ref, pin); (0, None) if
        they are not connected by copper."""
        S = self.pads.get(tuple(src), [])
        D = set(self.pads.get(tuple(dst), []))
        if not S or not D:
            return 0.0, None
        best = {s: WIDE for s in S}
        prev = {}
        heap = [(-WIDE, s) for s in S]
        while heap:
            w, n = heapq.heappop(heap)
            w = -w
            if w < best.get(n, 0):
                continue
            if n in D:
                path, cur = [], n
                while cur in prev:
                    cur, info = prev[cur]
                    path.append(info)
                return w, path[::-1]
            for m, ew, info in self.adj.get(n, []):
                nw = min(w, ew)
                if nw > best.get(m, 0):
                    best[m] = nw
                    prev[m] = (n, info)
                    heapq.heappush(heap, (-nw, m))
        return 0.0, None

    def narrowest_on(self, path):
        worst = None
        for info in path or []:
            if info[0] == "track" and (worst is None or info[4] < worst[4]):
                worst = info
        return worst
