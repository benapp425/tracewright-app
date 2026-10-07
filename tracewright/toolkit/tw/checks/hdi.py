"""Dense boards: the vias match the build the fab is asked for (HDI or not), microvias are ones a laser can make, and every
ball-grid part has a way for its pins to get out (tw/escape.py)."""
from . import check, Finding, NotApplicable, examined, plural
from .. import geom


def _in_pad(b, v):
    """The small SMD pad a via sits in (a ball, a pin, a passive's end), or None. Thermal vias in an exposed pad are
    the usual practice and are not counted."""
    for fp in b.fp_list:
        bb = fp.bbox()
        if not (bb[0] - 0.5 <= v.x <= bb[2] + 0.5 and bb[1] - 0.5 <= v.y <= bb[3] + 0.5):
            continue
        for p in fp.pads:
            if p.kind != "smd" or min(p.w, p.h) >= 1.0 or (p.net and v.net and p.net != v.net):
                continue
            if any(geom.inside((v.x, v.y), pl) for pl in p.polys if len(pl) >= 3):
                return fp.ref, p
    return None


def _depth(plan, a, b, copper):
    """The dielectric a via between copper layers a and b goes through (mm), from the stack-up's build."""
    from ..stackup import PRESETS
    if not plan or plan.get("preset") not in PRESETS:
        return None
    build = PRESETS[plan["preset"]]["build"]
    i, j = sorted((copper.index(a), copper.index(b)))
    seen, depth = 0, 0.0
    for item in build:
        if item[0] == "copper":
            seen += 1
            continue
        if i < seen <= j:
            depth += item[1]
    return depth


@check("hdi.vias", "Vias the fab can build", "Manufacturing", needs=("pcb",))
def hdi_vias(ctx):
    """With HDI off the board must have through vias only, none in a pad (solder runs down an open via); with HDI on,
    microvias join adjacent layers only, are no deeper than they are wide (a laser via plates badly past 1:1), and stack
    two deep at most, and the fab notes ask for vias in pads to be filled and capped."""
    from .. import hdi as hdimod, stackup
    b = ctx.board
    if not b.vias:
        raise NotApplicable("no vias")
    cfg = getattr(ctx.p, "cfg", None) or {}
    h = hdimod.get(cfg)
    plan = stackup.get(cfg)
    if plan and len(b.copper) != plan.get("layers"):
        plan = None
    out = []
    kinds = {}
    for v in b.vias:
        kinds.setdefault(v.kind or "through", []).append(v)
    hdi_vias = [v for k, vs in kinds.items() if k != "through" for v in vs]
    in_pad = []
    for v in b.vias:
        hit = _in_pad(b, v)
        if hit:
            in_pad.append((v, hit))
    if not h["on"]:
        if hdi_vias:
            word = {"micro": "microvia", "blind": "blind via", "buried": "buried via"}
            names = " and ".join(plural(len(vs), word.get(k, k + " via")) for k, vs in sorted(kinds.items()) if k != "through")
            v = hdi_vias[0]
            out.append(Finding("hdi.vias", "error", f"{names}, but HDI is off",
                               {"x": v.x, "y": v.y}, key="hdi:off",
                               hint="A standard build drills through vias only. Turn HDI on (Board > Routing; the fab charges "
                                    "more), or replace them with through vias."))
        if in_pad:
            v, (ref, p) = in_pad[0]
            out.append(Finding("hdi.vias", "warning", f"{plural(len(in_pad), 'via')} in pads ({ref} pin {p.num}"
                               + (f" and {len(in_pad) - 1} more" if len(in_pad) > 1 else "") + ")",
                               {"ref": ref, "x": v.x, "y": v.y}, key="hdi:vip-off",
                               hint="Solder runs down an open via in a pad. Move the via beside the pad (a short stub), or turn on "
                                    "HDI's vias in pads so the fab fills and caps them."))
    else:
        deep_ones = {}
        for v in kinds.get("micro", []):
            ls = [l for l in v.layers if l in b.copper]
            if len(ls) == 2 and abs(b.copper.index(ls[0]) - b.copper.index(ls[1])) != 1:
                out.append(Finding("hdi.vias", "error", f"a microvia from {ls[0]} to {ls[1]} skips a layer",
                                   {"x": v.x, "y": v.y}, key=f"hdi:span:{v.x:.2f}:{v.y:.2f}",
                                   hint="A laser via joins neighbouring layers only: stack two, or use a blind via."))
                continue
            if v.drill + 1e-6 < h["micro_drill"] or v.d + 1e-6 < h["micro_d"]:
                out.append(Finding("hdi.vias", "error", f"a {v.d:g}/{v.drill:g} mm microvia, smaller than the build's "
                                   f"{h['micro_d']:g}/{h['micro_drill']:g} mm", {"x": v.x, "y": v.y}, key=f"hdi:size:{v.x:.2f}:{v.y:.2f}",
                                   hint="Use the microvia size set for the board (HDI settings)."))
            depth = _depth(plan, ls[0], ls[1], b.copper) if len(ls) == 2 else None
            if depth and depth / max(v.drill, 1e-6) > hdimod.MAX_ASPECT + 1e-6:
                deep_ones.setdefault((ls[0], ls[1], depth, v.drill), []).append(v)
        for (la, lb, depth, drill), vs in deep_ones.items():
            out.append(Finding("hdi.vias", "warning", f"{plural(len(vs), 'microvia')} {la} to {lb}: {depth:.3f} mm deep with a "
                               f"{drill:g} mm hole ({depth / drill:.2f}:1)", {"x": vs[0].x, "y": vs[0].y}, key=f"hdi:aspect:{la}:{lb}",
                               hint="Laser vias plate reliably to 1:1: a wider hole, or a thinner dielectric under the outer layer "
                                    "(ask the fab for a thinner prepreg there)."))
        stacks = {}
        for v in kinds.get("micro", []):
            stacks.setdefault((round(v.x, 3), round(v.y, 3)), []).append(v)
        deep = [k for k, vs in stacks.items() if len(vs) > 2]
        if deep:
            out.append(Finding("hdi.vias", "warning", f"microvias stacked more than two deep at {plural(len(deep), 'spot')}",
                               {"x": deep[0][0], "y": deep[0][1]}, key="hdi:stack",
                               hint="Stacks deeper than two crack under thermal cycling: stagger them, or use a buried via under the pair."))
        if in_pad and not h["via_in_pad"]:
            v, (ref, p) = in_pad[0]
            out.append(Finding("hdi.vias", "warning", f"{plural(len(in_pad), 'via')} in pads, but vias in pads are off in the HDI settings",
                               {"ref": ref, "x": v.x, "y": v.y}, key="hdi:vip-unset",
                               hint="Turn on vias in pads so the fab notes ask for them to be filled and capped."))
        elif in_pad:
            out.append(Finding("hdi.vias", "info", f"{plural(len(in_pad), 'via')} in pads: the fab notes ask for them filled and capped",
                               {}, key="hdi:vip"))
        if not hdi_vias and not in_pad:
            out.append(Finding("hdi.vias", "info", "HDI is on, but the board has no microvias, blind vias or vias in pads",
                               {}, key="hdi:unused", hint="Turn HDI off and the fab builds it as a standard board, for less."))
    examined(ctx, plural(len(b.vias), "via") + (f", {len(hdi_vias)} HDI" if hdi_vias else "") + (f", {len(in_pad)} in pads" if in_pad else ""))
    return out


@check("hdi.fanout", "Dense parts have a way out", "Routing", needs=("pcb",))
def hdi_fanout(ctx):
    """Every ball-grid part: a via fits between its balls (or HDI is on to put them in the pads), and the board has the
    signal layers its rings of signals need to escape -- the usual estimate: the outer ring escapes straight out and each
    track between two balls or two vias takes out one more ring on that layer."""
    from .. import escape
    plans = [pl for pl in escape.plan(ctx.p, ctx.board) if pl["kind"] == "array"]
    if not plans:
        raise NotApplicable("no ball-grid parts")
    out = []
    for pl in plans:
        if pl["method"] is None:
            out.append(Finding("hdi.fanout", "error", f"{pl['ref']}: no via fits between its balls ({pl['pitch']:g} mm pitch)",
                               {"ref": pl["ref"]}, key=f"fanout:{pl['ref']}:via",
                               hint=(pl["hdi"] or "") + ". Turn HDI on (vias in the pads), or choose a coarser package."))
        elif pl["need"] > pl["have"]:
            out.append(Finding("hdi.fanout", "warning", f"{pl['ref']} needs about {pl['need']} signal layers to get its "
                               f"{pl['signals']} signals out; the board has {pl['have']}", {"ref": pl["ref"]},
                               key=f"fanout:{pl['ref']}:layers",
                               hint="More signal layers (the stack-up)" + (", or microvias (HDI: " + pl["hdi"] + ")" if pl.get("hdi") else "")
                                    + "; or fewer signals in the inner rings (unused balls left unconnected)."))
    examined(ctx, plural(len(plans), "ball-grid part"))
    return out
