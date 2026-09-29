"""A project's hard limits (tracewright.json "constraints"): what the board must fit and survive, set by
the user in the requirements' Advanced section. Anything left unset is Claude's to decide. Claude is
told the limits as hard ones, and the requirements check (req.limits) holds the design to those the
files can show: size, layers, thickness, copper, part heights, temperature ratings, parts cost.

    constraints.get(cfg)        {"max_size_mm": [45, 90], "layers": 4, ...}   only what is set
    constraints.validate(d)     cleaned values; ValueError on nonsense
    constraints.describe(cfg)   one line for Claude
"""

# key -> (label, kind, unit, hint, choices)
OPTIONS = {
    "max_size_mm": ("Largest board", "size", "mm", "Width and length the board must fit in (either way round).", None),
    "max_height_mm": ("Tallest part", "number", "mm", "Above the board's top surface: enclosure or bay clearance.", None),
    "layers": ("Copper layers", "choice", "", "", [2, 4, 6]),
    "thickness_mm": ("Board thickness", "choice", "mm", "", [0.8, 1.0, 1.2, 1.6, 2.0]),
    "copper_oz": ("Outer copper", "choice", "oz", "Heavier copper carries more current in the same width.", [1, 2]),
    "input_v": ("Input voltage", "range", "V", "The lowest and highest supply the board is fed.", None),
    "max_input_a": ("Input current", "number", "A", "The most the board draws from its supply, or passes on.", None),
    "temp_c": ("Operating temperature", "range", "°C", "Every part must be rated for it.", None),
    "cost_usd": ("Parts cost per board", "number", "USD", "Parts and assembly at the order quantity.", None),
    "quantity": ("Boards to order", "number", "", "", None),
    "finish": ("Surface finish", "choice", "", "ENIG for fine-pitch parts; HASL is cheaper.", ["HASL (lead free)", "ENIG"]),
    "impedance_control": ("Controlled impedance", "choice", "", "The fab builds to the stack-up's impedances (4+ layers).",
                          ["yes", "no"]),
}
ORDER = list(OPTIONS)


def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: {v!r} is not a number")
    if x != x or x < -1e6 or x > 1e6:
        raise ValueError(f"{what}: {v!r} is out of range")
    return int(x) if x == int(x) and abs(x) < 1e6 else x


def validate(d):
    """Cleaned constraints: sizes and ranges as [a, b], numbers as numbers, choices from their list. A
    value of None or "" removes the limit (back to Claude's choice)."""
    out = {}
    for k, v in (d or {}).items():
        if k not in OPTIONS:
            raise ValueError(f"unknown limit {k}; limits: {', '.join(ORDER)}")
        label, kind, unit, hint, choices = OPTIONS[k]
        if v is None or v == "" or v == []:
            out[k] = None
            continue
        if kind in ("size", "range"):
            if isinstance(v, str):
                v = [x for x in v.replace("x", " ").replace("×", " ").replace("..", " ").replace(",", " ").split() if x]
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ValueError(f"{label}: give two numbers")
            a, b = _num(v[0], label), _num(v[1], label)
            if kind == "size" and (a <= 0 or b <= 0):
                raise ValueError(f"{label}: sizes are positive")
            if kind == "range" and a > b:
                a, b = b, a
            out[k] = [a, b]
        elif kind == "number":
            x = _num(v, label)
            if x <= 0:
                raise ValueError(f"{label}: must be more than 0")
            out[k] = x
        else:
            if isinstance(v, bool):
                v = "yes" if v else "no"
            got = next((c for c in choices if str(c).lower() == str(v).strip().lower() or
                        (isinstance(c, (int, float)) and _is_num(v) and float(v) == float(c))), None)
            if got is None:
                raise ValueError(f"{label}: one of {', '.join(str(c) for c in choices)}")
            out[k] = got
    return out


def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def get(cfg):
    """The limits the project sets (unset ones are absent)."""
    raw = (cfg or {}).get("constraints") or {}
    try:
        return {k: v for k, v in validate(raw).items() if v is not None}
    except ValueError:
        out = {}
        for k, v in raw.items():
            try:
                out.update({kk: vv for kk, vv in validate({k: v}).items() if vv is not None})
            except ValueError:
                continue
        return out


def merge(cfg, changes):
    """cfg["constraints"] updated with changes (None removes a limit); returns the new limits."""
    clean = validate(changes)
    cur = dict((cfg.get("constraints") or {}))
    for k, v in clean.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    if cur:
        cfg["constraints"] = cur
    else:
        cfg.pop("constraints", None)
    return get(cfg)


def fmt(k, v):
    label, kind, unit, hint, choices = OPTIONS[k]
    u = f" {unit}" if unit and unit != "°C" else unit
    if kind == "size":
        return f"{label.lower()} {v[0]:g} x {v[1]:g} mm"
    if kind == "range":
        return f"{label.lower()} {v[0]:g} to {v[1]:g}{u}"
    if kind == "choice" and k == "impedance_control":
        return "controlled impedance" if v == "yes" else "no impedance control"
    if isinstance(v, (int, float)):
        return f"{label.lower()} {'at most ' if k in ('max_height_mm', 'cost_usd', 'max_input_a') else ''}{v:g}{u}"
    return f"{label.lower()} {v}"


def describe(cfg):
    """'Hard limits: largest board 45 x 90 mm, tallest part at most 9 mm, ...' or ''."""
    c = get(cfg)
    if not c:
        return ""
    return "Hard limits the user set (don't exceed them; say so if one can't be met): " + \
        "; ".join(fmt(k, c[k]) for k in ORDER if k in c) + "."
