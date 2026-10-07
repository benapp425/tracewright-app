"""Fan-out and escape: how the pins of a dense part get out from under it. How many tracks fit between its pads, whether
a via fits between four balls, how many rings of balls each layer can take out, and so how many signal layers the part
needs -- and when that calls for HDI (vias in the pads, microvias). Claude reads it before placing a BGA, the Routing
tab shows it, and fanout() puts the vias down so the router starts from them.

    tracks_between(gap, w, s)       how many tracks of width w fit in a gap, s from the copper each side and each other
    via_fits(pitch, pad, via, s)    a via between four balls
    grid(fp)                        an area-array part's ball grid, or None
    plan(project, board=None)       [{ref, kind, pitch, rows, cols, rings, need, have, method, hdi, lines}, ...]
    fanout(project, ref, method)    the vias (and dog-bone stubs) as board ops

The ring count is the usual estimate: the outer ring escapes straight out; each track that fits between two balls (or
two vias) takes one more ring out on that layer. Power and ground balls drop to their planes through their own vias.
"""
import math, statistics

from . import geom

def tracks_between(gap, w, s):
    """n tracks need n*w + (n+1)*s <= gap."""
    if gap <= 0 or w <= 0:
        return 0
    return max(0, int(math.floor((gap - s) / (w + s) + 1e-9)))


def via_fits(pitch, pad, via, s):
    """A via at the centre of four balls: half the diagonal must hold the via's radius, the clearance and the ball's."""
    return pitch * math.sqrt(2) / 2 >= via / 2 + s + pad / 2 - 1e-9 and pitch >= via + s - 1e-9


def _cluster(vals, tol=0.02):
    out = []
    for v in sorted(vals):
        if out and abs(v - out[-1][-1]) <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def grid(fp):
    """{"pitch", "rows", "cols", "balls": [(pad, row, col, ring)], "pad": ball pad size, "cx", "cy"} for an area-array
    part (balls under the whole body, not only round its edge), else None."""
    pads = [p for p in fp.pads if sum(1 for l in p.layers if l.endswith(".Cu")) == 1 and p.kind == "smd"]
    if len(pads) < 9:
        return None
    sizes = [min(p.w, p.h) for p in pads]
    med = statistics.median(sizes)
    balls = [p for p in pads if min(p.w, p.h) <= med * 1.6]          # no thermal pad
    if len(balls) < 9:
        return None
    xs, ys = _cluster([p.x for p in balls]), _cluster([p.y for p in balls])
    if len(xs) < 3 or len(ys) < 3:
        return None
    steps = [b - a for v in (xs, ys) for a, b in zip(v, v[1:]) if b - a > 0.05]
    if not steps:
        return None
    pitch = round(min(steps), 3)
    col = lambda x: min(range(len(xs)), key=lambda i: abs(xs[i] - x))
    row = lambda y: min(range(len(ys)), key=lambda i: abs(ys[i] - y))
    R, C = len(ys), len(xs)
    out = []
    for p in balls:
        r, c = row(p.y), col(p.x)
        out.append((p, r, c, min(r, c, R - 1 - r, C - 1 - c)))
    inner = sum(1 for _, _, _, ring in out if ring >= 2)
    if not inner:                                  # pads round the edge only: a QFN or QFP, not an array
        return None
    return {"pitch": pitch, "rows": R, "cols": C, "balls": out, "pad": round(med, 3),
            "cx": (xs[0] + xs[-1]) / 2, "cy": (ys[0] + ys[-1]) / 2}


def _kinds(board, cfg=None):
    from .nettypes import classify
    kinds = classify(sorted(board.nets), cfg=cfg)
    return {n: (k or {}).get("kind") for n, k in kinds.items()}


def _rules(project, board):
    """The tracks and vias the router uses under the part: its neck-down width and clearance, the Default via."""
    from .pro import ProjectSettings
    from .route.driver import neck_values
    try:
        c = ProjectSettings.load(project.pro).cls("Default") if project.pro else {}
    except Exception:
        c = {}
    from . import dfm
    w = float(c.get("track_width") or 0.2)
    s = float(c.get("clearance") or 0.2)
    nw, ns = neck_values(board)
    fab = dfm.profile(((getattr(project, "cfg", None) or {}).get("fab") or {}).get("house") or "jlcpcb", len(board.copper))
    vd, vh = float(c.get("via_diameter") or 0.45), float(c.get("via_drill") or 0.2)
    return {"w": min(w, nw), "s": min(s, ns), "w_class": w, "s_class": s,           # fan-out vias: the fab's smallest
            "via_d": min(vd, fab["min_via_diameter"]), "via_drill": min(vh, fab["min_via_drill"])}


def _signal_layers(project, board):
    from . import stackup
    return stackup.routing_layers(stackup.get(getattr(project, "cfg", None)), board.copper)


def plan_part(fp, g, kinds, rules, have, hdi_cfg):
    """One part's escape plan."""
    from . import hdi as hdimod
    w, s, via = rules["w"], rules["s"], rules["via_d"]
    P, pad = g["pitch"], g["pad"]
    sig = [(p, r, c, ring) for p, r, c, ring in g["balls"] if p.net and not p.net.startswith("unconnected-")
           and kinds.get(p.net) not in ("power", "ground")]
    supply = [b for b in g["balls"] if b[0].net and kinds.get(b[0].net) in ("power", "ground")]
    rings = max((ring for _, _, _, ring in sig), default=-1) + 1
    n_pad = tracks_between(P - pad, w, s)
    dog = via_fits(P, pad, via, s)
    n_via = tracks_between(P - via, w, s)
    top = 1 + n_pad
    per = 1 + n_via
    need = 1 if rings <= top else 1 + math.ceil((rings - top) / per)
    h = hdi_cfg
    n_micro = tracks_between(P - h["micro_d"], w, s)
    micro_ok = h["micro_d"] + s <= P + 1e-9
    vip_ok = via + s <= P + 1e-9 and via <= pad + 0.15        # a through via in the pad: no wider than the pad's land
    need_micro = 1 if rings <= top else 1 + (1 if rings - top <= 1 + n_micro else 1 + math.ceil((rings - top - 1 - n_micro) / per))
    if rings <= top and not supply:
        method = "top"
    elif dog:
        method = "dog-bone"
    elif h["on"] and h["via_in_pad"] and vip_ok:
        method = "via-in-pad"
    elif h["on"] and h["microvias"] and micro_ok:
        method = "microvia"
    else:
        method = None
    why_hdi = None
    if not dog and (rings > top or supply):
        why_hdi = (f"a {via:g} mm via does not fit between its balls (it needs {via / 2 + s + pad / 2:.2f} mm from each, there is "
                   f"{P * math.sqrt(2) / 2:.2f}): " + ("vias in the pads" if vip_ok else "laser microvias in the pads" if micro_ok
                                                     else "not even a microvia fits; a coarser package"))
    elif need > have and micro_ok and need_micro < need:
        why_hdi = f"it needs {need} signal layers with through vias, {need_micro} with microvias; the board has {have}"
    name = f"{fp.ref} ({g['rows']}×{g['cols']}, {P:g} mm)"
    lines = [f"{name}: {len(g['balls'])} balls, {len(sig)} signals in {rings} ring{'s' if rings != 1 else ''} from the edge"]
    lines.append(f"top layer: {'no' if not n_pad else n_pad} track{'s' if n_pad != 1 else ''} of {w:g} mm between balls, "
                 f"so it takes out {top} ring{'s' if top != 1 else ''}")
    if rings > top or supply:
        if dog:
            lines.append(f"a {via:g} mm via fits between four balls (dog-bone); {n_via or 'no'} track{'s' if n_via != 1 else ''} between "
                         f"vias, so each further layer takes out {per} ring{'s' if per != 1 else ''}")
        else:
            lines.append(f"no room for a {via:g} mm via between balls: vias go in the pads (HDI)"
                         + (f"; with {h['micro_d']:g} mm microvias, {n_micro or 'no'} track{'s' if n_micro != 1 else ''} between them" if micro_ok else ""))
    if rings > top:
        lines.append(f"{need} signal layer{'s' if need != 1 else ''} needed, the board has {have}"
                     + (" -- fine" if need <= have else " -- too few"))
    if why_hdi:
        lines.append(("HDI is on: " if h["on"] else "HDI would help: ") + why_hdi)
    return {"ref": fp.ref, "kind": "array", "pitch": P, "rows": g["rows"], "cols": g["cols"], "balls": len(g["balls"]),
            "signals": len(sig), "supply_balls": len(supply), "rings": rings, "tracks_between_balls": n_pad,
            "tracks_between_vias": n_via, "via_fits": dog, "top_rings": top, "rings_per_layer": per, "need": need,
            "need_with_microvias": need_micro if micro_ok else None, "via_in_pad_fits": vip_ok, "microvia_fits": micro_ok,
            "have": have, "method": method, "hdi": why_hdi,
            "ok": bool(method) and need <= have, "lines": lines}


def _perimeter(fp, rules):
    """A fine-pitch part with pads round its edge only: what fits between two pads."""
    pads = [p for p in fp.pads if sum(1 for l in p.layers if l.endswith(".Cu")) == 1 and p.kind == "smd" and p.net]
    if len(pads) < 8:
        return None
    best = None
    for i, a in enumerate(pads):
        for b in pads[i + 1:]:
            d = geom.dist((a.x, a.y), (b.x, b.y))
            if d > 0.05 and (best is None or d < best[0]):
                best = (d, a)
    if not best or best[0] > 0.51:
        return None
    pitch, a = round(best[0], 3), best[1]
    across = min(a.w, a.h)
    n = tracks_between(pitch - across, rules["w"], rules["s"])
    line = (f"{fp.ref} ({pitch:g} mm pitch): {'no track' if not n else str(n) + ' track' + ('s' if n > 1 else '')} between pads"
            f" at {rules['w']:g}/{rules['s']:g} mm; every pad leaves outward")
    return {"ref": fp.ref, "kind": "perimeter", "pitch": pitch, "tracks_between_pads": n, "ok": True, "method": "top",
            "hdi": None, "lines": [line]}


def plan(project, board=None, refs=None):
    """The escape plan of every dense part on the board (area arrays, and pads under 0.5 mm apart)."""
    from .board import Board
    from . import hdi as hdimod
    b = board or Board.load(project.pcb)
    cfg = getattr(project, "cfg", None) or {}
    kinds = _kinds(b, cfg)
    rules = _rules(project, b)
    have = len(_signal_layers(project, b))
    h = hdimod.get(cfg)
    out = []
    for fp in b.fp_list:
        if refs and fp.ref not in refs:
            continue
        g = grid(fp)
        if g:
            out.append(plan_part(fp, g, kinds, rules, have, h))
        else:
            q = _perimeter(fp, rules)
            if q:
                out.append(q)
    return out


def fanout(project, ref, method=None, board=None):
    """(ops, summary): a via for each ball that needs one -- signal balls past the rings the top layer takes out, and every
    power and ground ball (to its plane) -- dog-bone (a short stub to a via between four balls, pointing away from the
    part's centre), via-in-pad or microvia (HDI). Balls that already have their via are left."""
    from .board import Board
    from . import hdi as hdimod
    b = board or Board.load(project.pcb)
    fp = b.footprints.get(ref)
    if fp is None:
        raise ValueError(f"no footprint {ref} on the board")
    g = grid(fp)
    if not g:
        raise ValueError(f"{ref} is not a ball-grid part")
    cfg = getattr(project, "cfg", None) or {}
    h = hdimod.get(cfg)
    rules = _rules(project, b)
    kinds = _kinds(b, cfg)
    pl = plan_part(fp, g, kinds, rules, len(_signal_layers(project, b)), h)
    method = method or pl["method"]
    if method in (None, "top"):
        if method is None:
            raise ValueError(f"{ref}: {pl['hdi']}. Turn HDI on (vias in pads or microvias), or use a coarser package.")
        return [], {"ref": ref, "method": "top", "vias": 0, "note": "every signal escapes on the top layer; no vias needed"}
    if method in ("via-in-pad", "microvia") and not h["on"]:
        raise ValueError(f"{method}: HDI is off. Turn it on first (the fab builds it as an HDI board, at a higher price).")
    if method == "dog-bone" and not pl["via_fits"]:
        raise ValueError(f"{ref}: {pl['hdi']}")
    P = g["pitch"]
    have = {(round(v.x, 2), round(v.y, 2)) for v in b.vias}
    vias, stubs = [], []
    left = {"signal": 0, "supply": 0}                 # microvias: balls whose layer or plane they cannot reach
    from . import stackup
    roles = stackup.board_roles(b, stackup.get(cfg))
    short = lambda n_: str(n_ or "").rsplit("/", 1)[-1]
    for p, r, c, ring in g["balls"]:
        if not p.net or p.net.startswith("unconnected-"):
            continue
        supply = kinds.get(p.net) in ("power", "ground")
        if not supply and ring < pl["top_rings"]:
            continue
        layer = next(l for l in p.layers if l.endswith(".Cu"))
        if method == "dog-bone":
            sx = 1 if p.x >= g["cx"] - 1e-6 else -1
            sy = 1 if p.y >= g["cy"] - 1e-6 else -1
            x, y = p.x + sx * P / 2, p.y + sy * P / 2
        else:
            x, y = p.x, p.y
        if (round(x, 2), round(y, 2)) in have:
            continue
        if method == "microvia":
            down = list(b.copper) if layer == "F.Cu" else list(reversed(b.copper))
            if supply:                                 # stacked microvias down to the ball's own plane (two at most)
                k = next((i for i, l in enumerate(down[1:3], 1) if roles.get(l, {}).get("role") == "plane"
                          and short(roles[l].get("net")) == short(p.net)), None)
            else:                                      # one microvia onto the escape layer under the pad
                k = 1 if roles.get(down[1], {}).get("role") != "plane" else None
            if k is None:
                left["supply" if supply else "signal"] += 1
                continue
            for i in range(k):
                vias.append({"net": p.net, "x": round(x, 4), "y": round(y, 4), "d": h["micro_d"], "drill": h["micro_drill"],
                             "kind": "micro", "layers": [down[i], down[i + 1]]})
        else:
            vias.append({"net": p.net, "x": round(x, 4), "y": round(y, 4), "d": rules["via_d"], "drill": rules["via_drill"]})
        if method == "dog-bone":
            stubs.append({"net": p.net, "layer": layer, "a": [round(p.x, 4), round(p.y, 4)], "b": [round(x, 4), round(y, 4)],
                          "w": rules["w"]})
    if project.pro and (vias or stubs):              # the board's minimums allow what goes down
        from .pro import set_board_rules
        lo = {"min_track_width": rules["w"], "min_clearance": rules["s"]}
        if method != "microvia":
            lo.update(min_via_diameter=rules["via_d"], min_through_hole_diameter=rules["via_drill"])
        set_board_rules(project.pro, lo, lower_only=True)
        if method == "microvia":                      # microvias: a DRC rule of their own (KiCad: board minimums else)
            from .pcb import rules as dru_
            dru_.ensure_rules(project, hdimod.dru_rules(cfg), replace=True)
    ops = []
    from .route.driver import fine_pitch_areas
    from .pcb import rules as dru
    from . import stackup
    routing = stackup.routing_layers(stackup.get(cfg), b.copper)
    area = next((r for rf, r in fine_pitch_areas(b, routing=routing) if rf == ref), None)
    if area and (vias or stubs):                     # DRC holds the escape to the neck-down clearance in here
        x0, y0, x1, y1 = area
        if not any(z.is_rule_area and z.name == f"TW neck {ref}" for z in b.zones):
            ops.append({"op": "rule_area", "name": f"TW neck {ref}", "layers": list(routing),
                        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "no_tracks": False, "no_vias": False,
                        "no_pour": False, "no_footprints": False})
        dru.ensure_rules(project, dict([dru.neck_rule(rules["s"])]), replace=True)
        # the inner planes under the part: at their usual clearance the via field cuts them into islands; a patch of
        # the same net at the neck-down clearance keeps the webs between the vias (it joins the plane it lies on)
        poly = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        for z in b.zones:
            if z.is_rule_area or not z.net or z.name.startswith("TW patch"):
                continue
            for l in z.layers:
                if l in ("F.Cu", "B.Cu") or not z.outline:
                    continue
                zb = geom.bbox([q for pl in z.outline for q in pl])
                if zb[0] <= x0 and zb[1] <= y0 and zb[2] >= x1 and zb[3] >= y1 and \
                        not any(o.name == f"TW patch {ref} {l}" for o in b.zones):
                    ops.append({"op": "zone", "net": z.net, "layers": [l], "polygon": poly, "name": f"TW patch {ref} {l}",
                                "priority": (z.priority or 0) + 1, "clearance": rules["s"], "min_width": min(rules["w"], 0.1),
                                "connect": "solid"})
    if stubs:
        ops.append({"op": "tracks", "items": stubs})
    if vias:
        ops.append({"op": "vias", "items": vias})
    if ops and any(z.net and not z.is_rule_area for z in b.zones):
        ops.append({"op": "fill"})                   # the pours make room for the new copper
    summ = {"ref": ref, "method": method, "vias": len(vias), "stubs": len(stubs),
            "via_in_pad": len(vias) if method in ("via-in-pad", "microvia") else 0}
    if left["signal"]:
        summ["note"] = (f"{left['signal']} signal ball{'s' if left['signal'] > 1 else ''} not fanned out: the layer under the pads "
                        f"is a plane (make it a signal layer for the microvias)")
    if left["supply"]:
        summ["note"] = (summ.get("note", "") + "; " if summ.get("note") else "") + (
            f"{left['supply']} power or ground ball{'s' if left['supply'] > 1 else ''} not fanned out: no plane of theirs within "
            "two microvias; route them on the escape layer to a via outside the ball field")
    return ops, summ
