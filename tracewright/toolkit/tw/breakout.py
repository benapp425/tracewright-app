"""Breakout: the pins of dense parts brought out before the rest of the board is routed, the way a person starts a board
with a BGA or a fine-pitch connector.

Ball-grid parts. A dog-bone via for every ball that cannot leave on the part's own layer, and for every power and ground
ball (to its plane). Then, one layer at a time, outer rings first, each signal ball's track from its via (or its pad, on
the part's own layer) straight out to the edge of the ball field on its side -- the side it is nearest, the side facing
the rest of its net when two are as near. That end is the net's port: the router picks the net up there. A ball whose
track does not fit on a layer tries the next one, so the escape fills the layers the way the ring count says it can.

Fine-pitch parts whose nets change layer beside them (a connector on the bottom fed from a part on top): a short stub out
of each pad, away from the part, to a via; the vias in two staggered rows, as the pitch is too fine for one.

    rep = run(project, refs=None, apply=True)   {"parts": [{ref, kind, balls, vias, escaped, by_layer, left, ...}], ...}
    ports(project)                              {net: [{"ref", "pad", "layer", "at", "via"}]} (build/breakout.json)
    prune(project)                              the escapes the router did not use, taken off again

Every track is laid by the grid router itself, on its legality raster (DRC-clean as laid); `./tw drc` stays the judge.
"""
import json, math, os, collections

import numpy as np

from . import geom

DPORT = 1.0          # a port this many pitches beyond the outermost ball row: clear of the outer dog-bone vias
UTIL = 0.6           # the share of a channel's tracks the breakout fills on one layer: the router needs the rest


def _path(project):
    return os.path.join(project.build, "breakout.json")


def ports(project):
    """{net: [port, ...]} from the last breakout."""
    try:
        with open(_path(project)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    out = collections.defaultdict(list)
    for part in d.get("parts", []):
        for e in part.get("escapes", []):
            out[e["net"]].append(e)
    return dict(out)


def fields(project):
    """[(ref, (x0, y0, x1, y1))]: the ball fields the last breakout covered -- every ball there has its via; the router
    adds none of its own (one more in the field walls a plane's own vias off from the plane)."""
    try:
        with open(_path(project)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    return [(p["ref"], tuple(p["field"])) for p in d.get("parts", []) if p.get("field")]


def _layer_order(surface, routing):
    """The layers a part's escape fills: its own first, then the inner signal layers nearest to it, the far side last."""
    rs = list(routing)
    if surface not in rs:
        return rs
    i = rs.index(surface)
    inner = [l for l in rs if l not in ("F.Cu", "B.Cu")]
    inner.sort(key=lambda l: abs(rs.index(l) - i))
    far = [l for l in ("F.Cu", "B.Cu") if l in rs and l != surface]
    return [surface] + inner + far


class _Grid:
    """A ball grid in board coordinates: row 0 the top (smallest y), column 0 the left."""

    def __init__(self, fp, g):
        self.fp, self.g = fp, g
        self.P = g["pitch"]
        self.R, self.C = g["rows"], g["cols"]
        xs = sorted({round(p.x, 3) for p, _, _, _ in g["balls"]})
        ys = sorted({round(p.y, 3) for p, _, _, _ in g["balls"]})
        self.X0, self.X1, self.Y0, self.Y1 = xs[0], xs[-1], ys[0], ys[-1]
        self.cx, self.cy = g["cx"], g["cy"]

    def sides(self, r, c):
        """The sides a ball is nearest (N, S, W, E), by its distance in rows to each edge."""
        d = {"N": r, "S": self.R - 1 - r, "W": c, "E": self.C - 1 - c}
        m = min(d.values())
        return [k for k, v in d.items() if v == m]

    def band(self, side, depth=0.25):
        """The port band beyond a side: (x0, y0, x1, y1)."""
        P, e = self.P, DPORT * self.P
        if side == "N":
            return (self.X0 - P, self.Y0 - e - depth, self.X1 + P, self.Y0 - e)
        if side == "S":
            return (self.X0 - P, self.Y1 + e, self.X1 + P, self.Y1 + e + depth)
        if side == "W":
            return (self.X0 - e - depth, self.Y0 - P, self.X0 - e, self.Y1 + P)
        return (self.X1 + e, self.Y0 - P, self.X1 + e + depth, self.Y1 + P)

    def field(self, m=0.0):
        P = self.P
        return (self.X0 - P / 2 - m, self.Y0 - P / 2 - m, self.X1 + P / 2 + m, self.Y1 + P / 2 + m)


def _cells_in(B, box, li, lt):
    x0, y0, x1, y1 = box
    m = B._rect(x0, y0, x1, y1).ravel() & (lt[li] == 1)
    return [(li, int(i)) for i in m.nonzero()[0]]


def _via_ok(B, lv, x, y):
    return bool(lv[B.idx(x, y)])


def _seg_ok(B, lt, li, a, b):
    """Every cell along a straight run legal on layer li."""
    n = max(2, int(geom.dist(a, b) / 0.05) + 1)
    for k in range(n):
        t = k / (n - 1)
        if not lt[li][B.idx(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)]:
            return False
    return True


def _channel_caps(B, G, rules):
    """{side: tracks}: how many tracks fit side by side in the channel beyond each side of the ball field, measured
    outward from the port band to the board's edge or the first copper on every layer (a through-hole pad), at the
    class pitch; the narrow quarter of the side's samples counts."""
    pitch = rules["w_class"] + rules["s_class"]
    out = {}
    for sd in ("N", "S", "W", "E"):
        x0, y0, x1, y1 = G.band(sd)
        widths = []
        for k in range(9):
            f = (k + 0.5) / 9
            if sd in ("W", "E"):
                y, x, step = y0 + (y1 - y0) * f, (x0 if sd == "W" else x1), (-0.1 if sd == "W" else 0.1)
                pts = lambda d: (x + step * d, y)
            else:
                x, y, step = x0 + (x1 - x0) * f, (y0 if sd == "N" else y1), (-0.1 if sd == "N" else 0.1)
                pts = lambda d: (x, y + step * d)
            d = 0
            while d < 400:
                px, py = pts(d)
                j, i = B.cell(px, py)
                if not (0 <= j < B.ny and 0 <= i < B.nx):
                    break
                # copper on every layer, or none allowed: the board's edge, a hole, a through-hole pad
                if all(B.T[next(iter(B.profiles))][li][j, i] != -1 for li in range(len(B.layers))):
                    break
                d += 1
            widths.append(d * 0.1)
        widths.sort()
        w = widths[len(widths) // 4]
        out[sd] = max(4, int(w / pitch))
    return out


def _others(pads_by_net, net, ref):
    return [p for p in pads_by_net.get(net, []) if p["ref"] != ref]


def _area_part(g, fp, kinds, rules, routing, pads_by_net, planes, log):
    """Vias and escapes for one ball-grid part. Works on the router's raster (g.B), stamping as it goes."""
    from . import escape
    from .route import router as R
    B = g.B
    gr = escape.grid(fp)
    if not gr:
        return None
    G = _Grid(fp, gr)
    P = G.P
    surface = next((l for p, _, _, _ in gr["balls"] for l in p.layers if l.endswith(".Cu")), "F.Cu")
    if surface not in B.layers:
        return {"ref": fp.ref, "kind": "array", "error": f"{surface} is not a routing layer"}
    order = _layer_order(surface, B.layers)
    plan = escape.plan_part(fp, gr, kinds, rules, len(routing), {"on": False, "via_in_pad": False, "microvias": False,
                                                                  "micro_d": 0.25, "micro_drill": 0.1})
    top = plan["top_rings"]
    dump_pad = {(p["ref"], p["num"]): p for p in g.dump["pads"]}
    field = G.field(0.3)
    # which balls: signals the rest of the board needs (not one-pad nets, not nets that only go under the part)
    sig, sup = [], []
    for p, r, c, ring in gr["balls"]:
        net = p.net
        if not net or net.startswith("unconnected-") or not g._net_ok(net):
            continue
        other = _others(pads_by_net, net, fp.ref)
        if net in planes:                           # a plane's net: a via down to it is all it needs
            sup.append((p, r, c, ring))
            continue
        # a supply without a plane goes out like a signal: inside the field nothing reaches it once the escapes are in
        if not other:
            continue
        # down, not out: a net with a part under the ball field on the far side (a capacitor under a BGA) is joined
        # there, through the ball's via; escaping it to the edge would send the route back in under the part
        inside = lambda q: field[0] <= q["pos"][0] <= field[2] and field[1] <= q["pos"][1] <= field[3]
        under = all(inside(q) for q in other) or any(inside(q) and surface not in q["layers"] and q["drill"] == 0 for q in other)
        sig.append((p, r, c, ring, under, other))
    prof_of = lambda n: g.net_class.get(n, "Default")
    vd = rules["via_d"]
    taken = {(round(v["pos"][0], 2), round(v["pos"][1], 2)): v["net"] for v in g.dump["vias"]}
    have_via = {}
    stubs, vias = [], []

    li_s = B.layers.index(surface)

    def outward(p):
        sx = 1 if p.x >= G.cx - 1e-6 else -1
        sy = 1 if p.y >= G.cy - 1e-6 else -1
        return [(sx, sy), (sx, -sy), (-sx, sy), (-sx, -sy)]

    def spots(p, prefer):
        """The ball's diagonal via spots that are legal for its net (its stub too), in order of preference."""
        lt, lv = B.legal(p.net, prof_of(p.net))
        out = []
        for sx, sy in prefer:
            x, y = round(p.x + sx * P / 2, 4), round(p.y + sy * P / 2, 4)
            k = (round(x, 2), round(y, 2))
            if k in taken and taken[k] != p.net:
                continue
            if _via_ok(B, lv, x, y) and _seg_ok(B, lt, li_s, (p.x, p.y), (x, y)):
                out.append((x, y))
        return out

    def lay(p, xy):
        x, y = xy
        k = (round(x, 2), round(y, 2))
        B.stamp_track(p.net, surface, (p.x, p.y), (x, y), rules["w"])
        stubs.append({"net": p.net, "layer": surface, "a": [round(p.x, 4), round(p.y, 4)], "b": [x, y], "w": rules["w"]})
        if k not in taken:
            B.stamp_via(p.net, (x, y), vd, drill=rules["via_drill"])
            taken[k] = p.net
            vias.append({"net": p.net, "x": x, "y": y, "d": vd, "drill": rules["via_drill"]})
        have_via[(fp.ref, p.num)] = (x, y)

    # 1. vias for the signals past the rings the part's own layer takes out (and those that only go under the part): a
    # matching of balls to spots, so a ball with one free spot gets it and its neighbours take their others (parts on the
    # far side, a decoupling capacitor under the part, take spots)
    need = [s_ for s_ in sig if s_[3] >= top or s_[4]]
    # the channels beside the ball field: a net whose far end lies past the field's other end runs the length of the
    # channel on its escape's side (or, from the top or bottom side, of the nearer channel round it). Each channel takes
    # on each layer the tracks that fit across it, filled to a share that leaves the router room (UTIL); the outer balls
    # a channel's surface share has no room for go down by a via too (inner ring first)
    cap = {k: max(3, int(v * UTIL)) for k, v in _channel_caps(B, G, rules).items()}

    def chan_of(sd, other):
        ox, oy = sum(q["pos"][0] for q in other) / len(other), sum(q["pos"][1] for q in other) / len(other)
        if sd in ("W", "E"):
            return None if G.Y0 - 1 <= oy <= G.Y1 + 1 else sd
        if (sd == "N" and oy > G.Y1 + 1) or (sd == "S" and oy < G.Y0 - 1):
            return "W" if ox < G.cx else "E"
        return None
    near = collections.defaultdict(list)
    for s_ in sig:
        if s_[3] < top and not s_[4]:
            d_ = {"N": s_[1], "S": G.R - 1 - s_[1], "W": s_[2], "E": G.C - 1 - s_[2]}
            ch = chan_of(min(d_, key=d_.get), s_[5])
            if ch:
                near[ch].append(s_)
    for ch, ss in near.items():
        if len(ss) > cap[ch]:
            need += sorted(ss, key=lambda s_: -s_[3])[:len(ss) - cap[ch]]
    cand = {s_[0].num: [(round(x, 2), round(y, 2)) for x, y in spots(s_[0], outward(s_[0]))] for s_ in need}
    match = {}

    def assign(num, seen):
        for k in cand[num]:
            if k in seen:
                continue
            seen.add(k)
            if k not in match or assign(match[k], seen):
                match[k] = num
                return True
        return False
    for s_ in sorted(need, key=lambda s_: (len(cand[s_[0].num]), s_[3])):
        assign(s_[0].num, set())
    by_num = {s_[0].num: s_[0] for s_ in need}
    for k, num in match.items():
        lay(by_num[num], k)
    # supplies: their own spot, or one a neighbour of the same net already has (a via shared by two balls)
    for p, r, c, ring in sorted(sup, key=lambda s_: -s_[3]):
        pref = list(reversed(outward(p))) if ring < top else outward(p)
        opts = spots(p, pref)
        shared = [xy for xy in opts if taken.get((round(xy[0], 2), round(xy[1], 2))) == p.net]
        pick = (shared or opts or [None])[0]
        if pick:
            lay(p, pick)

    # 2. escapes, layer by layer, outer rings first. Each side's channel takes as many tracks on a layer as fit across it
    # (to the board's edge or the first row of through-hole pads): more there would have the router change layer in the
    # channel, where its via takes room; inside the ball field the dog-bone via costs nothing
    Re = R.Router(B, bend45=30, bend90=160, via=10_000, bcu_cross=1.0, directions={l: "any" for l in B.layers})
    Re.fcu_cross = 1.0
    todo = [s for s in sig if not s[4]]
    done, by_layer, left, long_bones = [], collections.Counter(), [], []
    count = collections.Counter()
    cen = lambda pts: (sum(q[0] for q in pts) / len(pts), sum(q[1] for q in pts) / len(pts))
    for L in order:
        li = B.layers.index(L)
        nxt = []
        cand = sorted(todo, key=lambda s: (s[3], s[1], s[2]))
        for s in cand:
            p, r, c, ring, under, other = s
            via = have_via.get((fp.ref, p.num))
            if L == surface and via is not None:
                nxt.append(s)                       # it went down: its escape is on an inner layer
                continue
            if L != surface and via is None:
                nxt.append(s)
                continue
            prof = prof_of(p.net)
            lt, lv = B.legal(p.net, prof)
            if via is not None:
                j, i = B.cell(*via)
                src = [(li, j * B.nx + i)]
            else:
                src = [c_ for c_ in R.pad_cells(B, dump_pad[(fp.ref, p.num)], prof) if c_[0] == li]
            if not src or not all(lt[l_][ix] for l_, ix in src[:1]):
                nxt.append(s)
                continue
            # the side: the nearest edge, unless the rest of the net lies another way and that edge is not much farther
            # (escaping away from where the net goes sends the route back through the ball field)
            ox, oy = cen([q["pos"] for q in other])
            dist_ = {"N": r, "S": G.R - 1 - r, "W": c, "E": G.C - 1 - c}
            beyond = {"N": G.Y0 - oy, "S": oy - G.Y1, "W": G.X0 - ox, "E": ox - G.X1}      # how far past that edge it lies
            facing = {sd for sd, v in beyond.items() if v > 0}
            if L == surface:                        # the outer rings straight out: across a neighbour's way it blocks it
                sides = sorted(dist_, key=lambda sd: (dist_[sd], 0 if sd in facing else 1, -beyond[sd]))
            else:
                sides = sorted(dist_, key=lambda sd: (dist_[sd] + (0 if sd in facing else 3), -beyond[sd]))
            rec = None
            ok_ = [x for x in sides if chan_of(x, other) is None or count[(chan_of(x, other), L)] < cap[chan_of(x, other)]]
            for sd in ok_[:2]:
                tg = _cells_in(B, G.band(sd), li, lt)
                if not tg:
                    continue
                rec = Re.connect(p.net, prof, src, tg, allow_vias=False, layers=[L], margin=1.2)
                if rec is not None:
                    break
            if rec is None:
                nxt.append(s)
                continue
            segs = rec["segments"]
            end = segs[-1][2] if segs else (via or (p.x, p.y))
            done.append({"net": p.net, "ref": fp.ref, "pad": p.num, "layer": L, "at": [round(end[0], 4), round(end[1], 4)],
                         "via": [round(via[0], 4), round(via[1], 4)] if via else None, "side": sd,
                         "segments": [[l_, [round(a[0], 4), round(a[1], 4)], [round(b[0], 4), round(b[1], 4)], round(w_, 4)]
                                      for l_, a, b, w_ in segs]})
            by_layer[L] += 1
            if chan_of(sd, other):
                count[(chan_of(sd, other), L)] += 1
        todo = nxt
        if L == surface:                            # outer balls the surface's channels had no room for: a via, then below
            for s in todo:
                p = s[0]
                if (fp.ref, p.num) not in have_via:
                    opts = spots(p, outward(p))
                    if opts:
                        lay(p, opts[0])
        if L == surface:                            # balls with no via spot round them: a longer stub to the nearest one
            for s in todo + [s_ for s_ in sig if s_[4]]:
                p = s[0]
                if (fp.ref, p.num) in have_via:
                    continue
                prof = prof_of(p.net)
                lt, lv = B.legal(p.net, prof)
                src = [c_ for c_ in R.pad_cells(B, dump_pad[(fp.ref, p.num)], prof) if c_[0] == li_s]
                reach = 2.5 * P
                m = B._rect(p.x - reach, p.y - reach, p.x + reach, p.y + reach).ravel() & (lv == 1) & (lt[li_s] == 1)
                m &= np.hypot(B.GX.ravel() - p.x, B.GY.ravel() - p.y) >= G.g["pad"] / 2 + vd / 2 + 0.05
                tg = [(li_s, int(i)) for i in m.nonzero()[0]]
                if not src or not tg:
                    continue
                rec = Re.connect(p.net, prof, src, tg, allow_vias=False, layers=[surface], margin=0.6)
                if rec is None:
                    continue
                end = rec["segments"][-1][2] if rec["segments"] else (p.x, p.y)
                end = (round(end[0], 4), round(end[1], 4))
                for l_, a, b_, w_ in rec["segments"]:
                    stubs.append({"net": p.net, "layer": l_, "a": [round(a[0], 4), round(a[1], 4)],
                                  "b": [round(b_[0], 4), round(b_[1], 4)], "w": w_})
                B.stamp_via(p.net, end, vd, drill=rules["via_drill"])
                taken[(round(end[0], 2), round(end[1], 2))] = p.net
                vias.append({"net": p.net, "x": end[0], "y": end[1], "d": vd, "drill": rules["via_drill"]})
                have_via[(fp.ref, p.num)] = end
                long_bones.append(p.num)
    for p, r, c, ring, under, other in todo + [s_ for s_ in sig if s_[4] and (fp.ref, s_[0].num) not in have_via]:
        left.append({"net": p.net, "pad": p.num, "ring": ring,
                     "why": "no via spot" if (ring >= top or under) and (fp.ref, p.num) not in have_via else "no room on any layer"})
    return {"ref": fp.ref, "kind": "array", "pitch": P, "rows": G.R, "cols": G.C, "surface": surface,
            "field": [round(v, 3) for v in G.field(0.0)],
            "signals": len(sig), "under": sum(1 for s in sig if s[4]), "supply_balls": len(sup),
            "vias": len(vias), "long_dogbones": len(long_bones), "escaped": len(done), "by_layer": dict(by_layer), "left": left,
            "stubs": stubs, "new_vias": vias, "escapes": done, "neck_rules": rules}


def _perimeter_part(g, fp, kinds, rules, pads_by_net, planes, pitch_max=0.65):
    """A fine-pitch part whose pads' nets change layer next to it: a stub and a via for each, in two staggered rows."""
    B = g.B
    pads = [p for p in fp.pads if p.kind == "smd" and p.net and not p.net.startswith("unconnected-")
            and sum(1 for l in p.layers if l.endswith(".Cu")) == 1 and g._net_ok(p.net)]
    if len(pads) < 8:
        return None
    pitch = min((geom.dist((a.x, a.y), (b.x, b.y)) for i, a in enumerate(pads) for b in pads[i + 1:]), default=9)
    if pitch > pitch_max or escape_grid(fp):
        return None
    layer = next(l for l in pads[0].layers if l.endswith(".Cu"))
    if layer not in B.layers:
        return None
    li = B.layers.index(layer)
    pour_on = {n for n, ls in planes.items() if layer in ls}
    need = []
    for p in pads:
        if p.net in pour_on:
            continue
        other = _others(pads_by_net, p.net, fp.ref)
        same = [q for q in other if layer in q["layers"] and q["drill"] == 0]
        thru = [q for q in other if q["drill"] > 0]
        if p.net in planes or (other and not same and not thru):
            need.append(p)
    if not need:
        return None
    xs = [p.x for p in pads]; ys = [p.y for p in pads]
    bx = (min(xs), min(ys), max(xs), max(ys))
    vd, vr, s, w = rules["via_d"], rules["via_d"] / 2, rules["s_class"], rules["w"]
    rows = collections.defaultdict(list)
    for p in need:
        d = {(-1, 0): p.x - bx[0], (1, 0): bx[2] - p.x, (0, -1): p.y - bx[1], (0, 1): bx[3] - p.y}
        n = min(d, key=d.get)
        rows[n].append(p)
    stubs, vias, made, left = [], [], [], []
    taken = {(round(v["pos"][0], 2), round(v["pos"][1], 2)) for v in g.dump["vias"]}
    others = [(q.x, q.y) for f2 in g.b.fp_list if f2.ref != fp.ref for q in f2.pads]

    def crowd(ps, nn, reach=2.5):
        """Other parts' pads (any side) in the strip a row of vias would take on that side of the row."""
        along = (abs(nn[1]), abs(nn[0]))
        lo = min(p.x * along[0] + p.y * along[1] for p in ps) - 1
        hi = max(p.x * along[0] + p.y * along[1] for p in ps) + 1
        base = sum(p.x * nn[0] + p.y * nn[1] for p in ps) / len(ps)
        return sum(1 for x, y in others if lo <= x * along[0] + y * along[1] <= hi and 0.2 < (x * nn[0] + y * nn[1]) - base <= reach)
    for n, ps in rows.items():
        along = (abs(n[1]), abs(n[0]))                      # the row's direction
        ps.sort(key=lambda p: p.x * along[0] + p.y * along[1])
        if crowd(ps, (-n[0], -n[1])) < crowd(ps, n):        # through vias keep off the parts on the other side too: the
            n = (-n[0], -n[1])                              # emptier side of the row (between two rows, under the body)
        for k, p in enumerate(ps):
            half = max((abs((q[0] - p.x) * n[0] + (q[1] - p.y) * n[1]) for q in (p.poly or [])), default=max(p.w, p.h) / 2)
            d1 = half + max(s + vr, getattr(B, "hole_cl", 0.0) + rules["via_drill"] / 2) + 0.02     # hole clearance too
            space = max(vd + s, rules["via_drill"] + getattr(B, "h2h", 0.25))        # between two vias' centres
            d2 = d1 + max(0.45, vd, math.sqrt(max(0.0, space ** 2 - pitch ** 2)) + 0.02)
            prof = g.net_class.get(p.net, "Default")
            lt, lv = B.legal(p.net, prof)
            ok = None
            # outward first, in its row of the stagger, then farther out; then the other way (between two rows of pads,
            # under the part's body) -- a through via must keep off the parts on the other side as well
            near = (d1, d2) if k % 2 == 0 else (d2, d1)
            tries = [(n, d) for d in near + (near[0] + 0.9, near[1] + 0.9, near[0] + 1.8)]
            tries += [((-n[0], -n[1]), d) for d in near]
            for (nn, d) in tries:
                x, y = round(p.x + nn[0] * d, 4), round(p.y + nn[1] * d, 4)
                if (round(x, 2), round(y, 2)) in taken or not _via_ok(B, lv, x, y) or not _seg_ok(B, lt, li, (p.x, p.y), (x, y)):
                    continue
                ok = (x, y)
                break
            if not ok:
                left.append({"net": p.net, "pad": p.num, "why": "no room for its via"})
                continue
            x, y = ok
            B.stamp_track(p.net, layer, (p.x, p.y), (x, y), w)
            B.stamp_via(p.net, (x, y), vd, drill=rules["via_drill"])
            taken.add((round(x, 2), round(y, 2)))
            stubs.append({"net": p.net, "layer": layer, "a": [round(p.x, 4), round(p.y, 4)], "b": [x, y], "w": w})
            vias.append({"net": p.net, "x": x, "y": y, "d": vd, "drill": rules["via_drill"]})
            made.append({"net": p.net, "ref": fp.ref, "pad": p.num, "layer": None, "at": [x, y], "via": [x, y], "side": None,
                         "segments": []})
    return {"ref": fp.ref, "kind": "fine-pitch", "pitch": round(pitch, 3), "surface": layer, "pads": len(need),
            "vias": len(vias), "escaped": len(made), "by_layer": {}, "left": left, "stubs": stubs, "new_vias": vias,
            "escapes": [], "fields": made}


def escape_grid(fp):
    from . import escape
    return escape.grid(fp)


def run(project, refs=None, apply=True, log=print, apply_fn=None):
    """Break out every dense part (or the named ones) and write the copper to the board. Returns the report; the ports go
    to build/breakout.json for the router. apply_fn(ops) -> the apply's result: how the ops go onto the board (the app
    passes its own, one undo step); default tw.pcb.client.apply."""
    from . import escape
    from .board import Board
    from .route.driver import GridRoute
    from .pcb import client
    b = Board.load(project.pcb)
    cfg = getattr(project, "cfg", None) or {}
    g = GridRoute(project, log=lambda m: None).setup()
    kinds = escape._kinds(b, cfg)
    rules = escape._rules(project, b)
    routing = list(g.routing)
    pads_by_net = collections.defaultdict(list)
    for p in g.dump["pads"]:
        if p["net"]:
            pads_by_net[p["net"]].append(p)
    planes = {}
    for z in b.zones:
        if z.net and not z.is_rule_area:
            planes.setdefault(z.net, set()).update(z.layers)
    parts = []
    arrays = [fp for fp in b.fp_list if (not refs or fp.ref in refs) and escape.grid(fp)]
    for fp in sorted(arrays, key=lambda f: -len(f.pads)):
        r = _area_part(g, fp, kinds, rules, routing, pads_by_net, planes, log)
        if r:
            parts.append(r)
            log(f"{fp.ref}: {r.get('escaped', 0)} of {r.get('signals', 0) - r.get('under', 0)} signal balls escaped "
                f"({', '.join(f'{k} {v}' for k, v in r.get('by_layer', {}).items())}), {r.get('vias', 0)} vias, "
                f"{len(r.get('left', []))} left")
    for fp in b.fp_list:
        if refs and fp.ref not in refs or any(x["ref"] == fp.ref for x in parts):
            continue
        r = _perimeter_part(g, fp, kinds, rules, pads_by_net, planes)
        if r:
            parts.append(r)
            log(f"{fp.ref}: {r['vias']} vias beside {r['pads']} pads that change layer ({len(r['left'])} without room)")
    ops = []
    pri = collections.Counter()                      # the plane patches' priorities, distinct where they overlap
    for r in parts:
        if r.get("stubs") or r.get("new_vias") or r.get("escapes"):
            ops += escape.area_ops(project, b, r["ref"], rules, taken=pri)
    stubs = [t for r in parts for t in r.get("stubs", [])]
    tracks = stubs + [{"net": e["net"], "layer": l_, "a": a, "b": b_, "w": w_} for r in parts for e in r.get("escapes", [])
                      for l_, a, b_, w_ in e["segments"]]
    vias = [v for r in parts for v in r.get("new_vias", [])]
    if tracks:
        ops.append({"op": "tracks", "items": tracks})
    if vias:
        ops.append({"op": "vias", "items": vias})
    rep = {"parts": [{k: v for k, v in r.items() if k not in ("stubs", "new_vias", "neck_rules")} for r in parts],
           "tracks": len(tracks), "vias": len(vias)}
    if apply and ops:
        if project.pro:
            from .pro import set_board_rules
            set_board_rules(project.pro, {"min_track_width": rules["w"], "min_clearance": rules["s"],
                                          "min_via_diameter": rules["via_d"], "min_through_hole_diameter": rules["via_drill"]},
                            lower_only=True)
        if any(z.net and not z.is_rule_area for z in b.zones):
            ops.append({"op": "fill"})
        res = (apply_fn or (lambda o: client.apply(project, o, live="auto")))(ops)
        rep["applied"] = bool(res.get("ok"))
        if not res.get("ok"):
            rep["error"] = "; ".join(str(x.get("error", "")) for x in res.get("results", []) if not x.get("ok")) or res.get("error")
        ids = collections.defaultdict(list)            # each escape's tracks by id: the prune takes exactly these off
        for c in res.get("changes") or []:
            if c.get("kind") == "track" and c.get("uuid"):
                ids[(c["net"], c["layer"], tuple(round(v, 3) for v in c["a"]), tuple(round(v, 3) for v in c["b"]))].append(c["uuid"])
        for r in rep["parts"]:
            for e in r.get("escapes", []):
                e["uuids"] = [u for l_, a, b_, w_ in e["segments"]
                              for u in ids.get((e["net"], l_, tuple(round(v, 3) for v in a), tuple(round(v, 3) for v in b_)), [])[:1]]
    os.makedirs(project.build, exist_ok=True)
    with open(_path(project), "w") as f:
        json.dump(rep, f, indent=1)
    return rep


def prune(project, keep_failed=()):
    """Take off the escapes the router did not use: an escape track whose port end nothing else of its net reaches
    (the net was joined at its via on another layer, or not at all and is kept for a later route when listed in
    keep_failed). Returns the number of tracks taken off."""
    from .board import Board
    from .pcb import client
    d = ports(project)
    if not d:
        return 0
    b = Board.load(project.pcb)
    by = collections.defaultdict(list)
    for t in b.tracks:
        by[(t.net, t.layer)].append(t)
    gone = []
    keep = {n.rsplit("/", 1)[-1] for n in keep_failed}
    for net, es in d.items():
        if net.rsplit("/", 1)[-1] in keep:
            continue
        for e in es:
            if not e.get("segments"):
                continue
            mine = set(e.get("uuids") or [])
            if not mine:                                 # an older breakout without ids: its tracks by their ends
                segs = [(tuple(a), tuple(b_)) for _, a, b_, _ in e["segments"]]
                for t in by[(net, e["layer"])]:
                    if any(geom.dist(t.a, a) < 0.01 and geom.dist(t.b, b_) < 0.01 or geom.dist(t.a, b_) < 0.01 and geom.dist(t.b, a) < 0.01
                           for a, b_ in segs):
                        mine.add(t.uuid)
            port = tuple(e["at"])
            reached = any(t.uuid not in mine and (geom.dist(t.a, port) < 0.02 or geom.dist(t.b, port) < 0.02
                                                  or geom.seg_point_dist(port, t.a, t.b) < 0.02)
                          for t in by[(net, e["layer"])])
            reached = reached or any(v.net == net and geom.dist((v.x, v.y), port) < v.d / 2 for v in b.vias)   # down at once
            if not reached:
                gone += sorted(mine)
    if gone:
        client.apply(project, [{"op": "delete", "uuids": gone, "kinds": ["track"]}], live="auto")
    return len(gone)


# ----------------------------------------------------------------------------- room for the vias
def make_room(project, refs=None, apply=True, reach=0.8, log=print, apply_fn=None):
    """Parts on the far side of a ball-grid part (the capacitors under a BGA), moved a little and turned so that as many
    of its signal balls as can get a via spot do: a capacitor whose pads sit on the four spots round a ball leaves that
    ball nowhere to go. Each part moves at most `reach` mm (two-pad parts may also turn a quarter), stays clear of the
    other parts on its side, and moves only when the balls gain by it. Returns {"moves": [ops], "parts": [{ref,
    stuck_before, stuck_after}]}; with apply, the moves go onto the board."""
    from . import escape
    from .board import Board
    from .pcb import client
    b = Board.load(project.pcb)
    cfg = getattr(project, "cfg", None) or {}
    kinds = escape._kinds(b, cfg)
    rules = escape._rules(project, b)
    clr = rules["via_d"] / 2 + rules["s_class"]
    planes = {z.net for z in b.zones if z.net and not z.is_rule_area}
    by_net = collections.defaultdict(list)
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net:
                by_net[p.net].append(fp.ref)
    moves, report = [], []
    for U in b.fp_list:
        if refs and U.ref not in refs:
            continue
        gr = escape.grid(U)
        if not gr:
            continue
        G = _Grid(U, gr)
        P = G.P
        plan = escape.plan_part(U, gr, kinds, rules, 1, {"on": False, "via_in_pad": False, "microvias": False,
                                                        "micro_d": 0.25, "micro_drill": 0.1})
        top = plan["top_rings"]
        need = {}
        for p, r, c, ring in gr["balls"]:
            n = p.net
            if not n or n.startswith("unconnected-") or n in planes:
                continue
            if ring < top or len([x for x in by_net[n] if x != U.ref]) == 0:
                continue
            need[p.num] = [(round(p.x + sx * P / 2, 4), round(p.y + sy * P / 2, 4)) for sx in (-1, 1) for sy in (-1, 1)]
        if not need:
            continue
        fx0, fy0, fx1, fy1 = G.field(P)
        far = [f for f in b.fp_list if f.side != U.side and f.ref != U.ref and f.pads
               and any(fx0 <= q.x <= fx1 and fy0 <= q.y <= fy1 for q in f.pads)]
        if not far:
            continue
        same_side = [f for f in b.fp_list if f.side != U.side and f.ref != U.ref]

        def shapes(f, dx=0.0, dy=0.0, da=0):
            out = []
            for q in f.pads:
                pls = q.polys or [q.poly]
                for pl in pls:
                    pts = []
                    for x, y in pl:
                        vx, vy = geom.rot(x - f.x, y - f.y, da)
                        pts.append((f.x + vx + dx, f.y + vy + dy))
                    out.append(pts)
            return out

        def box(pls, m=0.15):
            bx = geom.bbox([q for pl in pls for q in pl])
            return (bx[0] - m, bx[1] - m, bx[2] + m, bx[3] + m)

        cy_now = {}                                    # courtyards of parts moved here, where they now are

        def court(f, dx=0.0, dy=0.0, da=0):
            """The part's courtyard box where it would be (its pads' box and a margin when it has no courtyard)."""
            loops = cy_now.get(f.ref) or f.courtyard()
            if not loops:
                return box(shapes(f, dx, dy, da))
            pts = []
            for lp in loops:
                for x, y in lp:
                    vx, vy = geom.rot(x - f.x, y - f.y, da)
                    pts.append((f.x + vx + dx, f.y + vy + dy))
            return geom.bbox(pts)

        cur = {f.ref: shapes(f) for f in far}
        boxes = {f.ref: court(f) for f in same_side}
        spots = sorted({s for ss in need.values() for s in ss})

        def blocked_by(pls):
            bb = box(pls, clr + 0.05)
            out = set()
            for s in spots:
                if bb[0] <= s[0] <= bb[2] and bb[1] <= s[1] <= bb[3] and any(geom.poly_dist(s, pl) < clr for pl in pls):
                    out.add(s)
            return out
        blk = {ref: blocked_by(pls) for ref, pls in cur.items()}

        def matched(blk):
            gone = set().union(*blk.values()) if blk else set()
            cand = {num: [s for s in ss if s not in gone] for num, ss in need.items()}
            match = {}

            def assign(num, seen):
                for k in cand[num]:
                    if k in seen:
                        continue
                    seen.add(k)
                    if k not in match or assign(match[k], seen):
                        match[k] = num
                        return True
                return False
            return sum(1 for num in sorted(cand, key=lambda n_: len(cand[n_])) if assign(num, set()))
        base = matched(blk)
        start = base
        for _ in range(3):
            better = False
            for f in sorted(far, key=lambda f: -len(blk[f.ref])):
                if not blk[f.ref]:
                    continue
                turns = (0, 90, 180, 270) if len(f.pads) == 2 else (0, 180)
                best = None
                steps = [round(k * 0.1, 2) for k in range(-int(reach * 10), int(reach * 10) + 1)]
                others = [bx for r_, bx in boxes.items() if r_ != f.ref]
                for da in turns:
                    for dx in steps:
                        for dy in steps:
                            if not (dx or dy or da):
                                continue
                            pls = shapes(f, dx, dy, da)
                            bb = court(f, dx, dy, da)
                            if any(bb[0] < o[2] - 1e-6 and o[0] < bb[2] - 1e-6 and bb[1] < o[3] - 1e-6 and o[1] < bb[3] - 1e-6
                                   for o in others):
                                continue
                            nb = blocked_by(pls)
                            if len(nb) >= len(blk[f.ref]) and best is not None:
                                continue
                            trial = dict(blk)
                            trial[f.ref] = nb
                            m = matched(trial)
                            key = (m, -len(nb), -math.hypot(dx, dy) - (0.3 if da else 0))
                            if m >= base and (best is None or key > best[0]):
                                best = (key, dx, dy, da, nb, pls)
                if best and (best[0][0] > base or len(best[4]) < len(blk[f.ref])):
                    _, dx, dy, da, nb, pls = best
                    base = best[0][0]
                    blk[f.ref] = nb
                    boxes[f.ref] = court(f, dx, dy, da)
                    cy_now[f.ref] = [[(f.x + geom.rot(x - f.x, y - f.y, da)[0] + dx, f.y + geom.rot(x - f.x, y - f.y, da)[1] + dy)
                                      for x, y in lp] for lp in (cy_now.get(f.ref) or f.courtyard())]
                    nx_, ny_ = round(f.x + dx, 4), round(f.y + dy, 4)
                    rot = (f.angle + da) % 360
                    moves.append({"op": "move", "ref": f.ref, "x": nx_, "y": ny_, "rot": rot})
                    f.x, f.y, f.angle = nx_, ny_, rot
                    for q in f.pads:                     # the part's pads where they now are, for the next part's look
                        for pl in (q.polys or [q.poly]):
                            pl[:] = [(f.x + geom.rot(x - (nx_ - dx), y - (ny_ - dy), da)[0], f.y + geom.rot(x - (nx_ - dx), y - (ny_ - dy), da)[1])
                                     for x, y in pl]
                    better = True
            if not better:
                break
        report.append({"ref": U.ref, "balls_needing_vias": len(need), "with_a_spot_before": start, "with_a_spot_after": base})
        log(f"{U.ref}: {start} -> {base} of {len(need)} balls with a via spot, {len([m for m in moves])} parts moved")
    out = {"moves": moves, "parts": report}
    if apply and moves:
        res = (apply_fn or (lambda o: client.apply(project, o, live="auto")))(moves)
        out["applied"] = bool(res.get("ok"))
    return out
