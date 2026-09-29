"""Assembly by hand: when you build the board yourself (sourcing "self", fab.assembly off), the parts
that are hard or impossible to solder with an iron, what to order to make it easier (a stencil, a
hotplate), and roughly how long the soldering takes."""
import re
from . import check, Finding, NotApplicable, examined, plural

TOO_SMALL = re.compile(r"(?<!\d)(0201|01005|008004)(?!\d)")
SMALL = re.compile(r"(?<!\d)0402(?!\d)")
BALLS = re.compile(r"\b(BGA|[A-Z]*BGA|WLCSP|CSP|DSBGA|XBGA|UCSP)\b|BGA", re.I)
LGA = re.compile(r"\bLGA\b|LGA-", re.I)
NO_LEADS = re.compile(r"\b(QFN|DFN|VQFN|WQFN|TQFN|UQFN|VDFN|WDFN|UDFN|SON|VSON|WSON|USON|TSON|MLF|MLP|HVQFN|PQFN)\b|QFN|DFN", re.I)
PITCH = re.compile(r"P(0\.\d+)mm", re.I)


def _package(fp):
    return f"{fp.lib_id.split(':')[-1]} {fp.value}"


@check("assembly.hand", "Parts you can solder by hand", "Assembly", needs=("pcb",))
def hand_assembly(ctx):
    """Only when you assemble the board yourself: 0201 and smaller, BGAs and wafer-level packages
    (an iron cannot reach under them) are errors; 0402, LGAs, QFN/DFNs with a center pad and pitches
    under 0.5 mm are warnings; the stencil, the two sides and the soldering time are notes."""
    if ctx.setting("fab.assembly", True):
        raise NotApplicable("the fab assembles this board (JLC turnkey); in self-assembly mode this checks what you can solder by hand")
    b = ctx.board
    out, smd_pads, tht_pads, sides, stencil = [], 0, 0, set(), []
    tiny, small, balls, lga, leadless, fine = [], [], [], [], [], []
    for fp in b.fp_list:
        if not fp.pads or getattr(fp, "dnp", False) or "exclude_from_bom" in fp.attrs or "board_only" in fp.attrs:
            continue
        name = _package(fp)
        smd = [p for p in fp.pads if p.kind == "smd"]
        smd_pads += len(smd)
        tht_pads += sum(1 for p in fp.pads if p.kind in ("thru_hole", "through_hole"))
        if smd:
            sides.add(fp.side)
        if TOO_SMALL.search(name):
            tiny.append(fp.ref)
        elif SMALL.search(name):
            small.append(fp.ref)
        if BALLS.search(name):
            balls.append(fp.ref)
        elif LGA.search(name):
            lga.append(fp.ref)
        elif NO_LEADS.search(name) and smd:
            big = max(smd, key=lambda p: p.w * p.h)
            if len(smd) > 2 and big.w * big.h > 4 * min(p.w * p.h for p in smd):      # a center (thermal) pad
                leadless.append(fp.ref)
        m = PITCH.search(name)
        if m and float(m.group(1)) < 0.5:
            fine.append((fp.ref, float(m.group(1))))
    refs = lambda rs: ", ".join(rs[:12]) + (f" +{len(rs) - 12}" if len(rs) > 12 else "")
    if tiny:
        out.append(Finding("assembly.hand", "error", f"too small to solder by hand: {refs(tiny)} (0201 or smaller)",
                           {"refs": tiny}, hint="Use 0402 or larger in self-assembly, or have the fab assemble the board.",
                           key="assembly:tiny"))
    if balls:
        out.append(Finding("assembly.hand", "error", f"ball-grid packages need reflow and cannot be checked by eye: {refs(balls)}",
                           {"refs": balls}, hint="Pick a leaded or QFN version, or have the fab assemble these.", key="assembly:bga"))
    if small:
        out.append(Finding("assembly.hand", "warning", f"0402 parts: {refs(small)} -- doable with fine tweezers and magnification",
                           {"refs": small}, hint="0603 is much easier by hand where space allows.", key="assembly:0402"))
        stencil += small
    if lga:
        out.append(Finding("assembly.hand", "warning", f"land-grid packages (pads underneath): {refs(lga)} -- solder paste and hot air or a hotplate",
                           {"refs": lga}, key="assembly:lga"))
        stencil += lga
    if leadless:
        out.append(Finding("assembly.hand", "warning", f"no-lead packages with a center pad: {refs(leadless)} -- the center pad needs paste "
                           "and hot air or a hotplate (an iron cannot reach it)", {"refs": leadless},
                           hint="Order the stencil with the boards; a via in the center pad lets you solder it from the back.",
                           key="assembly:qfn"))
        stencil += leadless
    if fine:
        out.append(Finding("assembly.hand", "warning", "fine pitch: " + ", ".join(f"{r} ({p:g} mm)" for r, p in fine[:10])
                           + " -- drag-solder with flux, then check for bridges under magnification",
                           {"refs": [r for r, _ in fine]}, key="assembly:pitch"))
    if stencil or smd_pads > 40:
        out.append(Finding("assembly.hand", "info", f"order the stencil with the boards ({smd_pads} SMD pads"
                           + (f"; paste makes {refs(sorted(set(stencil)))} practical" if stencil else "") + ")", key="assembly:stencil"))
    if len(sides) > 1:
        out.append(Finding("assembly.hand", "info", "SMD parts on both sides: solder the side with the smaller, lighter parts first",
                           key="assembly:sides"))
    minutes = (smd_pads * 10 + tht_pads * 12) / 60          # placing and soldering, with a little inspection
    if smd_pads + tht_pads:
        out.append(Finding("assembly.hand", "info", f"about {smd_pads + tht_pads} joints ({smd_pads} SMD, {tht_pads} through-hole): "
                           f"roughly {max(0.25, round(minutes / 60 * 4) / 4):g} h of soldering per board", key="assembly:time"))
    examined(ctx, plural(len(b.fp_list), "footprint"))
    return out
