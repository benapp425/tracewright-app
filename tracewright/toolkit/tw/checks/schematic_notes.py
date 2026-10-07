"""Schematics people can read: notes short and beside what they explain (the reasoning in the design notes, off the
sheet), support parts drawn at the pin they serve, connectors' pins named, titles filled in."""
import math, re

from . import check, Finding, NotApplicable, examined, plural
from .. import font

HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 /&()+.,:-]{2,40}$")        # a block's title, NOTES, CONTENTS
CITE = re.compile(r"\b(?:datasheet|data sheet)\b[^.\n]{0,30}\b(?:ch(?:apter)?\.?|table|fig(?:ure)?\.?|section|s\.|p\.|page)\s*[\dA-Z][\d.-]*", re.I)
PASSIVE = re.compile(r"^(R|C|L|FB)\d")
CONN = re.compile(r"^(J|P|CN|X|XS|XP|USB|CONN)\d")
SHEET_NOTE = 72          # the longest line a sheet note should have (characters)


def _is_list(text):
    first = text.splitlines()[0].strip() if text.strip() else ""
    return bool(HEADING.match(first)) and len(text.splitlines()) > 1 or bool(re.match(r"^\s*\d+[.)]\s", text))


def _box(t):
    """A note's page box from its anchor (KiCad's text anchor: left end, baseline-ish)."""
    lines = t["text"].split("\n")
    w = max(font.ink_width(l) for l in lines) if lines else 0
    return (t["x"], t["y"] - 1.27, t["x"] + w, t["y"] + 1.27 + 2.0 * (len(lines) - 1))


def _dist(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return math.hypot(dx, dy)


@check("sch.notes", "Notes short and beside what they explain", "Schematic", needs=("sch",))
def sch_notes(ctx):
    """A note on the sheet is one plain line an engineer reads at a glance, next to the part it is about; the reasoning,
    numbers and sources go in that part's design note (design-notes.json: the app shows it on hover). Flags a note
    longer than a line or two, one that cites a data sheet's chapter on the sheet, and one drawn far from the part it
    names."""
    out, seen, n = [], set(), 0
    for sh in ctx.hier.sheets:
        sf = sh.sf
        if sf.path in seen:
            continue
        seen.add(sf.path)
        where = {"sheet": sh.name_path, "file": sh.filename}
        boxes = {s.ref: s.bbox for s in sh.symbols if not s.ref.startswith("#")}
        for t in sf.texts:
            body = t["text"].strip()
            if not body or (HEADING.match(body) and "\n" not in body) or _is_list(body) or sh.parent is None:
                continue                                  # a block's title, a numbered list, the cover's notes
            n += 1
            lines = body.split("\n")
            first = lines[0][:60]
            key = f"notes:{sh.filename}:{t['x']:.1f}:{t['y']:.1f}"
            if len(lines) > 2 or max(len(l) for l in lines) > SHEET_NOTE:
                out.append(Finding("sch.notes", "warning", f"long note: “{first}…” ({len(body)} characters)",
                                   {**where, "x": t["x"], "y": t["y"]}, key=key + ":long",
                                   hint="Keep one plain line on the sheet (“Boot straps: IO2, IO8 high”); put the reasoning, "
                                        "numbers and source in the part's design note (g.note(..., why=...) or the notes tool)."))
            elif CITE.search(body):
                out.append(Finding("sch.notes", "info", f"a data sheet reference on the sheet: “{first}”",
                                   {**where, "x": t["x"], "y": t["y"]}, key=key + ":cite",
                                   hint="The source belongs in the part's design note; the sheet keeps what an engineer needs at a glance."))
            named = [r for r in re.findall(r"\b([A-Z]{1,3}\d{1,4})\b", body) if r in boxes]
            nb = _box(t)
            if named:
                d = min(_dist(nb, boxes[r]) for r in named)
                if d > 20:
                    out.append(Finding("sch.notes", "warning", f"the note about {named[0]} is {d:.0f} mm from it: “{first}”",
                                       {**where, "x": t["x"], "y": t["y"], "ref": named[0]}, key=key + ":far",
                                       hint=f"Put the note beside {named[0]} (g.note(..., near={named[0]!r}))."))
            elif boxes and min(_dist(nb, b) for b in boxes.values()) > 30:
                out.append(Finding("sch.notes", "info", f"a note far from any part: “{first}”", {**where, "x": t["x"], "y": t["y"]},
                                   key=key + ":alone", hint="Put it beside the part it explains."))
    if not n:
        raise NotApplicable("no notes on the sheets")
    examined(ctx, plural(n, "note") + " on " + plural(len(seen), "sheet"))
    return out


def _power_nets(ctx):
    try:
        from ..netmodel import for_context
        m = for_context(ctx)
        return {n for n, r in m.records.items() if r.get("kind") in ("power", "ground")}
    except Exception:
        return set()


@check("sch.support", "Support parts drawn at the pin they serve", "Schematic", needs=("sch", "netlist"))
def sch_support(ctx):
    """A pull-up, a filter capacitor, an RC on an enable pin: drawn at the pin it serves, so the reader sees them
    together. Flags a resistor, capacitor or inductor on a signal that reaches one pin of one chip on its sheet but is
    drawn far from that pin (joined only by a label)."""
    nl = ctx.netlist
    power = _power_nets(ctx)
    out, n = [], 0
    for sh in ctx.hier.sheets:
        syms = [s for s in sh.symbols if not s.ref.startswith("#")]
        pos = {(s.ref, str(num)): (x, y) for s in syms for num, _, _, x, y in s.pins}
        refs = {s.ref for s in syms}
        for s in syms:
            if not PASSIVE.match(s.ref):
                continue
            nets = {nl.pin.get((s.ref, str(num))) for num, _, _, _, _ in s.pins}
            signal = [x for x in nets if x and x not in power and not x.startswith("unconnected-")]
            for net in signal:
                users = [(r, p) for r, p in nl.nets.get(net, []) if r in refs and r != s.ref and not PASSIVE.match(r)]
                if len(users) != 1:
                    continue
                n += 1
                r, p = users[0]
                at = pos.get((r, str(p)))
                mine = [pos.get((s.ref, str(num))) for num, _, _, _, _ in s.pins if nl.pin.get((s.ref, str(num))) == net]
                mine = [m for m in mine if m]
                if not at or not mine:
                    continue
                d = min(math.hypot(m[0] - at[0], m[1] - at[1]) for m in mine)
                if d > 40:
                    name = re.sub(r"~\{([^}]*)\}", r"\1", nl.pin_name(r, p) or "")      # KiCad's overbar markup, as read
                    out.append(Finding("sch.support", "warning", f"{s.ref} serves {r} pin {p}{' (' + name + ')' if name else ''} but is "
                                       f"drawn {d:.0f} mm from it", {"sheet": sh.name_path, "file": sh.filename, "ref": s.ref},
                                       hint=f"Draw {s.ref} at the pin: pull(..., cap=...) for an RC, decouple(...) for a capacitor, "
                                            f"pull(...) or series(...) for a resistor.", key=f"support:{s.ref}:{r}:{p}"))
    if not n:
        raise NotApplicable("no support parts on single pins")
    examined(ctx, plural(n, "support part"))
    return out


@check("sch.labels", "Connector pins named, titles filled", "Schematic", needs=("sch", "netlist"))
def sch_labels(ctx):
    """Every pin of a connector carries its signal's name (a net named only by KiCad, Net-(J3-Pad4), tells the reader
    nothing), and every sheet has its title."""
    nl = ctx.netlist
    out, n = [], 0
    for ref, part in sorted(nl.parts.items()):
        if not CONN.match(ref):
            continue
        n += 1
        bare = sorted({p for p in nl.pins_of(ref) if (nl.pin.get((ref, p)) or "").split("/")[-1].startswith("Net-(")},
                      key=lambda x: (len(x), x))
        if bare:
            out.append(Finding("sch.labels", "warning", f"{ref}: {plural(len(bare), 'pin')} on nets with no name (pin{'s' if len(bare) > 1 else ''} "
                               f"{', '.join(bare[:8])})", {"ref": ref}, key=f"labels:{ref}",
                               hint="Name each with its signal (a net label at the pin, g.net(j, pin, NAME)), so the reader sees what goes where."))
    seen = set()
    for sh in ctx.hier.sheets:
        if sh.sf.path in seen:
            continue
        seen.add(sh.sf.path)
        if not (sh.sf.title.get("title") or "").strip():
            out.append(Finding("sch.labels", "warning", f"{sh.filename} has no title in its title block",
                               {"sheet": sh.name_path, "file": sh.filename}, key=f"labels:title:{sh.filename}",
                               hint="Give every sheet its title (what it holds), the board's name and revision."))
    examined(ctx, plural(n, "connector") + ", " + plural(len(seen), "title block"))
    return out
