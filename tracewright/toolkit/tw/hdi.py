"""HDI: the build options for dense boards -- vias in pads (filled and capped), laser microvias between the outer layer
and the one under it, blind vias. Off unless the user turns it on: it costs more and fewer fabs build it. Claude offers
it when a part cannot escape without it (tw/escape.py: a 0.5 mm BGA, more rows than the layers can take).

    tracewright.json "hdi": {"on": false, "via_in_pad": true, "microvias": true, "blind": false,
                             "micro_d": 0.25, "micro_drill": 0.1}

    get(cfg) -> the settings, filled in; on(cfg) -> bool
    validate(d) -> cleaned settings (ValueError on nonsense)
    micro_spans(n) -> [("F.Cu", "In1.Cu"), ("In{n-2}.Cu", "B.Cu")]: where microvias may go
    aspect(plan, drill) -> {span: depth / drill}: a laser via deeper than its width does not plate reliably
    describe(cfg) -> one line
"""

DEFAULT = {"on": False, "via_in_pad": True, "microvias": True, "blind": False, "micro_d": 0.25, "micro_drill": 0.1}
MAX_ASPECT = 1.0           # a laser microvia's depth over its drill (IPC-2226: 1:1 at most, 0.75:1 is comfortable)


def get(cfg):
    d = (cfg or {}).get("hdi")
    if not isinstance(d, dict):                       # 1.1 betas kept a bare flag under route
        d = {"on": bool(((cfg or {}).get("route") or {}).get("hdi"))}
    return {**DEFAULT, **{k: v for k, v in d.items() if k in DEFAULT}}


def on(cfg):
    return bool(get(cfg)["on"])


def validate(d):
    out = dict(DEFAULT)
    for k, v in (d or {}).items():
        if k not in DEFAULT:
            raise ValueError(f"hdi: no setting {k!r} ({', '.join(DEFAULT)})")
        if k in ("micro_d", "micro_drill"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"hdi {k}: a number in mm")
            if not 0.05 <= v <= 0.5:
                raise ValueError(f"hdi {k}: {v:g} mm is not a microvia size (0.05 to 0.5 mm)")
        else:
            v = bool(v)
        out[k] = v
    if out["micro_drill"] >= out["micro_d"]:
        raise ValueError("hdi: the microvia's pad must be wider than its drill")
    return out


def micro_spans(n):
    """Microvias join an outer layer to the one under it (one dielectric)."""
    if n < 4:
        return []
    return [("F.Cu", "In1.Cu"), (f"In{n - 2}.Cu", "B.Cu")]


def aspect(plan, drill):
    """{"F.Cu-In1.Cu": ratio, ...} from the plan's build: the dielectric under each outer layer over the drill."""
    from .stackup import PRESETS
    if not plan or plan.get("preset") not in PRESETS:
        return {}
    build = PRESETS[plan["preset"]]["build"]
    diel = [x[1] for x in build if x[0] != "copper"]
    if len(diel) < 2:
        return {}
    a, b = micro_spans(plan["layers"])
    return {f"{a[0]}-{a[1]}": round(diel[0] / drill, 2), f"{b[0]}-{b[1]}": round(diel[-1] / drill, 2)}


def describe(cfg):
    h = get(cfg)
    if not h["on"]:
        return "HDI off: through vias only, none in pads"
    bits = []
    if h["via_in_pad"]:
        bits.append("vias in pads (filled and capped)")
    if h["microvias"]:
        bits.append(f"microvias {h['micro_d']:g}/{h['micro_drill']:g} mm between adjacent layers (stacked two deep at most)")
    if h["blind"]:
        bits.append("blind vias")
    return "HDI on: " + (", ".join(bits) or "no HDI features chosen")


def dru_rules(cfg):
    """Custom DRC rules for the HDI vias: KiCad holds every via to the board's through-via minimums unless a rule for
    its type says otherwise. {name: rule text}."""
    h = get(cfg)
    if not h["on"]:
        return {}
    d, dr = h["micro_d"], h["micro_drill"]
    out = {"tw microvias": f'''# Tracewright: laser microvias (HDI), their own sizes
(rule "tw microvias"
  (condition "A.Via_Type == 'Micro'")
  (constraint via_diameter (min {d:g}mm))
  (constraint hole_size (min {dr:g}mm))
  (constraint annular_width (min {(d - dr) / 2:g}mm)))'''}
    if h["blind"]:
        out["tw blind vias"] = f'''# Tracewright: blind and buried vias (HDI), laser or controlled-depth drilled
(rule "tw blind vias"
  (condition "A.Via_Type == 'Blind' || A.Via_Type == 'Buried'")
  (constraint via_diameter (min {max(d, 0.3):g}mm))
  (constraint hole_size (min {max(dr, 0.15):g}mm))
  (constraint annular_width (min 0.075mm)))'''
    return out


def fab_notes(cfg, plan=None):
    """What the fab drawing must say for the HDI features in use."""
    h = get(cfg)
    if not h["on"]:
        return []
    out = []
    if h["via_in_pad"]:
        out.append("Vias in pads: fill with non-conductive epoxy and plate over (VIPPO), flush with the pad.")
    if h["microvias"]:
        out.append(f"Laser microvias {h['micro_d']:g} mm pad / {h['micro_drill']:g} mm hole between adjacent layers; "
                   "stacked microvias copper-filled.")
    if h["blind"]:
        out.append("Blind vias as drawn in the drill files (one file per layer pair).")
    return out
