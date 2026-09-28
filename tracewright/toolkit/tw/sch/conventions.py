"""A project's schematic conventions (tracewright.json "schematic"): how its sheets are joined, how
supplies and decoupling are drawn, how nets, pairs and parts are named, which paper and value notation
it uses. The layout engine (tw.sch.auto), the style converter and the checks follow them, and Claude
is told them; each board can differ.

    conventions.get(cfg)        {"style": "hierarchical", "supplies": "symbols", ...}
    conventions.validate(d)     only known keys and choices
    conventions.describe(cfg)   one line for Claude
"""

OPTIONS = {
    "style": {"label": "Sheets joined by", "default": "hierarchical",
              "choices": [("hierarchical", "Sheet pins", "Signals cross sheets through sheet pins and hierarchical labels; the parent sheet shows what goes where."),
                          ("flat", "Global labels", "Pages joined by global labels of the same name.")]},
    "supplies": {"label": "Supplies drawn as", "default": "symbols",
                 "choices": [("symbols", "Power symbols", "A power symbol at each pin: supplies up, ground down."),
                             ("labels", "Global labels", "A global label names the rail at each pin (ground stays a symbol).")]},
    "decoupling": {"label": "Decoupling capacitors", "default": "at_pin",
                   "choices": [("at_pin", "At the pin", "Each capacitor drawn at the power pin it serves, the way it is placed."),
                               ("row", "In a row", "The part's capacitors together in a row beside it, joined by the rail's name.")]},
    "active_low": {"label": "Active-low nets", "default": "_N",
                   "choices": [("_N", "RESET_N", "A trailing _N."), ("N_prefix", "NRESET", "A leading N."),
                               ("#", "RESET#", "A trailing #."), ("overbar", "~{RESET}", "KiCad's overbar in the label.")]},
    "pairs": {"label": "Differential pairs", "default": "_P/_N",
              "choices": [("_P/_N", "D_P / D_N", "USB_D_P and USB_D_N."), ("+/-", "D+ / D-", "USB_D+ and USB_D-."),
                          ("DP/DM", "DP / DM", "USB_DP and USB_DM.")]},
    "designators": {"label": "Reference numbers", "default": "per_sheet",
                    "choices": [("per_sheet", "By sheet", "R101 on sheet 1, R201 on sheet 2."),
                                ("sequential", "Sequential", "R1, R2, R3 across the whole design.")]},
    "values": {"label": "Values written", "default": "iec",
               "choices": [("iec", "4k7, 100n", "IEC 60062: the multiplier in place of the decimal point."),
                           ("decimal", "4.7k, 100nF", "Decimals with the unit.")]},
    "paper": {"label": "Sheet size", "default": "A4",
              "choices": [("A4", "A4", "Grows to A3 when a sheet is full."), ("A3", "A3", "For dense sheets."),
                          ("USLetter", "Letter", "US letter."), ("B", "Tabloid", "US B (11 x 17 in).")]},
    "notes": {"label": "Notes on the sheets", "default": "design",
              "choices": [("design", "Design notes", "A line or two beside the parts that need it: why a value, a rating, a layout constraint."),
                          ("minimal", "Only essentials", "General notes on the cover; nothing else unless it is needed to build the board.")]},
}


def get(cfg):
    s = (cfg or {}).get("schematic") or {}
    out = {}
    for k, o in OPTIONS.items():
        v = s.get(k)
        out[k] = v if v in [c[0] for c in o["choices"]] else o["default"]
    return out


def validate(d):
    out = {}
    for k, v in (d or {}).items():
        o = OPTIONS.get(k)
        if o and v in [c[0] for c in o["choices"]]:
            out[k] = v
    return out


def chosen(cfg):
    """The conventions the project set explicitly (the rest are defaults)."""
    s = (cfg or {}).get("schematic") or {}
    return {k: v for k, v in validate(s).items()}


def describe(cfg):
    c = get(cfg)
    label = lambda k: next(t for v, t, _ in OPTIONS[k]["choices"] if v == c[k])
    return (f"sheets joined by {label('style').lower()} ({c['style']}); supplies as {label('supplies').lower()}; decoupling "
            f"{label('decoupling').lower()}; active-low nets like {label('active_low')}; pairs like {label('pairs')}; "
            f"references {label('designators').lower()}; values like {label('values')}; {c['paper']} sheets; "
            f"notes: {label('notes').lower()}")
