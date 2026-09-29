"""The net model's own check: every net has a kind the other checks can use, and what the project
declared about its nets refers to nets that exist."""
from . import check, Finding, NotApplicable, examined
from .. import netmodel


@check("nets.model", "Every net is known: supplies with a voltage, pairs with a partner", "Schematic")
def nets_model(ctx):
    """Reads the net model (tracewright.json "nets", what the schematic generator declared, older checks
    settings, and what names and pins say). A declaration that names no net is a typo or a stale name, so
    whatever it declares is silently lost; a supply with no known voltage leaves the capacitor-rating and
    level checks blind; a pair whose declared partner is missing cannot be routed or checked as a pair."""
    if not (ctx.available("netlist") or ctx.available("pcb")):
        raise NotApplicable("no schematic or board yet")
    m = ctx.nets
    if not m.records:
        raise NotApplicable("no nets yet")
    out = []
    for key, src in m.unused_keys():
        out.append(Finding("nets.model", "warning", f"{key} is declared in {src} but no net is named that",
                           {"net": key}, hint="Rename it to the net's name as the netlist shows it (the short name, like "
                                              "VPYRO, is enough), or remove it.", key=f"nets:unused:{key}"))
    for n, r in sorted(m.records.items()):
        s = netmodel.short(n)
        if r["kind"] == "power" and r.get("voltage") is None:
            out.append(Finding("nets.model", "warning", f"{s} is a supply with no known voltage: capacitor ratings and "
                               "voltage-level checks skip it", {"net": s},
                               hint=f'Declare it: "nets": {{"{s}": {{"kind": "power", "voltage": 3.3}}}} in tracewright.json, '
                                    "or the nets tool.", key=f"nets:novolt:{s}"))
        src_kind = (r.get("source") or {}).get("kind")
        if r["kind"] != "power" and (r.get("current") or 0) >= 1.0 and src_kind == "inferred":
            out.append(Finding("nets.model", "warning", f"{s} carries {r['current']:g} A but reads as a {netmodel.KIND_LABEL[r['kind']]}: "
                               "say what it is so the checks treat it right", {"net": s},
                               hint=f'A supply: "nets": {{"{s}": {{"kind": "power", "voltage": ..., "current": {r["current"]:g}}}}}; '
                                    'a switched load line (a motor, heater or e-match output): kind "signal" with its current.',
                               key=f"nets:loaded:{s}"))
        if r["kind"] == "pair":
            mate = r.get("pair")
            if not mate or not m.full(mate):
                out.append(Finding("nets.model", "error", f"{s} is declared as one half of a pair, but its partner "
                                   f"{mate or '(not named)'} is not a net", {"net": s},
                                   hint="Name the partner with \"pair\", or fix the net's name.", key=f"nets:nomate:{s}"))
            elif m.kind(mate) != "pair":
                out.append(Finding("nets.model", "warning", f"{s} is a pair half but its partner {mate} is not marked as one",
                                   {"net": s}, key=f"nets:halfpair:{s}"))
    examined(ctx, m.summary())
    return out
