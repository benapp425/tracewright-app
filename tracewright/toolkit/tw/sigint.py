"""Signals and supplies on the board as laid out: where a net's return current flows in the plane under it (and where it
must detour round a gap), the net's impedance along its route, how its edge arrives at the far end (lossless lines in
ngspice), what it picks up from its neighbours, and how a supply rail's impedance holds up against its loads (PDN).

    return_paths(ctx, net)                      -> {"runs", "gaps", "vias", "lines"}
    route(ctx, net, receiver=None)              -> the chain driver -> receiver: segments with Z0 and delay
    reflections(ctx, net, rise_ns=1, rs=25, series=None, receiver=None) -> waveforms, overshoot, and the fix
    crosstalk(ctx, net, rise_ns=1)              -> each neighbour's coupled length, gap and estimated noise
    pdn(ctx, net, ripple=0.05, step=None)       -> |Z(f)| of the rail against its target impedance

The numbers are engineering estimates (IPC-2141 impedance, lossless lines, the 1/(1 + (D/H)^2) coupling rule, ideal
capacitor models with typical ESR and ESL); each result says what it assumed.
"""
import math, re

from . import geom
from .checks.signal import Planes, plane_nets, neighbours, coupling, z_microstrip, z_stripline, zdiff
from .checks.power import _cap_between

C_MM_NS = 299.792458         # light, mm per ns
EPS0 = 8.854e-12


def _short(n):
    return str(n or "").rsplit("/", 1)[-1]


def _full(b, net):
    full = next((n for n in b.nets if n == net or _short(n) == net), None)
    if full is None:
        raise ValueError(f"no net {net} on the board")
    return full


# ------------------------------------------------------------------ the stack-up as the field sees it
def _rows(b):
    """[(name, kind, thickness mm, er)] copper and dielectric rows top to bottom (a 1.6 mm two-layer board if the file
    has no stack-up)."""
    rows = [(l["name"], l["type"], float(l.get("thickness") or 0), float(l.get("epsilon_r") or 0)) for l in b.stackup
            if l.get("type") in ("copper", "core", "prepreg")]
    if {r[0] for r in rows if r[1] == "copper"} != set(b.copper):     # no build in the file: an even one for its layers
        cu = list(b.copper) or ["F.Cu", "B.Cu"]
        tcu = [0.035 if l in ("F.Cu", "B.Cu") else 0.0152 for l in cu]
        d = max(0.1, ((b.thickness or 1.6) - sum(tcu)) / max(1, len(cu) - 1))
        rows = []
        for k, (l, t) in enumerate(zip(cu, tcu)):
            rows.append((l, "copper", t, 0))
            if k < len(cu) - 1:
                rows.append((f"dielectric {k + 1}", "core", d, 4.4))
    return rows


def _between(b, la, lb):
    """(dielectric mm, mean er) between two copper layers."""
    rows = _rows(b)
    names = [r[0] for r in rows]
    if la not in names or lb not in names:
        return None, None
    i, j = sorted((names.index(la), names.index(lb)))
    d = [r for r in rows[i + 1:j] if r[1] != "copper"]
    h = sum(r[2] for r in d)
    cu = sum(r[2] for r in rows[i + 1:j] if r[1] == "copper")
    er = sum(r[2] * (r[3] or 4.4) for r in d) / h if h else 4.4
    return h + cu, er


def _cu_t(b, layer):
    return next((r[2] for r in _rows(b) if r[0] == layer), 0.035) or 0.035


class Field:
    """What each point of a signal layer sits over: the reference pours of the layers either side."""

    def __init__(self, ctx):
        self.ctx, self.b = ctx, ctx.board
        self.refnets = plane_nets(ctx, self.b)
        self.planes = Planes(self.b, self.refnets)

    def refs_at(self, layer, x, y):
        """[(ref layer, net, dielectric mm, er)] of the reference pours above and below (x, y), nearest first."""
        out = []
        for l in neighbours(self.b, layer):
            n = self.planes.net_at(l, x, y) if self.planes.has(l) else None
            if n:
                h, er = _between(self.b, layer, l)
                if h:
                    out.append((l, n, h, er))
        if not out and len(self.b.copper) == 2:
            return out
        return sorted(out, key=lambda r: r[2])

    def z0(self, layer, x, y, w, gap=None):
        """(Z0 or Zdiff ohms, eps_eff, how) for a track of width w at (x, y); gap: a pair's spacing (edge to edge)."""
        refs = self.refs_at(layer, x, y)
        t = _cu_t(self.b, layer)
        if not refs:
            return None, None, "no reference plane under it"
        l, n, h, er = refs[0]
        if len(refs) >= 2:                                   # between two planes: stripline
            b2 = refs[0][2] + refs[1][2] + t
            er2 = (refs[0][3] + refs[1][3]) / 2
            z = zdiff(w, gap, h, t, er2, strip=True, b2=b2) if gap else z_stripline(w, b2, t, er2)
            return z, er2, f"stripline between {refs[0][0]} and {refs[1][0]}"
        z = zdiff(w, gap, h, t, er) if gap else z_microstrip(w, h, t, er)
        eeff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 * h / w)
        return z, eeff, f"microstrip over {_short(n)} on {l}, {h:.3f} mm below"


# ------------------------------------------------------------------ return paths
def _detour(r, a, b, limit=2.5e5):
    """The shortest way through the plane's cells (8-connected) from a to b: [(x, y)] or None."""
    import heapq
    ja, ia = r.cell(*a)
    jb, ib = r.cell(*b)
    if not r.at_cell(ja, ia) or not r.at_cell(jb, ib):
        return None
    dist = {(ja, ia): 0.0}
    prev = {}
    heap = [(0.0, ja, ia)]
    n = 0
    while heap:
        d, j, i = heapq.heappop(heap)
        if (j, i) == (jb, ib):
            out = [(j, i)]
            while out[-1] in prev:
                out.append(prev[out[-1]])
            return [r.xy(j_, i_) for j_, i_ in reversed(out)]
        if d > dist.get((j, i), 1e18):
            continue
        n += 1
        if n > limit:
            return None
        for dj, di, c in ((0, 1, 1), (1, 0, 1), (0, -1, 1), (-1, 0, 1), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)):
            q = (j + dj, i + di)
            if not r.at_cell(*q):
                continue
            nd = d + c
            if nd < dist.get(q, 1e18):
                dist[q] = nd
                prev[q] = (j, i)
                heapq.heappush(heap, (nd, q[0], q[1]))
    return None


class _PlaneGrid:
    """One reference net's pour on one layer as a boolean grid."""

    def __init__(self, b, layer, net, res=0.2):
        import numpy as np
        from .fields import Raster
        polys = [pl for z in b.zones if not z.is_rule_area and z.net == net for pl in (z.fills or {}).get(layer, []) if len(pl) >= 3]
        box = geom.bbox_union([geom.bbox(pl) for pl in polys]) if polys else (0, 0, 1, 1)
        self.R = Raster((box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1), res)
        self.m = self.R.empty()
        for pl in polys:
            self.R.poly(self.m, pl)
        self.np = np

    def cell(self, x, y):
        return self.R.cell_of(x, y)

    def at_cell(self, j, i):
        return 0 <= j < self.R.ny and 0 <= i < self.R.nx and bool(self.m[j, i])

    def at(self, x, y):
        j, i = self.cell(x, y)
        return self.at_cell(j, i) and abs(self.R.xs[i] - x) <= self.R.c and abs(self.R.ys[j] - y) <= self.R.c

    def xy(self, j, i):
        return (round(float(self.R.xs[i]), 3), round(float(self.R.ys[j]), 3))


def return_paths(ctx, net, step=0.2):
    """Where the net's return current runs: under each track in its reference pour; at a gap, round it (the detour and
    the loop it opens); at each layer change, through the nearest via of the reference net."""
    b = ctx.board
    net = _full(b, net)
    F = Field(ctx)
    tracks = [t for t in b.tracks if t.net == net]
    if not tracks:
        raise ValueError(f"{_short(net)} has no tracks")
    runs, gaps = [], []
    grids = {}
    for t in tracks:
        pts = geom.arc_points(t.a, t.mid, t.b) if t.mid else [t.a, t.b]
        for a, c in zip(pts, pts[1:]):
            L = geom.dist(a, c)
            n = max(2, int(L / step) + 1)
            samples = [(a[0] + (c[0] - a[0]) * k / (n - 1), a[1] + (c[1] - a[1]) * k / (n - 1)) for k in range(n)]
            refs = [F.refs_at(t.layer, x, y) for x, y in samples]
            cur, start = None, 0
            for k in range(n + 1):
                key = (refs[k][0][0], refs[k][0][1]) if k < n and refs[k] else None
                if k < n and key == cur:
                    continue
                if k > start:
                    seg = samples[start:k]
                    if cur:
                        h = next(r[2] for r in refs[start] if (r[0], r[1]) == cur)
                        runs.append({"layer": t.layer, "ref_layer": cur[0], "ref_net": _short(cur[1]), "h": round(h, 3),
                                     "a": [round(v, 3) for v in seg[0]], "b": [round(v, 3) for v in seg[-1]], "w": t.w})
                    else:
                        gaps.append({"layer": t.layer, "a": seg[0], "b": seg[-1], "length": round(geom.dist(seg[0], seg[-1]), 2),
                                     "before": refs[start - 1][0] if start > 0 and refs[start - 1] else None,
                                     "after": refs[k][0] if k < n and refs[k] else None,
                                     "from": samples[start - 1] if start > 0 else None, "to": samples[k] if k < n else None})
                cur, start = key, k
    # each gap: the detour through the reference pour, and the loop it opens
    out_gaps = []
    for g in gaps:
        if g["length"] < 0.3:
            continue
        ref = g["before"] or g["after"]
        e = {"layer": g["layer"], "at": [round(v, 2) for v in g["a"]], "to": [round(v, 2) for v in g["b"]], "length": g["length"]}
        if not ref or not (g["before"] and g["after"]):
            e["text"] = f"{_short(net)} on {g['layer']} runs {g['length']:.1f} mm with no reference pour under it"
            out_gaps.append(e)
            continue
        if g["before"][1] != g["after"][1]:
            e["text"] = (f"{_short(net)} on {g['layer']} passes from the {_short(g['before'][1])} pour to the {_short(g['after'][1])} "
                         f"pour: the return crosses through a capacitor (or nowhere) there")
            out_gaps.append(e)
            continue
        key = (ref[0], ref[1])
        if key not in grids:
            grids[key] = _PlaneGrid(b, ref[0], ref[1])
        G = grids[key]
        a, c = g["from"], g["to"]                    # the last solid point before the gap, the first after it
        path = _detour(G, a, c)
        if path:
            extra = sum(geom.dist(p, q) for p, q in zip(path, path[1:])) - g["length"]
            loop = abs(geom.area([tuple(a), tuple(c)] + [tuple(p) for p in reversed(path)]))
            e.update(detour=[list(p) for p in path], extra=round(max(0.0, extra), 2), loop=round(loop, 1),
                     text=f"{_short(net)} on {g['layer']} crosses a {g['length']:.1f} mm gap in {_short(ref[1])} on {ref[0]}: "
                          f"the return goes {max(0.0, extra):.1f} mm round it, opening a {loop:.0f} mm² loop")
        else:
            e["text"] = f"{_short(net)} on {g['layer']} crosses a {g['length']:.1f} mm gap in {_short(ref[1])}: no way round it in the pour"
        out_gaps.append(e)
    # layer changes: the return jumps planes through the nearest via of the reference net
    vias = []
    refvias = [(v.x, v.y, v.net) for v in b.vias if v.net in F.refnets] + \
              [(p.x, p.y, p.net) for p in b.pads() if p.net in F.refnets and p.drill and p.kind == "thru_hole"]
    for v in b.vias:
        if v.net != net:
            continue
        span = v.span(b.copper)
        here = [r for r in runs if r["a"] == [round(v.x, 3), round(v.y, 3)] or r["b"] == [round(v.x, 3), round(v.y, 3)]]
        layers = sorted({r["layer"] for r in here}) or [span[0], span[-1]]
        refs = sorted({(r["ref_layer"], r["ref_net"]) for r in here})
        best = min(((math.hypot(v.x - x, v.y - y), n) for x, y, n in refvias), default=(None, None))
        e = {"x": round(v.x, 3), "y": round(v.y, 3), "layers": layers, "refs": [list(r) for r in refs]}
        if len({n for _, n in refs}) > 1:
            e["text"] = (f"via at ({v.x:.1f}, {v.y:.1f}): the reference changes from {refs[0][1]} to {refs[-1][1]}; the return "
                         f"crosses through the nearest capacitor between them")
        elif best[0] is None:
            e["text"] = f"via at ({v.x:.1f}, {v.y:.1f}): no via of the reference net anywhere: the return finds its own way"
        else:
            e["return_via"] = round(best[0], 2)
            e["text"] = f"via at ({v.x:.1f}, {v.y:.1f}): the return jumps planes through a {_short(best[1])} via {best[0]:.1f} mm away"
        vias.append(e)
    lines = []
    under = {}
    for r in runs:
        under.setdefault((r["layer"], r["ref_layer"], r["ref_net"]), 0.0)
        under[(r["layer"], r["ref_layer"], r["ref_net"])] += geom.dist(r["a"], r["b"])
    for (l, rl, rn), L in sorted(under.items(), key=lambda kv: -kv[1]):
        lines.append(f"{L:.1f} mm on {l} over {rn} on {rl}")
    lines += [g["text"] for g in out_gaps]
    lines += [v["text"] for v in vias if v.get("return_via") is None or v["return_via"] > 2.0]
    if not out_gaps and not [v for v in vias if v.get("return_via") is None or v["return_via"] > 2.0]:
        lines.append("the return current has solid copper under the whole route")
    return {"net": _short(net), "runs": runs, "gaps": out_gaps, "vias": vias, "lines": lines}


# ------------------------------------------------------------------ the route, its impedance, its reflections
def _ends(ctx, net):
    """(driver (ref, pin), [receivers]) from the pin types: an output drives; else an IC drives a connector."""
    b, nl = ctx.board, ctx.netlist
    pads = [(fp.ref, str(p.num)) for fp in b.fp_list for p in fp.pads if p.net == net]
    def typ(rp):
        try:
            return nl.pin_type(*rp) if nl is not None else None
        except Exception:
            return None
    drv = next((rp for rp in pads if typ(rp) in ("output", "power_out", "tri_state")), None)
    if drv is None:
        drv = next((rp for rp in pads if rp[0][:1] == "U" and typ(rp) == "bidirectional"), None)
    if drv is None:
        drv = next((rp for rp in pads if rp[0][:1] in ("U", "Y", "X")), pads[0] if pads else None)
    others = [rp for rp in pads if rp != drv and (drv is None or rp[0] != drv[0])]     # another part (not the same connector)
    rx = [rp for rp in others if rp[0][:1] not in ("C", "TP")] or others or [rp for rp in pads if rp != drv]
    return drv, rx


def route(ctx, net, receiver=None, driver=None):
    """The copper from the driver to one receiver as a chain of pieces: [{"kind": "track", layer, length, w, z0, eeff,
    td_ps, how} | {"kind": "via"}], with the ends."""
    from .copper import NetGraph
    b = ctx.board
    net = _full(b, net)
    drv, rxs = _ends(ctx, net)
    if driver:
        r, _, p = str(driver).partition(".")
        drv = (r, p)
    if receiver:
        r, _, p = str(receiver).partition(".")
        rxs = [(r, p)]
    if not drv or not rxs:
        raise ValueError(f"{_short(net)}: needs a driver and a receiver")
    g = NetGraph(b, net)
    best = None
    for rx in rxs:
        w, path = g.widest_path(drv, rx)
        if path is not None:
            L = sum(geom.dist(i[2], i[3]) for i in path if i[0] == "track")
            if best is None or L > best[0]:
                best = (L, rx, path)
    if best is None:
        raise ValueError(f"{_short(net)} is not routed from {drv[0]} to a receiver")
    F = Field(ctx)
    partner = _partner(b, net)
    pieces = []
    for info in best[2]:
        if info[0] == "track":
            _, layer, a, c, w = info
            mid = ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2)
            gap = _pair_gap(b, partner, layer, a, c) if partner else None
            z, eeff, how = F.z0(layer, mid[0], mid[1], w, gap)
            L = geom.dist(a, c)
            if L < 1e-3:
                continue
            td = L * math.sqrt(eeff or 4.0) / C_MM_NS * 1000
            pieces.append({"kind": "track", "layer": layer, "a": list(a), "b": list(c), "length": round(L, 3), "w": w,
                           "z0": round(z, 1) if z else None, "eeff": round(eeff, 3) if eeff else None, "td_ps": round(td, 2),
                           "how": how, "pair_gap": round(gap, 3) if gap else None})
        elif info[0] == "via":
            pieces.append({"kind": "via", "x": info[1], "y": info[2]})
    return {"net": _short(net), "driver": f"{drv[0]}.{drv[1]}", "receiver": f"{best[1][0]}.{best[1][1]}",
            "length": round(best[0], 2), "pieces": pieces, "pair": _short(partner) if partner else None}


def _partner(b, net):
    from .nettypes import find_pairs
    for p, n in find_pairs(b.nets):
        if net == p:
            return n
        if net == n:
            return p
    return None


def _pair_gap(b, partner, layer, a, c):
    """The edge-to-edge gap to the pair's other half running beside this piece, or None."""
    class T:
        pass
    t = T()
    t.a, t.b = a, c
    best = None
    for o in b.tracks:
        if o.net != partner or o.layer != layer or o.mid:
            continue
        cp = coupling(t, o)
        if cp and cp[0] > 0.3:
            w = o.w
            gap = cp[1] - w
            if gap > 0 and (best is None or gap < best):
                best = gap
    return best


def _e24(x):
    E = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
    if x <= 0:
        return 0.0
    d = 10 ** math.floor(math.log10(x))
    return min((e * d for e in E + [10.0]), key=lambda v: abs(v - x))


def _lines(pieces, tr_ps):
    """The route as few lines as tell the story: neighbours within 8 % of each other's impedance merged, and pieces
    much shorter than the edge folded into their neighbour (ngspice steps at the shortest delay)."""
    out = []
    for p in pieces:
        if p["kind"] == "via":
            out.append({"via": True})
            continue
        if not p.get("z0"):
            continue
        last = next((q for q in reversed(out) if not q.get("via")), None)
        joined = last is not None and out[-1] is last
        if joined and (abs(p["z0"] - last["z0"]) / last["z0"] < 0.08 or p["td_ps"] < tr_ps / 40 or last["td"] < tr_ps / 40):
            td = last["td"] + p["td_ps"]
            last["z0"] = (last["z0"] * last["td"] + p["z0"] * p["td_ps"]) / td
            last["td"] = td
        else:
            out.append({"z0": p["z0"], "td": p["td_ps"]})
    return out


def _spice(rt, rise_ns, rs, series, vdd, rx_pf=3.0, via_pf=0.3):
    tr = rise_ns * 1e-9
    total = sum(p["td_ps"] for p in rt["pieces"] if p["kind"] == "track") * 1e-12
    stop = max(12 * total + 10 * tr, 8 * tr, 2e-9)
    width = stop
    lines = [f"* {rt['net']}: {rt['driver']} drives {rt['receiver']} over {rt['length']} mm",
             f"Vdrv src 0 PULSE(0 {vdd:g} {tr:g} {tr:g} {tr:g} {width:g} {4 * width:g})",
             f"Rs src n0 {rs:g}"]
    node = "n0"
    if series:
        lines.append(f"Rser n0 ns {series:g}")
        node = "ns"
    k = 0
    for p in _lines(rt["pieces"], rise_ns * 1000):
        if p.get("via"):
            lines.append(f"Cvia{k} {node} 0 {via_pf:g}p")
            continue
        # each line as an LC ladder, every section short against the edge (ngspice's ideal line steps at its delay)
        n = max(1, min(60, math.ceil(p["td"] * 12 / (rise_ns * 1000))))
        Ls, Cs = p["z0"] * p["td"] * 1e-12 / n, p["td"] * 1e-12 / p["z0"] / n
        for _ in range(n):
            k += 1
            nxt = f"t{k}"
            lines.append(f"L{k} {node} {nxt} {Ls:.4g}")
            lines.append(f"C{k} {nxt} 0 {Cs:.4g}")
            node = nxt
    lines += [f"Crx {node} 0 {rx_pf:g}p", f"Rrx {node} 0 1Meg", f".tran {tr / 25:g} {stop:g}", ".end"]
    return "\n".join(lines) + "\n", node


def _measure(res, probe, vdd):
    v = ((res.get("vectors") or {}).get(probe) or {}).get("values") or []
    t = ((res.get("vectors") or {}).get(res.get("scale")) or {}).get("values") or []
    if not v:
        return {}
    hi = max(v)
    first = next((i for i, x in enumerate(v) if x >= 0.9 * vdd), None)
    peak = v.index(hi)
    under = min(v[peak:]) if hi > vdd * 1.005 else vdd           # ring-back: the dip after the first overshoot
    settle = None
    if first is not None:
        for i in range(len(v) - 1, first - 1, -1):
            if abs(v[i] - vdd) > 0.05 * vdd:
                settle = t[i + 1] if i + 1 < len(t) else None
                break
    return {"overshoot_pct": round(max(0.0, hi - vdd) / vdd * 100, 1),
            "undershoot_pct": round(max(0.0, vdd - under) / vdd * 100, 1) if first is not None else None,
            "settle_ns": round(settle * 1e9, 2) if settle else None, "peak_v": round(hi, 3)}


def edge(ctx, net):
    """(rise time ns, why): how fast the net's edges are, from what it is."""
    from .checks.signal import fast_nets, usb_speed
    b = ctx.board
    full = _full(b, net)
    name = _short(full).upper()
    kind = fast_nets(ctx, b).get(full, "")
    if re.search(r"(^|_)(SDA|SCL|I2C)", name):
        return 100.0, "I²C: open drain, pulled up, slow edges"
    if "usb" in kind:
        p = _partner(b, full)
        sp = usb_speed(ctx, (full, p or full))
        return (4.0, "USB full speed, 4 to 20 ns edges") if sp == "fs" else (0.5, "USB high speed")
    if "clock" in kind:
        return 1.0, "a clock"
    if kind:
        return 0.5, f"a fast line ({kind})"
    return 2.0, "an ordinary logic output"


def reflections(ctx, net, rise_ns=None, rs=25.0, series=None, receiver=None, vdd=3.3, folder=None):
    """The edge at the receiver as the lines carry it (ngspice), with and without the series resistor that would tame it.
    rise_ns: the driver's edge (None: from the kind of net)."""
    from . import sim
    rise_why = None
    if rise_ns is None:
        rise_ns, rise_why = edge(ctx, net)
    rt = route(ctx, net, receiver)
    if not [p for p in rt["pieces"] if p["kind"] == "track" and p.get("z0")]:
        raise ValueError(f"{rt['net']} has no reference plane under its route: no impedance to simulate")
    zs = [p["z0"] for p in rt["pieces"] if p["kind"] == "track" and p.get("z0")]
    zavg = sum(p["z0"] * p["length"] for p in rt["pieces"] if p.get("z0")) / max(1e-9, sum(p["length"] for p in rt["pieces"] if p.get("z0")))
    trip = sum(p["td_ps"] for p in rt["pieces"] if p["kind"] == "track")
    cir, rx = _spice(rt, rise_ns, rs, series, vdd)
    res = sim.run(cir, ["v(n0)", f"v({rx})"], points=800)
    if not res.get("ok"):
        raise ValueError("ngspice: " + str(res.get("error", ""))[:300])
    m = _measure(res, f"v({rx})", vdd)
    out = {"route": rt, "netlist": cir, "rise_ns": rise_ns, "rs": rs, "series": series, "vdd": vdd, "zavg": round(zavg, 1),
           "zmin": min(zs), "zmax": max(zs), "trip_ps": round(trip, 1), "receiver_probe": f"v({rx})", "result": m, "res": res}
    fix = None
    if not series and m.get("overshoot_pct", 0) > 10:
        r = _e24(max(0.0, zavg - rs))
        if r >= 5:
            cir2, rx2 = _spice(rt, rise_ns, rs, r, vdd)
            res2 = sim.run(cir2, ["v(n0)", f"v({rx2})"], points=800)
            if res2.get("ok"):
                fix = {"series": r, "result": _measure(res2, f"v({rx2})", vdd), "res": res2, "probe": f"v({rx2})"}
    out["fix"] = fix
    short = 2 * trip < 0.2 * rise_ns * 1000
    lines = [f"{rt['net']}: {rt['driver']} to {rt['receiver']}, {rt['length']:.1f} mm, {zavg:.0f} Ω on average "
             f"({min(zs):.0f} to {max(zs):.0f} Ω), {trip:.0f} ps one way",
             f"a {rise_ns:g} ns edge" + (f" ({rise_why})" if rise_why else "") + f" from a {rs:g} Ω driver: "
             f"{m.get('overshoot_pct', 0):.0f} % overshoot at the receiver"
             + (f", {m['undershoot_pct']:.0f} % ringing back" if m.get("undershoot_pct") else "")]
    if short:
        lines.append("electrically short (the edge is far longer than the trip): " + (
            "what rings is the track's inductance with the input's capacitance" if m.get("overshoot_pct", 0) > 5 else "the line hardly matters"))
    if fix:
        lines.append(f"a {fix['series']:g} Ω resistor in series at {rt['driver'].split('.')[0]} brings it to "
                     f"{fix['result'].get('overshoot_pct', 0):.0f} %")
    out["lines"] = lines
    out["svg"] = _wave_svg(res, f"v({rx})", vdd, fix, title=f"{rt['net']} at {rt['receiver']}")
    for k in ("res",):
        out.pop(k, None)
    if fix:
        fix.pop("res", None)
    return out


def _wave_svg(res, probe, vdd, fix=None, title=""):
    """The receiver's waveform (and the terminated one) as a small SVG."""
    from .sim import ticks, si
    from html import escape
    t = ((res.get("vectors") or {}).get(res.get("scale")) or {}).get("values") or []
    v = ((res.get("vectors") or {}).get(probe) or {}).get("values") or []
    series = [("as laid out", t, v, "#e5a54b")]
    if fix and fix.get("res"):
        r2 = fix["res"]
        series.append((f"with {fix['series']:g} Ω series", ((r2.get("vectors") or {}).get(r2.get("scale")) or {}).get("values") or [],
                       ((r2.get("vectors") or {}).get(fix["probe"]) or {}).get("values") or [], "#5fb98e"))
    W, H, L_, R_, T_, B_ = 640, 280, 56, 14, 26, 34
    xs = [x for _, tt, _, _ in series for x in tt]
    ys = [y for _, _, vv, _ in series for y in vv] + [0, vdd]
    if not xs or not ys:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    y0, y1 = y0 - 0.05 * (y1 - y0), y1 + 0.05 * (y1 - y0)
    X = lambda x: L_ + (x - x0) / (x1 - x0 or 1) * (W - L_ - R_)
    Y = lambda y: T_ + (y1 - y) / (y1 - y0 or 1) * (H - T_ - B_)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system, sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="#14161a"/>', f'<text x="{L_}" y="16" fill="#c8ccd4" font-weight="600">{escape(title)}</text>']
    for ty in ticks(y0, y1, 5, cover=False):
        out.append(f'<line x1="{L_}" x2="{W - R_}" y1="{Y(ty):.1f}" y2="{Y(ty):.1f}" stroke="#2a2e36"/>'
                   f'<text x="{L_ - 6}" y="{Y(ty) + 4:.1f}" fill="#8b8f99" text-anchor="end">{si(ty, "V")}</text>')
    for tx in ticks(x0, x1, 6, cover=False):
        out.append(f'<line x1="{X(tx):.1f}" x2="{X(tx):.1f}" y1="{T_}" y2="{H - B_}" stroke="#22252c"/>'
                   f'<text x="{X(tx):.1f}" y="{H - B_ + 16}" fill="#8b8f99" text-anchor="middle">{si(tx, "s")}</text>')
    out.append(f'<line x1="{L_}" x2="{W - R_}" y1="{Y(vdd):.1f}" y2="{Y(vdd):.1f}" stroke="#6b7080" stroke-dasharray="4 3"/>')
    for k, (name, tt, vv, col) in enumerate(series):
        if not tt:
            continue
        step = max(1, len(tt) // 700)
        pts = " ".join(f"{X(a):.1f},{Y(b_):.1f}" for a, b_ in list(zip(tt, vv))[::step])
        out.append(f'<polyline fill="none" stroke="{col}" stroke-width="1.6" points="{pts}"/>')
        out.append(f'<rect x="{W - R_ - 170}" y="{T_ + 4 + 16 * k}" width="10" height="3" fill="{col}"/>'
                   f'<text x="{W - R_ - 155}" y="{T_ + 9 + 16 * k}" fill="#c8ccd4">{escape(name)}</text>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ crosstalk
def crosstalk(ctx, net, rise_ns=1.0):
    """The nets that run beside this one: coupled length, gap, and the noise they couple, estimated with
    K / (1 + (D/H)^2) (D centre to centre, H the height over the plane; K grows with the coupled length up to the
    edge's own length on the board)."""
    b = ctx.board
    net = _full(b, net)
    F = Field(ctx)
    quiet = F.refnets
    partner = _partner(b, net)
    mine = [t for t in b.tracks if t.net == net and not t.mid]
    acc = {}
    for t in mine:
        mid = ((t.a[0] + t.b[0]) / 2, (t.a[1] + t.b[1]) / 2)
        refs = F.refs_at(t.layer, *mid)
        h = refs[0][2] if refs else 1.5
        eeff = F.z0(t.layer, mid[0], mid[1], t.w)[1] or 3.5
        for o in b.tracks:
            if o.net in (net, partner) or o.net in quiet or o.layer != t.layer or o.mid or not o.net:
                continue
            c = coupling(t, o)
            if not c:
                continue
            ov, d = c
            if d > max(5 * h, 3 * max(t.w, o.w)):
                continue
            e = acc.setdefault(o.net, {"length": 0.0, "d": d, "h": h, "eeff": eeff, "layer": t.layer, "at": mid})
            e["length"] += ov
            if d < e["d"]:
                e.update(d=d, at=mid, h=h)
    rise_mm = rise_ns * C_MM_NS / math.sqrt(3.5)
    out = []
    for o, e in acc.items():
        k = min(1.0, e["length"] / (rise_mm / 2))
        x = k / (1 + (e["d"] / e["h"]) ** 2)
        out.append({"net": _short(o), "layer": e["layer"], "length": round(e["length"], 1), "spacing": round(e["d"], 3),
                    "height": round(e["h"], 3), "noise_pct": round(100 * x, 1), "at": [round(v, 2) for v in e["at"]]})
    out.sort(key=lambda r: -r["noise_pct"])
    lines = [f"{r['net']}: {r['length']:.1f} mm beside it on {r['layer']}, {r['spacing']:.2f} mm centre to centre over a "
             f"{r['height']:.2f} mm dielectric: about {r['noise_pct']:.0f} % of its swing" for r in out[:8]]
    if not out:
        lines = [f"no signal runs beside {_short(net)} (within five dielectric heights)"]
    return {"net": _short(net), "rise_ns": rise_ns, "neighbours": out, "lines": lines}


# ------------------------------------------------------------------ the supply's impedance (PDN)
ESL_NH = [("0201", 0.3), ("0402", 0.45), ("0603", 0.55), ("0805", 0.7), ("1206", 0.9), ("1210", 1.0), ("1812", 1.2)]


def _farads(v):
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([pnuµm]?)F?(\d*)", str(v or ""))
    if not m:
        return None
    x = float(m.group(1) + ("." + m.group(3) if m.group(3) else ""))
    return x * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}[m.group(2)]


def cap_model(value, footprint):
    """(C, ESR, ESL) for a capacitor from its value and footprint: typical figures for MLCCs by size, and for
    electrolytic and tantalum parts."""
    C = _farads(value)
    if not C:
        return None
    fpu = str(footprint or "").upper()
    if any(k in fpu for k in ("CP_", "ELEC", "RADIAL", "POLARIZED")):
        return C, 0.1, 5e-9
    if "TANTAL" in fpu:
        return C, 0.05, 2e-9
    esl = next((l for k, l in ESL_NH if k in fpu), 0.6) * 1e-9
    esr = 0.003 if C >= 10e-6 else 0.008 if C >= 1e-6 else 0.02 if C >= 100e-9 else 0.05 if C >= 10e-9 else 0.1
    return C, esr, esl


def pdn(ctx, net, ripple=0.05, step=None, mount_nh=0.5, fmin=1e3, fmax=1e9, n=241, judge_to=100e6):
    """|Z(f)| of the rail: its capacitors (typical ESR and ESL plus mounting), the plane pair if it has one, and the
    regulator's output (5 mOhm, 20 nH), against the target V x ripple / step current -- judged up to judge_to, above
    which the chips' own package and die capacitance carry the current."""
    from .checks.power import rail_voltages
    from .checks.power_layout import _rail_current
    b, nl = ctx.board, ctx.netlist
    net = _full(b, net)
    grounds = ctx.ground_nets()
    caps = _cap_between(nl, _short(net), grounds) if nl is not None else []
    branches, used = [], []
    for ref in caps:
        part = nl.parts.get(ref, {})
        m = cap_model(part.get("value"), part.get("footprint"))
        if not m:
            continue
        C, esr, esl = m
        branches.append((C, esr, esl + mount_nh * 1e-9))
        used.append({"ref": ref, "value": part.get("value"), "c": C, "esr": esr, "esl_nh": round((esl + mount_nh * 1e-9) * 1e9, 2),
                     "f_res": round(1 / (2 * math.pi * math.sqrt(C * (esl + mount_nh * 1e-9))), 0)})
    plane = _plane_cap(ctx, net)
    rv = rail_voltages(ctx) if nl is not None else {}
    volts = rv.get(_short(net)) or rv.get(net) or 3.3
    amps = _rail_current(ctx, net) if nl is not None else None
    said = []
    if step is None:
        step = 0.5 * amps if amps else 0.25
        said.append(f"a {step:g} A load step assumed" + (f" (half the declared {amps:g} A)" if amps else " (declare the rail's current)"))
    target = volts * ripple / step
    fs = [fmin * (fmax / fmin) ** (k / (n - 1)) for k in range(n)]
    zs = []
    for f in fs:
        w = 2 * math.pi * f
        y = 1 / complex(0.005, w * 20e-9)                     # the regulator
        for C, esr, esl in branches:
            y += 1 / complex(esr, w * esl - 1 / (w * C))
        if plane:
            y += complex(0, w * plane["c"])
        zs.append(abs(1 / y))
    peaks = []
    for k in range(1, n - 1):
        if fs[k] <= judge_to and zs[k] > target and zs[k] >= zs[k - 1] and zs[k] >= zs[k + 1]:
            peaks.append({"f": round(fs[k], 0), "z": round(zs[k], 4)})
    lines = [f"{_short(net)}: {len(used)} capacitor{'s' if len(used) != 1 else ''}"
             + (f" and a {plane['c'] * 1e9:.1f} nF plane pair" if plane else "")
             + f"; target {target * 1000:.0f} mΩ ({volts:g} V, {ripple * 100:g} % ripple, {step:g} A step)"]
    above = [(f, z) for f, z in zip(fs, zs) if z > target and f <= judge_to]
    if above:
        lo, hi = above[0][0], above[-1][0]
        lines.append(f"above the target from {_hz(lo)} to {_hz(hi)}, worst {max(z for _, z in above) * 1000:.0f} mΩ"
                     + (f" (where the capacitors run out, add a smaller one: {_suggest(hi, mount_nh)})" if not peaks else ""))
        for p in peaks[:3]:
            lines.append(f"a resonance at {_hz(p['f'])} reaches {p['z'] * 1000:.0f} mΩ: a capacitor resonating there "
                         f"({_suggest(p['f'], mount_nh)}) fills it")
    else:
        lines.append(f"below the target from {_hz(fmin)} to {_hz(judge_to)}; above that the chips' own capacitance takes over")
    lines += said
    return {"net": _short(net), "volts": volts, "target": target, "freq": fs, "z": zs, "caps": used, "plane": plane,
            "peaks": peaks, "lines": lines, "judge_to": judge_to, "ok": not above,
            "svg": _z_svg(fs, zs, target, f"{_short(net)} impedance", judge_to)}


def _hz(f):
    from .sim import si
    return si(f, "Hz")


def _suggest(f, mount_nh):
    """A standard capacitor in 0402 that resonates near f."""
    L = (0.45 + mount_nh) * 1e-9
    C = 1 / ((2 * math.pi * f) ** 2 * L)
    std = [1e-9, 2.2e-9, 4.7e-9, 10e-9, 22e-9, 47e-9, 100e-9, 220e-9, 470e-9, 1e-6, 2.2e-6, 4.7e-6, 10e-6, 22e-6]
    c = min(std, key=lambda s: abs(math.log(s / C)))
    from .sim import si
    return f"a {si(c, 'F')} 0402"


def _plane_cap(ctx, net):
    """The capacitance between the rail's pour and a ground pour on the next layer, if both are there."""
    b = ctx.board
    grounds = {n for n in b.nets if _short(n) in set(ctx.ground_nets())}
    best = None
    for z in b.zones:
        if z.is_rule_area or z.net != net:
            continue
        for l, polys in (z.fills or {}).items():
            area = sum(abs(geom.area(pl)) for pl in polys)
            for nb in neighbours(b, l):
                g_area = sum(abs(geom.area(pl)) for z2 in b.zones if not z2.is_rule_area and z2.net in grounds
                             for pl in (z2.fills or {}).get(nb, []))
                if not g_area:
                    continue
                h, er = _between(b, l, nb)
                if not h:
                    continue
                a = min(area, g_area)
                c = EPS0 * er * (a * 1e-6) / (h * 1e-3)
                if best is None or c > best["c"]:
                    best = {"c": c, "layers": [l, nb], "area_mm2": round(a, 0), "h": h}
    return best


def _z_svg(fs, zs, target, title, judge_to=None):
    from .sim import si
    from html import escape
    W, H, L_, R_, T_, B_ = 640, 280, 60, 14, 26, 34
    lx = [math.log10(f) for f in fs]
    ly = [math.log10(max(z, 1e-6)) for z in zs]
    y0, y1 = math.floor(min(ly + [math.log10(target)]) - 0.2), math.ceil(max(ly + [math.log10(target)]) + 0.2)
    x0, x1 = lx[0], lx[-1]
    X = lambda x: L_ + (x - x0) / (x1 - x0) * (W - L_ - R_)
    Y = lambda y: T_ + (y1 - y) / (y1 - y0) * (H - T_ - B_)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system, sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="#14161a"/>', f'<text x="{L_}" y="16" fill="#c8ccd4" font-weight="600">{escape(title)}</text>']
    for d in range(int(y0), int(y1) + 1):
        out.append(f'<line x1="{L_}" x2="{W - R_}" y1="{Y(d):.1f}" y2="{Y(d):.1f}" stroke="#2a2e36"/>'
                   f'<text x="{L_ - 6}" y="{Y(d) + 4:.1f}" fill="#8b8f99" text-anchor="end">{si(10 ** d, "Ω")}</text>')
    for d in range(int(math.ceil(x0)), int(math.floor(x1)) + 1):
        out.append(f'<line x1="{X(d):.1f}" x2="{X(d):.1f}" y1="{T_}" y2="{H - B_}" stroke="#22252c"/>'
                   f'<text x="{X(d):.1f}" y="{H - B_ + 16}" fill="#8b8f99" text-anchor="middle">{si(10 ** d, "Hz")}</text>')
    ty = math.log10(target)
    out.append(f'<line x1="{L_}" x2="{W - R_}" y1="{Y(ty):.1f}" y2="{Y(ty):.1f}" stroke="#e05a50" stroke-dasharray="5 3"/>'
               f'<text x="{W - R_ - 4}" y="{Y(ty) - 5:.1f}" fill="#e05a50" text-anchor="end">target {si(target, "Ω")}</text>')
    if judge_to:
        jx = X(math.log10(judge_to))
        out.append(f'<rect x="{jx:.1f}" y="{T_}" width="{W - R_ - jx:.1f}" height="{H - T_ - B_}" fill="#ffffff" opacity=".04"/>'
                   f'<text x="{jx + 4:.1f}" y="{T_ + 12}" fill="#6b7080">the chips’ own capacitance</text>')
    pts = " ".join(f"{X(a):.1f},{Y(b_):.1f}" for a, b_ in zip(lx, ly))
    out.append(f'<polyline fill="none" stroke="#6aa8ff" stroke-width="1.8" points="{pts}"/></svg>')
    return "".join(out)
