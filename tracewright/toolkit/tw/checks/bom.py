"""Parts and BOM: every part that gets bought or placed can be ordered and identified."""
import re, collections
from . import check, Finding

LCSC_RE = re.compile(r"^C\d{2,9}$")


def lcsc_field(ctx):
    return ctx.setting("fab.lcsc_field", "LCSC")


def lcsc_of(fields, ctx):
    names = [lcsc_field(ctx), "LCSC", "LCSC Part", "LCSC Part #", "JLCPCB Part #", "JLC", "LCSC#"]
    for n in names:
        v = (fields or {}).get(n, "").strip()
        if v:
            return v
    return ""


def placed_parts(ctx):
    """{ref: part} for parts that are bought and placed (not DNP, not excluded, not user-installed)."""
    nl = ctx.netlist
    skip = set(ctx.setting("fab.not_assembled", []) or [])
    out = {}
    for ref, p in nl.parts.items():
        if ref.startswith("#") or not p["in_bom"] or p["dnp"] or ref in skip:
            continue
        out[ref] = p
    return out


@check("bom.fields", "Every part has a value, footprint and orderable part number", "Parts & BOM", needs=("netlist",))
def bom_fields(ctx):
    """Missing footprints and values stop the board; missing MPN / LCSC codes stop the order.
    Required fields come from tracewright.json checks.require_fields (default MPN), and the LCSC
    code is required when fab.assembly is on."""
    out = []
    need = ctx.setting("checks.require_fields", ["MPN"]) or []
    want_lcsc = bool(ctx.setting("fab.assembly", True)) and ctx.setting("fab.house", "jlcpcb") == "jlcpcb"
    for ref, p in sorted(placed_parts(ctx).items()):
        w = {"ref": ref}
        if not p["footprint"]:
            out.append(Finding("bom.fields", "error", f"{ref} has no footprint", w, key=f"bom:fp:{ref}"))
        if not p["value"] or p["value"] in ("~", "?"):
            out.append(Finding("bom.fields", "error", f"{ref} has no value", w, key=f"bom:value:{ref}"))
        for f in need:
            v = p["fields"].get(f, "").strip()
            if not v and not (f == "MPN" and (p["fields"].get("Manufacturer Part Number") or p["fields"].get("MFR Part"))):
                out.append(Finding("bom.fields", "warning", f"{ref} ({p['value']}) has no {f}", w,
                                   key=f"bom:field:{f}:{ref}"))
        if want_lcsc:
            code = lcsc_of(p["fields"], ctx)
            if not code:
                out.append(Finding("bom.fields", "warning", f"{ref} ({p['value']}) has no LCSC code for JLC assembly", w,
                                   hint="Add an LCSC field (Cxxxx) or list the part in fab.not_assembled.",
                                   key=f"bom:lcsc:{ref}"))
            elif not LCSC_RE.match(code):
                out.append(Finding("bom.fields", "error", f"{ref}: LCSC code '{code}' is not of the form C12345", w,
                                   key=f"bom:lcscfmt:{ref}"))
    # one MPN, different values or footprints: a copy-paste slip
    by_mpn = collections.defaultdict(set)
    for ref, p in placed_parts(ctx).items():
        m = p["fields"].get("MPN", "").strip()
        if m:
            by_mpn[m].add((p["value"], p["footprint"].split(":")[-1]))
    for m, combos in by_mpn.items():
        fps = {c[1] for c in combos}
        if len(fps) > 1:
            out.append(Finding("bom.fields", "warning", f"MPN {m} is used with different footprints: {', '.join(sorted(fps))}",
                               key=f"bom:mpnfp:{m}"))
    return out


@check("bom.board", "Board and schematic agree on parts", "Parts & BOM", needs=("netlist", "pcb"))
def bom_board(ctx):
    """Footprints whose value, footprint or DNP flag differs from the schematic, duplicate references,
    and parts that exist on only one side (KiCad's parity check also reports some of these)."""
    nl, b = ctx.netlist, ctx.board
    out = []
    refs = collections.Counter(fp.ref for fp in b.fp_list)
    for r, n in refs.items():
        if n > 1 and r and not r.startswith(("#", "REF**", "G***")):
            out.append(Finding("bom.board", "error", f"reference {r} is used by {n} footprints", {"ref": r},
                               key=f"bom.board:dup:{r}"))
    for fp in b.fp_list:
        p = nl.parts.get(fp.ref)
        if p is None:
            if fp.ref and not fp.ref.startswith(("#", "REF**", "G***", "kibuzzard", "LOGO")) and "board_only" not in fp.attrs:
                out.append(Finding("bom.board", "warning", f"{fp.ref} is on the board but not in the schematic",
                                   {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"bom.board:extra:{fp.ref}"))
            continue
        if p["value"] != fp.value:
            out.append(Finding("bom.board", "error", f"{fp.ref}: value '{fp.value}' on the board, '{p['value']}' in the schematic",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"bom.board:value:{fp.ref}"))
        if p["footprint"] and p["footprint"].split(":")[-1] != fp.lib_id.split(":")[-1]:
            out.append(Finding("bom.board", "error",
                               f"{fp.ref}: footprint {fp.lib_id} on the board, {p['footprint']} in the schematic",
                               {"ref": fp.ref, "x": fp.x, "y": fp.y}, key=f"bom.board:fp:{fp.ref}"))
        if p["dnp"] != fp.dnp:
            out.append(Finding("bom.board", "warning", f"{fp.ref}: DNP differs between schematic and board",
                               {"ref": fp.ref}, key=f"bom.board:dnp:{fp.ref}"))
    on_board = {fp.ref for fp in b.fp_list}
    for ref, p in nl.parts.items():
        if ref.startswith("#") or not p.get("on_board", True) or not p["footprint"]:
            continue
        if ref not in on_board:
            out.append(Finding("bom.board", "error", f"{ref} ({p['value']}) is in the schematic but not on the board",
                               {"ref": ref}, hint="Update the board from the schematic.", key=f"bom.board:missing:{ref}"))
    return out
