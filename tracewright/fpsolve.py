"""The floorplan, solved: every connector on its edge clear of the others, a long header lying along its edge rather
than across the board; every block inside the board, clear of the holes, the keep-outs, the connectors and each other,
as near as it can be to where it was asked for. What cannot fit is named, with what would make it fit.

    fp2, report = solve(fp)
    report: {"moved": [id], "turned": [id], "unfit": [id], "lines": [text]}

Locked items, and what the user placed or turned by hand, keep their place and turn; the rest moves around them.
Millimetres from the board's top-left corner, y down (canvas.py)."""
import copy, math, re

GAP = 1.0           # between blocks, and between a block and a connector: room for the tracks between them
EDGE = 0.5          # how far blocks stay from the board's edge
CONN_GAP = 0.5      # between connectors on one edge
TURNABLE = re.compile(r"header|\bpins?\b|gpio|\b\d+\s*[x×]\s*\d+\b|\bidc\b|box|strip|\bsocket", re.I)


def hole_r(h):
    """The keep-clear radius round a mounting hole: the screw head and its washer."""
    return float(h.get("d", 3.2)) / 2 + 1.9


def conn_dims(it):
    """(along the edge, into the board) for a connector: turned (rot 90), its other side lies along the edge."""
    w, d = float(it.get("w", 8)), float(it.get("h", 6))
    return (d, w) if int(it.get("rot") or 0) % 180 == 90 else (w, d)


def conn_rect(it, W, H):
    along, depth = conn_dims(it)
    at = float(it.get("at", (H if it["edge"] in ("left", "right") else W) / 2))
    e = it["edge"]
    if e == "left":
        return (0.0, at - along / 2, depth, at + along / 2)
    if e == "right":
        return (W - depth, at - along / 2, W, at + along / 2)
    if e == "top":
        return (at - along / 2, 0.0, at + along / 2, depth)
    return (at - along / 2, H - depth, at + along / 2, H)


def block_rect(it, x=None, y=None, rot=None):
    rot = int(it.get("rot") or 0) if rot is None else rot
    w, h = (float(it["h"]), float(it["w"])) if rot % 180 == 90 else (float(it["w"]), float(it["h"]))
    x = float(it["x"]) if x is None else x
    y = float(it["y"]) if y is None else y
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _hit(a, b, gap=0.0):
    return a[0] < b[2] + gap - 1e-6 and b[0] < a[2] + gap - 1e-6 and a[1] < b[3] + gap - 1e-6 and b[1] < a[3] + gap - 1e-6


def _hole_box(h):
    r = hole_r(h)
    return (h["x"] - r, h["y"] - r, h["x"] + r, h["y"] + r)


def _keepout_rect(k):
    kw, kh = (k["h"], k["w"]) if int(k.get("rot") or 0) % 180 == 90 else (k["w"], k["h"])
    return (k["x"] - kw / 2, k["y"] - kh / 2, k["x"] + kw / 2, k["y"] + kh / 2)


def _fixed(o):
    return bool(o.get("locked") or o.get("moved"))


def side(o):
    """The side a block is on: "top" or "bottom" (set, or said in its label: "DF40 80p (bottom side)")."""
    if o.get("side") in ("top", "bottom"):
        return o["side"]
    return "bottom" if re.search(r"bottom side|underside|\(bottom\)|on the bottom", f"{o.get('label', '')} {o.get('note', '')}", re.I) else "top"


def _name(o):
    return o.get("ref") or o.get("label") or o["id"]


def solve(fp):
    fp = copy.deepcopy(fp)
    W, H = float(fp["board"]["w"]), float(fp["board"]["h"])
    items = fp.get("items") or []
    conns = [it for it in items if it.get("edge")]
    blocks = [it for it in items if not it.get("edge")]
    holes = fp.get("holes") or []
    rep = {"moved": [], "turned": [], "unfit": [], "lines": []}
    span = lambda e: H if e in ("left", "right") else W
    across = lambda e: W if e in ("left", "right") else H

    # connectors: a long one lies along its edge; then each edge is packed, nearest to where each was asked for
    for c in conns:
        along, depth = conn_dims(c)
        L, A = span(c["edge"]), across(c["edge"])
        fits = lambda a, d: a <= L - 1.0 and d <= A - 1.0
        awkward = depth > 0.45 * A and depth > along          # standing far out across the board
        turnable = TURNABLE.search(f"{c.get('label', '')} {c.get('note', '')} {c.get('kind', '')}") or max(along, depth) >= 3 * min(along, depth)
        # turned, its depth becomes its length along the edge: only when that fits, and is better than it is now
        if (awkward or not fits(along, depth)) and turnable and not _fixed(c) and fits(depth, along) and along < depth:
            c["rot"] = 0 if int(c.get("rot") or 0) % 180 == 90 else 90
            rep["turned"].append(c["id"])
            rep["lines"].append(f"{_name(c)} turned to lie along the {c['edge']} edge ({max(along, depth):g} mm long)")
    by_edge = {}
    for c in conns:
        by_edge.setdefault(c["edge"], []).append(c)
    placed_conn = []
    for e, cs in by_edge.items():
        L = span(e)
        taken = []                                         # intervals already on this edge
        order = sorted(cs, key=lambda c: (not _fixed(c), float(c.get("at", L / 2))))
        for c in order:
            along, depth = conn_dims(c)
            want = float(c.get("at", L / 2))
            if along > L - 1.0:
                rep["unfit"].append(c["id"])
                longer = max(W, H) if L < max(W, H) else None
                rep["lines"].append(f"{_name(c)} is {along:g} mm long and the {e} edge is {L:g} mm" +
                                    (f": put it on a {longer:g} mm edge" if longer and longer > along + 1 else "") +
                                    f", or make that side at least {math.ceil(along + 2):g} mm")
                placed_conn.append(c)
                continue
            if _fixed(c):
                taken.append((want - along / 2, want + along / 2))
                placed_conn.append(c)
                continue
            lo, hi = along / 2 + 0.5, L - along / 2 - 0.5
            best = None
            for cand in sorted({min(max(want, lo), hi)} | {b + CONN_GAP + along / 2 for _, b in taken} |
                               {a - CONN_GAP - along / 2 for a, _ in taken}, key=lambda v: abs(v - want)):
                if cand < lo - 1e-6 or cand > hi + 1e-6:
                    continue
                iv = (cand - along / 2, cand + along / 2)
                if all(iv[1] + CONN_GAP <= a + 1e-6 or iv[0] >= b + CONN_GAP - 1e-6 for a, b in taken):
                    best = cand
                    break
            if best is None:
                rep["unfit"].append(c["id"])
                rep["lines"].append(f"{_name(c)} has no room left on the {e} edge: move it to another edge, or make the board longer")
                placed_conn.append(c)
                continue
            if abs(best - want) > 0.25:
                rep["moved"].append(c["id"])
            c["at"] = round(best, 2)
            taken.append((best - along / 2, best + along / 2))
            placed_conn.append(c)
    # two connectors meeting at a corner: the one free to move slides away from it
    for _ in range(3):
        for i, a in enumerate(placed_conn):
            for b in placed_conn[i + 1:]:
                if a["edge"] == b["edge"] or not _hit(conn_rect(a, W, H), conn_rect(b, W, H), CONN_GAP):
                    continue
                mover = b if not _fixed(b) else a if not _fixed(a) else None
                if mover is None:
                    continue
                other = a if mover is b else b
                ra, ro = conn_rect(mover, W, H), conn_rect(other, W, H)
                along, _ = conn_dims(mover)
                L = span(mover["edge"])
                if mover["edge"] in ("left", "right"):
                    push = (ro[3] - ra[1] + CONN_GAP) if ra[1] < ro[3] and (ra[1] + ra[3]) / 2 > (ro[1] + ro[3]) / 2 - 1e-6 else -(ra[3] - ro[1] + CONN_GAP)
                else:
                    push = (ro[2] - ra[0] + CONN_GAP) if (ra[0] + ra[2]) / 2 > (ro[0] + ro[2]) / 2 - 1e-6 else -(ra[2] - ro[0] + CONN_GAP)
                mover["at"] = round(min(max(float(mover["at"]) + push, along / 2), L - along / 2), 2)
                if mover["id"] not in rep["moved"]:
                    rep["moved"].append(mover["id"])

    # blocks: inside the board, clear of everything on their side, as near as possible to where each was asked for.
    # Connectors at the edge, holes and keep-outs take room on both sides.
    obstacles = [(conn_rect(c, W, H), "both") for c in conns if c["id"] not in rep["unfit"]]
    obstacles += [(_hole_box(h), "both") for h in holes]
    obstacles += [(_keepout_rect(k), "both") for k in fp.get("keepouts") or []]
    clash = lambda r, sd: any(_hit(r, o, GAP) for o, osd in obstacles if osd in ("both", sd))
    step = max(0.5, round(max(W, H) / 130 * 2) / 2)
    for b in [b for b in blocks if _fixed(b)]:              # what the user placed stays; anything it overlaps is said
        r = block_rect(b)
        if clash(r, side(b)):
            rep["lines"].append(f"{_name(b)} (placed by you) overlaps something; it stays where you put it")
        obstacles.append((r, side(b)))
    free = sorted([b for b in blocks if not _fixed(b)], key=lambda b: -float(b["w"]) * float(b["h"]))
    for b in free:
        want = (float(b.get("x", W / 2)), float(b.get("y", H / 2)))
        rots = [int(b.get("rot") or 0)]
        if float(b["w"]) != float(b["h"]):
            rots.append((rots[0] + 90) % 360)
        best = None
        for rot in rots:
            w, h = (float(b["h"]), float(b["w"])) if rot % 180 == 90 else (float(b["w"]), float(b["h"]))
            x0, x1, y0, y1 = EDGE + w / 2, W - EDGE - w / 2, EDGE + h / 2, H - EDGE - h / 2
            if x0 > x1 + 1e-6 or y0 > y1 + 1e-6:
                continue
            xs = [x0 + i * step for i in range(int((x1 - x0) / step) + 1)] + [x1]
            ys = [y0 + i * step for i in range(int((y1 - y0) / step) + 1)] + [y1]
            cx, cy = min(max(want[0], x0), x1), min(max(want[1], y0), y1)
            cands = sorted(((x, y) for x in xs for y in ys), key=lambda p: (p[0] - want[0]) ** 2 + (p[1] - want[1]) ** 2)
            for x, y in [(cx, cy)] + cands:
                r = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                if not clash(r, side(b)):
                    d2 = (x - want[0]) ** 2 + (y - want[1]) ** 2 + (0 if rot == rots[0] else 4.0)    # a turn costs a little
                    if best is None or d2 < best[0]:
                        best = (d2, x, y, rot)
                    break
        if best is None:
            rep["unfit"].append(b["id"])
            rep["lines"].append(f"{_name(b)} ({float(b['w']):g} x {float(b['h']):g} mm) does not fit in the room left")
            continue
        _, x, y, rot = best
        if abs(x - want[0]) > 0.25 or abs(y - want[1]) > 0.25:
            rep["moved"].append(b["id"])
        if rot != int(b.get("rot") or 0):
            rep["turned"].append(b["id"])
        b["x"], b["y"], b["rot"] = round(x, 2), round(y, 2), rot
        obstacles.append((block_rect(b), side(b)))
    if rep["unfit"]:
        rep["lines"].append(bigger(fp))
    moved = [i for i in rep["moved"] if i not in rep["turned"]]
    if moved:
        names = [_name(next(o for o in items if o["id"] == i)) for i in moved]
        rep["lines"].insert(0, f"moved clear of each other: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
    return fp, rep


def bigger(fp):
    """A board size that would hold everything, same proportions: the parts' area with their spacing, and the
    longest connector."""
    W, H = float(fp["board"]["w"]), float(fp["board"]["h"])
    need = 0.0
    for it in fp.get("items") or []:
        if it.get("edge"):
            along, depth = conn_dims(it)
            need += (along + CONN_GAP) * (depth + GAP)
        else:
            need += (float(it["w"]) + GAP) * (float(it["h"]) + GAP)
    need += sum((2 * hole_r(h)) ** 2 for h in fp.get("holes") or [])
    need += sum(float(k["w"]) * float(k["h"]) for k in fp.get("keepouts") or [])
    k = max(1.0, math.sqrt(need / 0.7 / (W * H)))         # parts fill about 70 % of a board at best
    longest = max([max(conn_dims(it)) for it in fp.get("items") or [] if it.get("edge")] or [0])
    nw, nh = math.ceil(W * k), math.ceil(H * k)
    if longest > max(nw, nh) - 1:
        nw, nh = (math.ceil(longest + 2), nh) if W >= H else (nw, math.ceil(longest + 2))
    return f"everything would fit on about {nw:g} x {nh:g} mm (now {W:g} x {H:g})"
