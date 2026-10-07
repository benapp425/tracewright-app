"""Test points: the nets a person (or a test fixture) needs to touch on the built board -- every supply rail and ground,
the programming and debug lines, reset, the buses -- and where each can be touched: a test point already there, a
through-hole pin of the net, or a spot to add one on the chosen side, clear of other copper and at least a probe's pitch
from the other test points.

    plan(ctx, side="B", pitch=2.54) -> {"items": [{net, why, at: {kind, ref?, pin?, x, y, side}}], "lines", "add"}
"""
import math, re

from . import geom

DEBUG = re.compile(r"(^|_)(SWDIO|SWCLK|SWO|SWD|NRST|RESET|RST|TX|RX|TXD|RXD|UART\w*|BOOT\w*|TDI|TDO|TMS|TCK|JTAG\w*|UPDI|"
                   r"MOSI|MISO|SCK|EN|CHIP_PU|PROG\w*|DEBUG\w*)($|_)", re.I)
BUS = re.compile(r"(^|_)(SDA|SCL|I2C\w*|CAN_?[HL]|RS485_?[AB])($|_)", re.I)
TP_PAD = 1.0          # mm, the pad a test point gets (TestPoint_Pad_D1.0mm)


def _short(n):
    return str(n or "").rsplit("/", 1)[-1]


def wanted(ctx):
    """[(net, why)] the nets worth a test point."""
    b = ctx.board
    grounds = set(ctx.ground_nets())
    power = set(ctx.power_nets())
    out = []
    for n in sorted(b.nets):
        s = _short(n)
        if not s or n.startswith("unconnected-"):
            continue
        if s in grounds:
            out.append((n, "ground: the reference for every measurement"))
        elif s in power:
            out.append((n, "a supply rail: its voltage is the first thing to measure"))
        elif DEBUG.search(s):
            out.append((n, "programming / debug"))
        elif BUS.search(s):
            out.append((n, "a bus to watch with a scope or analyser"))
    return out


def _existing(b, net):
    """Places on the net a probe can touch: test points first, then through-hole pins."""
    out = []
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net != net:
                continue
            if fp.ref.upper().startswith("TP"):
                out.append((0, {"kind": "test point", "ref": fp.ref, "pin": str(p.num), "x": round(p.x, 2), "y": round(p.y, 2),
                                "side": "both" if p.drill else fp.side}))
            elif p.drill and p.kind == "thru_hole":
                out.append((1, {"kind": "through-hole pin", "ref": fp.ref, "pin": str(p.num), "x": round(p.x, 2), "y": round(p.y, 2),
                                "side": "both"}))
    return [x for _, x in sorted(out, key=lambda q: q[0])]


def _spot(ctx, net, side, taken, pitch):
    """A free place for a test point pad beside the net's copper on the side (its own track or pad within reach)."""
    b = ctx.board
    layer = "B.Cu" if side == "B" else "F.Cu"
    cands, via_c = [], []
    for t in b.tracks:
        if t.net != net:
            continue
        L = geom.dist(t.a, t.b)
        n = max(1, int(L / 1.0))
        for k in range(n + 1):
            x, y = t.a[0] + (t.b[0] - t.a[0]) * k / n, t.a[1] + (t.b[1] - t.a[1]) * k / n
            if t.layer == layer:
                cands.append((x, y))
            else:                                     # on the other side: a via down from the track, the pad beside it
                ux, uy = (t.b[0] - t.a[0]) / (L or 1), (t.b[1] - t.a[1]) / (L or 1)
                for sgn in (1, -1):
                    via_c.append(((x, y), (x - uy * 1.4 * sgn, y + ux * 1.4 * sgn), t.layer))
    for v in b.vias:
        if v.net == net:
            for dx, dy in ((1.6, 0), (-1.6, 0), (0, 1.6), (0, -1.6)):
                cands.append((v.x + dx, v.y + dy))
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net == net and (p.drill or (side == "B") == (fp.side == "B")):
                for dx, dy in ((2.0, 0), (-2.0, 0), (0, 2.0), (0, -2.0)):
                    cands.append((p.x + dx, p.y + dy))
    for x, y in cands:
        if any(math.hypot(x - a, y - b_) < pitch for a, b_ in taken):
            continue
        if _clear(b, x, y, layer, net, TP_PAD / 2 + 0.25):
            return {"kind": "add", "x": round(x, 2), "y": round(y, 2), "side": side, "layer": layer}
    for (vx, vy), (x, y), tl in via_c:
        if any(math.hypot(x - a, y - b_) < pitch for a, b_ in taken):
            continue
        mid = ((vx + x) / 2, (vy + y) / 2)
        if _clear(b, x, y, layer, net, TP_PAD / 2 + 0.25) and _clear(b, mid[0], mid[1], tl, net, 0.45) and \
                _clear(b, mid[0], mid[1], layer, net, 0.45):
            return {"kind": "add", "x": round(x, 2), "y": round(y, 2), "side": side, "layer": layer,
                    "via": [round(mid[0], 2), round(mid[1], 2)], "from": tl}
    return None


def _clear(b, x, y, layer, net, r):
    """Nothing of another net within r of (x, y) on the layer, no part body over it on that side, and inside the board."""
    p = (x, y)
    if b.outline:
        outer = max(b.outline, key=lambda o: abs(geom.area(o)))
        if not geom.inside(p, outer) or min(geom.seg_point_dist(p, outer[i], outer[(i + 1) % len(outer)]) for i in range(len(outer))) < r + 0.3:
            return False
    side = "B" if layer == "B.Cu" else "F"
    for fp in b.fp_list:
        bb = fp.bbox()
        if not (bb[0] - 3 <= x <= bb[2] + 3 and bb[1] - 3 <= y <= bb[3] + 3):
            continue
        if fp.side == side and bb[0] - 0.3 <= x <= bb[2] + 0.3 and bb[1] - 0.3 <= y <= bb[3] + 0.3:
            return False                               # under a part on this side
        for q in fp.pads:
            if (layer in q.layers or "*.Cu" in q.layers) and q.net != net:
                if min((geom.poly_dist(p, pl) for pl in q.polys if len(pl) >= 3), default=geom.dist(p, (q.x, q.y))) < r:
                    return False
    for t in b.tracks:
        if t.layer == layer and t.net != net and geom.seg_point_dist(p, t.a, t.b) - t.w / 2 < r:
            return False
    for v in b.vias:
        if v.net != net and layer in v.span(b.copper) and geom.dist(p, (v.x, v.y)) - v.d / 2 < r:
            return False
    return True


def plan(ctx, side="B", pitch=2.54):
    b = ctx.board
    items, taken = [], []
    for net, why in wanted(ctx):
        here = _existing(b, net)
        if here:
            at = here[0]
            taken.append((at["x"], at["y"]))
            items.append({"net": _short(net), "why": why, "at": at})
    other = "F" if side == "B" else "B"
    for net, why in wanted(ctx):
        if any(i["net"] == _short(net) for i in items):
            continue
        at = _spot(ctx, net, side, taken, pitch) or _spot(ctx, net, other, taken, pitch)
        if at:
            taken.append((at["x"], at["y"]))
        items.append({"net": _short(net), "why": why, "at": at or {"kind": "none"}})
    close = []
    pts = [(i["net"], i["at"]["x"], i["at"]["y"]) for i in items if i["at"].get("x") is not None]
    for k, (n, x, y) in enumerate(pts):
        for m, x2, y2 in pts[k + 1:]:
            if math.hypot(x - x2, y - y2) < pitch - 1e-6:
                close.append((n, m, round(math.hypot(x - x2, y - y2), 2)))
    add = [i for i in items if i["at"]["kind"] == "add"]
    lines = [f"{len(items)} nets to reach: {len(items) - len(add) - sum(1 for i in items if i['at']['kind'] == 'none')} already "
             f"reachable, {len(add)} test point{'s' if len(add) != 1 else ''} to add"]
    for i in items:
        a = i["at"]
        if a["kind"] == "add":
            lines.append(f"{i['net']}: add a test point at ({a['x']}, {a['y']}) on the {'bottom' if a['side'] == 'B' else 'top'}"
                         + (f", a via down from its {a['from']} track" if a.get("via") else "") + f" ({i['why']})")
        elif a["kind"] == "none":
            lines.append(f"{i['net']}: no free spot beside its copper: route a short stub out to a test point")
        else:
            lines.append(f"{i['net']}: {a['kind']} {a['ref']} pin {a['pin']}")
    for n, m, d in close:
        lines.append(f"{n} and {m} test points are {d} mm apart: a fixture's probes want {pitch:g} mm")
    return {"items": items, "lines": lines, "add": add, "close": close, "side": side, "pitch": pitch}
