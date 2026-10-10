"""Length tuning after the route: the shorter half of each differential pair brought within its skew budget (the
budget hs.pairs holds it to: tw.checks.signal.pair_tolerance), and the short members of each length group
(checks.length_groups) within the group's tolerance of its longest -- by meanders on a straight run of the track, as a
person tunes: square bumps with 45-degree shoulders, a pitch apart, on the side with room, clear of everything else
(the router's legality raster checks each bump as it is laid).

    rep = tune(project, apply=True)   {"tuned": [{net, added_mm, skew_before, skew_after, budget}], "left": [...]}

The meander shape is the board editor's (web/js/boardgeom.js serpentine): one bump of height h with shoulders c adds
2h - (8 - 4 sqrt 2) c; the fewest bumps that reach, each the height that makes it exact.
"""
import collections, math, re

from .. import geom

MEANDER_K = 8 - 4 * math.sqrt(2)


def serpentine(a, b, extra, pitch, max_amp, side=1):
    """A straight run a -> b made `extra` mm longer: (points, added, short) or None (the run is too short for a bump)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 3 * pitch or extra <= 0:
        return None
    ux, uy = dx / L, dy / L
    nx, ny = uy * side, -ux * side
    c = pitch / 4
    room = max(1, int((L - pitch) // (2 * pitch)))
    most = 2 * max_amp - MEANDER_K * c
    if most <= 0:
        return None
    nb = min(room, max(1, math.ceil(extra / most)))
    hgt = (extra / nb + MEANDER_K * c) / 2
    short = 0.0
    if hgt > max_amp:
        hgt = max_amp
        short = extra - nb * (2 * hgt - MEANDER_K * c)
    if hgt < 2 * c:
        c = max(0.01, extra / nb / (4 - MEANDER_K))
        hgt = 2 * c
    used = 2 * nb * pitch - pitch
    start = (L - used) / 2
    P = lambda s0, o: (a[0] + ux * s0 + nx * o, a[1] + uy * s0 + ny * o)
    out = [a]
    for i in range(nb):
        s0 = start + i * 2 * pitch
        out += [P(s0, 0), P(s0 + c, c), P(s0 + c, hgt - c), P(s0 + 2 * c, hgt), P(s0 + pitch - 2 * c, hgt),
                P(s0 + pitch - c, hgt - c), P(s0 + pitch - c, c), P(s0 + pitch, 0)]
    out.append(b)
    pts = [out[0]]
    for q in out[1:]:
        if geom.dist(q, pts[-1]) > 1e-6:
            pts.append(q)
    added = sum(geom.dist(p, q) for p, q in zip(pts, pts[1:])) - L
    return pts, added, max(0.0, short)


def _lengths(b):
    L = collections.defaultdict(float)
    for t in b.tracks:
        L[t.net] += t.length()
    return L


def _wants(project, b):
    """[(net to lengthen, by mm, why)] -- pairs over their budget, length-group members short of their longest."""
    from ..checks.context import Context
    from ..checks import signal
    ctx = Context(project, offline=True)
    L = _lengths(b)
    out = []
    for p, n in signal.find_pairs(b.nets):
        kind = signal.pair_kind(p)
        tol = signal.pair_tolerance(ctx, p, n, kind) if kind else None
        if not tol or L[p] == 0 or L[n] == 0:
            continue
        skew = abs(L[p] - L[n])
        if skew > tol["mm"]:
            short = p if L[p] < L[n] else n
            out.append((short, skew, f"pair {p.rsplit('/', 1)[-1]}/{n.rsplit('/', 1)[-1]}: {skew:.2f} mm apart, "
                                     f"{tol['mm']:g} mm allowed ({tol['why']})", tol["mm"]))
    for grp in (ctx.setting("checks.length_groups", []) or []):
        pats = [re.compile("^" + re.escape(x).replace(r"\*", ".*") + "$", re.I) for x in grp.get("nets", [])]
        mem = [n for n in b.nets if any(pt.match(n.rsplit("/", 1)[-1]) for pt in pats) and L[n] > 0]
        if len(mem) < 2:
            continue
        tol = float(grp.get("tolerance_mm", 2.5))
        top = max(L[n] for n in mem)
        for n in mem:
            if top - L[n] > tol:
                out.append((n, top - L[n], f"group {grp.get('name', '?')}: {top - L[n]:.2f} mm short of its longest", tol))
    return out


def pairs(project, board=None):
    """Every pair the checks hold to a budget: [{p, n, kind, length_p, length_n, skew, budget, why, ok}], longest skew first."""
    from ..board import Board
    from ..checks.context import Context
    from ..checks import signal
    b = board or Board.load(project.pcb)
    ctx = Context(project, offline=True)
    L = _lengths(b)
    out = []
    for p, n in signal.find_pairs(b.nets):
        kind = signal.pair_kind(p)
        tol = signal.pair_tolerance(ctx, p, n, kind) if kind else None
        if not tol:
            continue
        routed = L[p] > 0 and L[n] > 0
        skew = abs(L[p] - L[n])
        out.append({"p": p, "n": n, "kind": kind, "length_p": round(L[p], 3), "length_n": round(L[n], 3),
                    "skew": round(skew, 3) if routed else None, "budget": tol["mm"], "why": tol["why"],
                    "ok": (skew <= tol["mm"] + 1e-9) if routed else None})
    return sorted(out, key=lambda x: -(x["skew"] or 0) / max(x["budget"], 1e-6))


def tune(project, apply=True, log=print, apply_fn=None):
    from ..board import Board
    from .driver import GridRoute
    from ..pcb import client
    b = Board.load(project.pcb)
    wants = _wants(project, b)
    if not wants:
        return {"tuned": [], "left": []}
    g = GridRoute(project, log=lambda m: None).setup()
    B = g.B
    tuned, left, ops = [], [], []
    for net, extra, why, tol in wants:
        target = extra - tol / 2 if extra - tol / 2 > 0 else extra    # into the middle of the budget, not its edge
        prof = g.net_class.get(net, "Default")
        pr = B.profiles[prof]
        segs = sorted([t for t in b.tracks if t.net == net and not t.mid and t.layer in B.layers], key=lambda t: -t.length())
        done = None
        for t in segs[:8]:
            li = B.layers.index(t.layer)
            lt, _ = B.legal(net, prof)
            w = t.w
            pitch = max(4 * w, 2 * w + pr.cl, 0.6)
            for side in (1, -1):
                for amp in [max(v, 0.4) for v in (6 * w, 4.5 * w, 3 * w, 2 * w)]:
                    m = serpentine(t.a, t.b, target, pitch, amp, side)
                    if not m or m[2] > 0.02:
                        continue
                    pts = m[0]
                    ok = all(_clear(B, lt, li, p, q) for p, q in zip(pts, pts[1:]))
                    if ok:
                        done = (t, pts, m[1])
                        break
                if done:
                    break
            if done:
                break
        if not done:
            left.append({"net": net, "short_mm": round(extra, 3), "why": why + "; no straight run with room for a meander"})
            continue
        t, pts, added = done
        for p, q in zip(pts, pts[1:]):
            B.stamp_track(net, t.layer, p, q, t.w)
        ops.append({"op": "delete", "uuids": [t.uuid], "kinds": ["track"]})
        ops.append({"op": "tracks", "items": [{"net": net, "layer": t.layer, "a": [round(p[0], 4), round(p[1], 4)],
                                               "b": [round(q[0], 4), round(q[1], 4)], "w": t.w} for p, q in zip(pts, pts[1:])]})
        tuned.append({"net": net, "added_mm": round(added, 3), "why": why})
        log(f"tuned {net.rsplit('/', 1)[-1]}: +{added:.2f} mm ({why})")
    rep = {"tuned": tuned, "left": left}
    if apply and ops:
        if any(z.net and not z.is_rule_area for z in b.zones):
            ops.append({"op": "fill"})
        res = (apply_fn or (lambda o: client.apply(project, o, live="auto")))(ops)
        rep["applied"] = bool(res.get("ok"))
    return rep


def _clear(B, lt, li, a, b):
    """Every raster cell along a run is legal for the net on that layer."""
    n = max(2, int(geom.dist(a, b) / 0.05) + 1)
    for k in range(n):
        f = k / (n - 1)
        x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        j, i = B.cell(x, y)
        if not (0 <= j < B.ny and 0 <= i < B.nx) or not lt[li][j * B.nx + i]:
            return False
    return True
