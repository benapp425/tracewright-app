"""Assembly: the CPL puts JLC's own footprint on our pads (rotation, origin, polarity)."""
import os, csv, time, collections
from . import check, Finding, NotApplicable

SKIP_DIRS = {"build", ".git", ".tracewright", "sourcing", "tools", "node_modules", "libraries", "3dmodels", ".claude",
             "__pycache__", "backups", "trash"}
_COLS = {
    "ref": ("designator", "ref", "reference", "refdes", "ref des"),
    "x": ("mid x", "posx", "pos x", "center-x(mm)", "center x", "centerx", "x", "locationx"),
    "y": ("mid y", "posy", "pos y", "center-y(mm)", "center y", "centery", "y", "locationy"),
    "rot": ("rotation", "rot", "angle", "rotate"),
    "layer": ("layer", "side", "tb"),
}


def _columns(header):
    names = [h.strip().lstrip("﻿").strip().lower() for h in header]
    out = {}
    for key, aliases in _COLS.items():
        for i, n in enumerate(names):
            if n in aliases:
                out[key] = i
                break
    return out if {"ref", "x", "y", "rot"} <= set(out) else None


def _mm(v):
    v = str(v).strip().lower()
    if v.endswith("mil"):
        return float(v[:-3]) * 0.0254
    return float(v.rstrip("m").strip())


def read_cpl(path):
    """{ref: row with JLC's column names} from a pick-and-place CSV of any common layout (JLC's,
    KiCad's pos export, the JLC and Fabrication Toolkit plugins); None when it is not one."""
    try:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except OSError:
        return None
    if not rows:
        return None
    cols = _columns(rows[0])
    if not cols:
        return None
    out = {}
    for r in rows[1:]:
        if len(r) <= max(cols.values()) or not r[cols["ref"]].strip():
            continue
        try:
            x, y, rot = _mm(r[cols["x"]]), _mm(r[cols["y"]]), float(r[cols["rot"]])
        except ValueError:
            continue
        side = r[cols["layer"]].strip().lower() if "layer" in cols else "top"
        out[r[cols["ref"]].strip()] = {"Designator": r[cols["ref"]].strip(), "Mid X": f"{x:.4f}mm", "Mid Y": f"{y:.4f}mm",
                                       "Rotation": f"{rot:.3f}",
                                       "Layer": "Bottom" if side.startswith(("b", "back")) else "Top"}
    return out or None


def project_cpl_files(root, limit=4):
    """Pick-and-place files the project carries itself (not Tracewright's build/), newest first."""
    found = []
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and depth < 3]
        for f in files:
            if f.lower().endswith(".csv"):
                p = os.path.join(dirpath, f)
                try:
                    with open(p, encoding="utf-8-sig", errors="replace") as fh:
                        head = fh.readline()
                except OSError:
                    continue
                if _columns(next(csv.reader([head]), [])):
                    found.append(p)
    found.sort(key=lambda p: -os.path.getmtime(p))
    return found[:limit]


def _sources(ctx, o, parts):
    """[(label, {ref: row}, rel path or None)]: the CPL files to check."""
    root = ctx.p.root
    named = ctx.setting("fab.cpl")
    if named:
        out = []
        for rel in ([named] if isinstance(named, str) else named):
            rows = read_cpl(os.path.join(root, rel))
            out.append((rel, rows or {}, rel))
        return out
    own = project_cpl_files(root)
    if own:
        return [(os.path.relpath(p, root), read_cpl(p) or {}, os.path.relpath(p, root)) for p in own]
    rows, _ = o.cpl_rows(parts)
    return [("the CPL ./tw fab makes", {r["Designator"]: r for r in rows}, None)]


@check("cpl.jlc", "Pick-and-place matches JLC's footprints", "Assembly", needs=("pcb", "netlist", "network", "assembly"),
       timeout=300)
def cpl_jlc(ctx):
    """JLC places each part by its own footprint for the LCSC code, so KiCad's rotation is often
    wrong by 90/180 degrees and some origins are shifted. JLC's footprint is fitted onto our pads by
    pad number (LEDs and diodes by pin name: JLC numbers the anode 1) and every placement that would
    land turned, shifted or reversed is reported.

    The CPL checked is the one the project ships: tracewright.json fab.cpl when set, else the
    pick-and-place CSVs the project carries (an imported design's own), else the one `./tw fab`
    makes. `./tw cpl --write` stores corrections that the fab outputs apply.

    JLC's footprints are fetched from EasyEDA (cached in sourcing/cache). The lookups share a time
    budget (checks.lookup_budget_s, default 90 s); a part whose footprint could not be fetched is
    reported as not verified -- an error until the check runs again with the service answering."""
    from ..outputs import Outputs
    from ..jlc import Parts, fit_placements
    if ctx.setting("fab.house", "jlcpcb") != "jlcpcb":
        raise NotApplicable(f"the fab is {ctx.setting('fab.house')}, not JLCPCB")
    o = Outputs(ctx.p)
    parts = o.parts(ctx.netlist)
    placed = {p["ref"] for p in parts if p["placed"]}
    lcsc = {p["ref"]: p["lcsc"] for p in parts}
    budget = float(ctx.setting("checks.lookup_budget_s", 90) or 90)
    lookups = Parts(ctx.p.root, deadline=time.time() + budget, stop=ctx.stop)
    sources = _sources(ctx, o, parts)
    out = []
    if len(sources) > 1 and not ctx.setting("fab.cpl"):
        out.append(Finding("cpl.jlc", "info", f"the project carries {len(sources)} pick-and-place files ("
                           + ", ".join(s[0] for s in sources) + "): each is checked",
                           hint='Set "fab": {"cpl": "<the file you upload>"} in tracewright.json to check only that one.',
                           key="cpl:many"))
    unverified = collections.defaultdict(set)                 # code -> refs whose JLC footprint did not come
    why = {}
    for label, cpl, rel in sources:
        pre = f"{label}: " if rel else ""
        tag = f"{label}:" if rel else ""
        if not cpl:
            out.append(Finding("cpl.jlc", "error", f"{pre}not a readable pick-and-place file", key=f"cpl:unreadable:{label}"))
            continue
        cpl = {r: row for r, row in cpl.items() if r in ctx.board.footprints and r in placed}
        if rel:
            missing = sorted(r for r in placed if r not in cpl and r in ctx.board.footprints)
            if missing:
                out.append(Finding("cpl.jlc", "warning", f"{pre}{len(missing)} placed parts are not in it: "
                                   + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""),
                                   {"ref": missing[0]}, hint="Export the CPL again from the current board.",
                                   key=f"cpl:{tag}missing"))
        res, fitted, _ = fit_placements(lookups, ctx.board, cpl, lcsc, ctx.setting("fab.cpl_pad_map", {}))
        for ref, code, title, rot, frot, off, worst, verdict in res:
            if verdict == "ok":
                continue
            if verdict.startswith("lookup failed"):
                unverified[code].add(ref)
                why.setdefault(verdict.split(": ", 1)[-1], code)
                continue
            sev = "info" if verdict in ("no LCSC code",) else "error" if verdict.startswith(("rotate", "origin")) else "warning"
            b = ctx.board.footprints.get(ref)
            out.append(Finding("cpl.jlc", sev, f"{pre}{ref} ({code}): {verdict}",
                               {"ref": ref, "x": b.x if b else None, "y": b.y if b else None},
                               hint=("Correct this file, or " if rel else "") +
                                    "run `./tw cpl --write` to derive corrections, then regenerate the fab outputs.",
                               key=f"cpl:{tag}{ref}:{verdict.split(' (')[0]}"))
    if unverified:                  # one finding: the service, not the board, is what failed
        refs = sorted({r for rs in unverified.values() for r in rs})
        codes = sorted(unverified)
        b = ctx.board.footprints.get(refs[0])
        reasons = "; ".join(sorted(why))[:160]
        out.append(Finding("cpl.jlc", "error",
                           f"not verified: JLC's footprints for {len(codes)} part code{'s' if len(codes) > 1 else ''} "
                           f"({', '.join(codes[:6])}{' ...' if len(codes) > 6 else ''}) could not be fetched, so "
                           f"{len(refs)} placement{'s are' if len(refs) > 1 else ' is'} unchecked "
                           f"({', '.join(refs[:10])}{' ...' if len(refs) > 10 else ''}) -- {reasons}",
                           {"ref": refs[0], "x": b.x if b else None, "y": b.y if b else None},
                           hint="The parts service (EasyEDA) did not answer in time: a network or throttling problem, not "
                                "the board. Run this check again later; answers are cached, so each run gets further.",
                           key="cpl:unverified"))
    return out
