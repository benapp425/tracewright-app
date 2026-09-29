"""KiCad's own checks: ERC, DRC (with zone refill) and schematic <-> board parity."""
from . import check, Finding, examined, plural, NotApplicable

# DRC types that are style advice rather than defects (reported as info)
INFO_TYPES = {"lib_footprint_issues", "lib_footprint_mismatch", "footprint_symbol_field_mismatch",
              "silk_edge_clearance", "text_height", "text_thickness"}
WARN_TYPES = {"silk_over_copper", "silk_overlap", "courtyards_overlap", "missing_courtyard", "isolated_copper",
              "starved_thermal", "creepage", "solder_mask_bridge", "footprint_filters_mismatch"}


def _where(item, extra=None):
    w = {}
    pos = item.get("pos")
    if pos:
        w["x"], w["y"] = pos.get("x"), pos.get("y")
    if item.get("uuid"):
        w["uuid"] = item["uuid"]
    if extra:
        w.update(extra)
    return w


@check("erc", "KiCad electrical rules (ERC)", "KiCad", needs=("erc",))
def erc(ctx):
    """Every ERC violation KiCad reports, at its sheet and position. ERC warnings stay warnings."""
    out = []
    for sh in ctx.erc.get("sheets", []):
        for v in sh.get("violations", []):
            sev = "error" if v.get("severity") == "error" else "warning"
            items = v.get("items", [])
            desc = "; ".join(i.get("description", "") for i in items[:2])
            msg = f"{v.get('description', v.get('type'))}" + (f" -- {desc}" if desc else "")
            out.append(Finding("erc", sev, msg, _where(items[0] if items else {}, {"sheet": sh.get("path", "/"),
                                                                                  "type": v.get("type")}),
                               key=f"erc:{v.get('type')}:{desc}"))
    examined(ctx, plural(len(ctx.erc.get("sheets", [])), "sheet"))
    return out


@check("drc", "KiCad design rules (DRC) + schematic parity", "KiCad", needs=("drc",))
def drc(ctx):
    """DRC violations, unrouted connections and schematic/board differences, as KiCad reports them.
    The custom rules file (.kicad_dru) is read first: KiCad silently ignores the whole file when one
    rule in it has a mistake, so a clean DRC could come from rules that never applied."""
    import os
    from .. import dru as twdru
    out = []
    path = os.path.splitext(ctx.p.pcb)[0] + ".kicad_dru" if ctx.p.pcb else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.strip():
            _, problems = twdru.check(text)
            errs = [p for p in problems if p["severity"] == "error"]
            if errs:
                p = errs[0]
                out.append(Finding("drc", "error", f"custom rules (.kicad_dru) line {p['line']}: {p['message']} -- KiCad ignores the "
                                   "whole file, so none of its rules apply", {"type": "rules"},
                                   hint="Fix it in the Rules tab (it checks the file with KiCad before saving).", key="drc:dru"))
            else:
                ok, why = twdru.kicad_accepts(ctx.p.pcb, text)
                if not ok:
                    out.append(Finding("drc", "error", f"custom rules (.kicad_dru): {why}", {"type": "rules"},
                                       hint="Open the Rules tab: it shows the file with its problems.", key="drc:dru"))
    d = ctx.drc
    for v in d.get("violations", []):
        t = v.get("type", "")
        sev = "info" if t in INFO_TYPES else ("warning" if t in WARN_TYPES or v.get("severity") == "warning" else "error")
        items = v.get("items", [])
        desc = "; ".join(i.get("description", "")[:80] for i in items[:2])
        out.append(Finding("drc", sev, f"{v.get('description', t)} -- {desc}", _where(items[0] if items else {}, {"type": t}),
                           key=f"drc:{t}:{desc}"))
    for v in d.get("unconnected_items", []):
        items = v.get("items", [])
        desc = " to ".join(i.get("description", "")[:60] for i in items[:2])
        out.append(Finding("drc", "error", f"unrouted: {desc}", _where(items[0] if items else {}, {"type": "unconnected"}),
                           key=f"drc:unconnected:{desc}"))
    for v in d.get("schematic_parity", []):
        items = v.get("items", [])
        desc = "; ".join(i.get("description", "")[:80] for i in items[:2])
        out.append(Finding("drc", "error", f"schematic/board mismatch: {v.get('description', '')} {desc}".strip(),
                           _where(items[0] if items else {}, {"type": "parity"}),
                           hint="Update the board from the schematic (Tools > Update PCB, or ./tw sync).",
                           key=f"drc:parity:{v.get('description', '')}:{desc}"))
    examined(ctx, plural(len(ctx.board.nets), "net") + ", " + plural(len(ctx.board.fp_list), "footprint"))
    return out
