"""The limits the user set in the requirements (tracewright.json "constraints"), held against the files:
board size, copper layers, thickness and weight, the tallest part (from the 3D models), every part's
temperature rating, and the parts cost (LCSC unit prices). Limits left to Claude are not checked."""
import re, time
from . import check, Finding, NotApplicable, examined
from .. import constraints

TEMP = re.compile(r"([-+−]?\s*\d+(?:\.\d+)?)\s*(?:℃|°\s*C|deg\s*C|C\b)", re.I)


def temp_range(text):
    """(-40, 85) from '-40℃~+85℃', '-55°C ~ +150°C (TJ)', or None."""
    vals = [float(v.replace("−", "-").replace(" ", "")) for v in TEMP.findall(text or "")]
    return (min(vals[:2]), max(vals[:2])) if len(vals) >= 2 else None


@check("req.limits", "The design stays within the limits you set", "Requirements", needs=())
def req_limits(ctx):
    """Each limit set in the requirements' Advanced section, where the files can show it: the board fits
    the largest size (either way round); it has the layers, thickness and copper asked for; no part stands
    taller than allowed (heights from the footprints' 3D models; parts without one are listed as
    unverified); every part is rated for the operating temperature (LCSC's data for its code); the parts
    cost per board stays under the budget (LCSC unit prices). Unset limits are Claude's choice."""
    lim = constraints.get(ctx.cfg)
    checkable = {k: v for k, v in lim.items() if k in ("max_size_mm", "layers", "thickness_mm", "copper_oz",
                                                           "max_height_mm", "assembly_sides", "temp_c", "cost_usd")}
    if not checkable:
        raise NotApplicable("no limits set that the files can show (Claude decides)" if not lim else
                            "the limits set are for Claude and the order, not something the files show")
    out, seen = [], []
    b = ctx.board if ctx.available("pcb") else None
    if b is not None and b.outline:
        w, h = b.size()
        if "max_size_mm" in lim:
            W, H = lim["max_size_mm"]
            seen.append("size")
            fits = (w <= W + 0.05 and h <= H + 0.05) or (w <= H + 0.05 and h <= W + 0.05)
            if not fits:
                out.append(Finding("req.limits", "error", f"the board is {w:.1f} x {h:.1f} mm; the limit is {W:g} x {H:g} mm",
                                   hint="Shrink the outline, or change the limit in the requirements.", key="req:size"))
        if "layers" in lim:
            seen.append("layers")
            if len(b.copper) != lim["layers"]:
                out.append(Finding("req.limits", "error", f"the board has {len(b.copper)} copper layers; the requirements say "
                                   f"{lim['layers']}", key="req:layers"))
        if "thickness_mm" in lim and b.thickness:
            seen.append("thickness")
            if abs(b.thickness - lim["thickness_mm"]) > 0.05:
                out.append(Finding("req.limits", "error", f"the board is {b.thickness:g} mm thick; the requirements say "
                                   f"{lim['thickness_mm']:g} mm", hint="Board Setup > Physical Stackup.", key="req:thickness"))
        if "copper_oz" in lim:
            seen.append("copper")
            cu = b.copper_mm("F.Cu")
            if cu and cu / 0.035 + 0.25 < lim["copper_oz"]:
                out.append(Finding("req.limits", "error", f"outer copper is {cu / 0.035:.1g} oz; the requirements say "
                                   f"{lim['copper_oz']:g} oz", key="req:copper"))
        if "max_height_mm" in lim:
            from .. import models3d
            seen.append("height")
            top, unknown, tallest = lim["max_height_mm"], [], None
            for fp in b.fp_list:
                if fp.side != "F" or fp.dnp or not fp.pads or fp.ref.startswith(("H", "MH", "FID", "TP")) or \
                        any(w in fp.lib_id.lower() for w in ("mountinghole", "fiducial", "testpoint", "logo")):
                    continue
                hh, src = models3d.height(fp, ctx.p)
                if hh is None:
                    unknown.append(fp.ref)
                    continue
                if tallest is None or hh > tallest[0]:
                    tallest = (hh, fp.ref)
                if hh > top + 0.05:
                    out.append(Finding("req.limits", "error", f"{fp.ref} stands {hh:g} mm tall ({src}); the limit is {top:g} mm",
                                       {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"req:height:{fp.ref}"))
            if unknown:
                out.append(Finding("req.limits", "warning", f"height not verified for {len(unknown)} part"
                                   f"{'s' if len(unknown) > 1 else ''} without a 3D model: {', '.join(sorted(unknown)[:12])}"
                                   f"{' ...' if len(unknown) > 12 else ''}", {"ref": sorted(unknown)[0]},
                                   hint="Give their footprints a STEP model (the maker's, or a simple box at the data sheet's "
                                        "height) so the limit can be checked.", key="req:height:unknown"))
        if lim.get("assembly_sides") == "top only":
            seen.append("sides")
            under = sorted(fp.ref for fp in b.fp_list if fp.side == "B" and fp.pads and not fp.dnp
                           and not fp.ref.startswith(("H", "MH", "FID", "TP", "#")))
            if under:
                out.append(Finding("req.limits", "error", f"{len(under)} part{'s' if len(under) > 1 else ''} on the bottom "
                                   f"({', '.join(under[:10])}{' ...' if len(under) > 10 else ''}); the requirements say top only",
                                   {"ref": under[0]}, hint="Move them to the top, or allow both sides in the requirements "
                                   "(two-sided assembly costs more).", key="req:sides"))
    elif any(k in lim for k in ("max_size_mm", "layers", "thickness_mm", "copper_oz", "max_height_mm", "assembly_sides")):
        out.append(Finding("req.limits", "info", "no board yet: size, layers and heights are checked once there is one",
                           key="req:noboard"))
    if ("temp_c" in lim or "cost_usd" in lim) and ctx.available("netlist"):
        from .bom import placed_parts, lcsc_of
        parts = placed_parts(ctx)
        lookups = None
        if ctx.setting("fab.house", "jlcpcb") == "jlcpcb":
            from ..jlc import Parts
            budget = float(ctx.setting("checks.lookup_budget_s", 90) or 90) / 2 if ctx.available("network") else 0.0
            lookups = Parts(ctx.p.root, deadline=time.time() + budget, stop=ctx.stop)     # offline: the cache only
        details, missing = {}, []
        for ref, part in sorted(parts.items()):
            code = lcsc_of(part["fields"], ctx)
            d = None
            if code and lookups is not None:
                try:
                    d = lookups.detail(code, max_age_h=None)
                except Exception:
                    d = None
            if d:
                details[ref] = d
            else:
                missing.append(ref)
        if "temp_c" in lim:
            seen.append("temperature")
            lo, hi = lim["temp_c"]
            unrated = []
            for ref, d in details.items():
                rng = temp_range((d.get("params") or {}).get("Operating Temperature") or "")
                if not rng:
                    unrated.append(ref)
                    continue
                if rng[0] > lo + 0.01 or rng[1] < hi - 0.01:
                    out.append(Finding("req.limits", "error", f"{ref} ({d.get('mpn') or ''}) is rated {rng[0]:g} to {rng[1]:g} °C; "
                                       f"the board must work from {lo:g} to {hi:g} °C", {"ref": ref},
                                       hint="Choose a part rated for the range (industrial parts are usually -40 to 85 °C).",
                                       key=f"req:temp:{ref}"))
            unknown = sorted(set(unrated) | set(missing))
            if unknown:
                passive = [r for r in unknown if r[:1] in ("R", "C", "L") and not r.startswith(("CN", "CON"))]
                why = "" if ctx.available("network") else " (checked offline: only parts already looked up have data)"
                what = (f"{len(passive)} resistors, capacitors and inductors, and " if passive and len(passive) < len(unknown) else
                        "resistors, capacitors and inductors: " if passive else "")
                rest = [r for r in unknown if r not in passive]
                shown = ", ".join((rest or unknown)[:12]) + (" ..." if len(rest or unknown) > 12 else "")
                out.append(Finding("req.limits", "warning", f"temperature rating not known for {len(unknown)} part"
                                   f"{'s' if len(unknown) > 1 else ''}{why}: {what}{shown}",
                                   {"ref": (rest or unknown)[0]}, hint="Run the checks online to look them up, or check them "
                                   "against the data sheets (most passives are rated -55 to 125 °C).", key="req:temp:unknown"))
        if "cost_usd" in lim:
            seen.append("cost")
            total = sum(float(d["price_1"]) for d in details.values() if d.get("price_1"))
            unpriced = [r for r in parts if r not in details or not details[r].get("price_1")]
            if total > lim["cost_usd"]:
                out.append(Finding("req.limits", "warning", f"parts cost about ${total:.2f} per board at LCSC's unit prices; the "
                                   f"budget is ${lim['cost_usd']:g}", hint="The Parts tab's savings finds cheaper equivalents.",
                                   key="req:cost"))
            elif unpriced:
                out.append(Finding("req.limits", "info", f"${total:.2f} per board for the priced parts; {len(unpriced)} have "
                                   "no price, so the total may be higher", key="req:cost:partial"))
    examined(ctx, "limits: " + ", ".join(seen) if seen else "limits set; nothing to hold them against yet")
    return out

