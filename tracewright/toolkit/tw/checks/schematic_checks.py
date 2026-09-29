"""Schematic checks that KiCad's ERC does not make: readability of the plotted sheets, wires that
silently merge nets, and net names that are probably typos."""
import re, collections
from . import check, Finding, NotApplicable, examined, plural

# KiCad's default schematic theme as plotted (kicad-cli --theme _builtin_default)
WIRE, NC, BODY = "#009600", "#000084", "#840000"
OVER = 0.09        # mm two ink boxes must share both ways to count as an overlap
INSET = 0.09       # mm a line must reach inside a text's ink box


def _through(seg, box):
    """Does the segment pass through the box's interior (Liang-Barsky)?"""
    x0, y0, x1, y1 = box
    ax, ay, bx, by = seg[:4]
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return t0 < t1


def _edges(r):
    x0, y0, x1, y1 = r
    return [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]


def _same_block(a, b):
    """Consecutive lines of one multi-line text: same anchor and size, one line pitch apart (KiCad's
    own line spacing lets descenders meet the next line's ascenders; that is not a layout fault)."""
    try:
        return abs(a.x - b.x) < 0.02 and abs(a.size - b.size) < 0.02 and 0.9 * a.size < abs(a.y - b.y) < 2.2 * a.size
    except TypeError:
        return False


def render_findings(doc, rects, sheet_label):
    """[(kind, message, (x, y))] for one plotted sheet. rects: block / zone rectangles of the sheet."""
    words, seen = [], set()
    for t in doc.texts:                       # KiCad plots some texts twice (e.g. fields): keep one
        if not t.box or not t.text.strip():
            continue
        k = (t.text, round(t.box[0] / 0.05), round(t.box[1] / 0.05), round(t.box[2] / 0.05), round(t.box[3] / 0.05))
        if k in seen:
            continue
        seen.add(k)
        words.append(t)
    out = []
    # text against text (sort-and-sweep on x)
    words.sort(key=lambda t: t.box[0])
    for i, a in enumerate(words):
        for b in words[i + 1:]:
            if b.box[0] >= a.box[2] - OVER:
                break
            ix = min(a.box[2], b.box[2]) - max(a.box[0], b.box[0])
            iy = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
            if ix > OVER and iy > OVER and not _same_block(a, b):
                out.append(("text-text", f"'{a.text}' overlaps '{b.text}'",
                            ((a.box[0] + b.box[0]) / 2, (a.box[1] + b.box[1]) / 2)))
    wires = [s for s in doc.segs if s[4] == WIRE]
    body = [s for s in doc.segs if s[4] == BODY and s[5] > 0.2]
    marks = [s for s in doc.segs if (s[4] == BODY and s[5] <= 0.2) or s[4] == NC]
    borders = [e for r in rects for e in _edges(r)]
    pools = (("text-wire", wires, "a wire runs through"), ("text-body", body, "a symbol outline runs through"),
             ("text-pin", marks, "a pin / power symbol / no-connect mark runs through"),
             ("text-border", borders, "a block or zone border runs through"))
    for t in words:
        s = (t.box[0] + INSET, t.box[1] + INSET, t.box[2] - INSET, t.box[3] - INSET)
        if s[0] >= s[2] or s[1] >= s[3]:
            continue
        for kind, pool, what in pools:
            for q in pool:
                if max(q[0], q[2]) < s[0] or min(q[0], q[2]) > s[2] or max(q[1], q[3]) < s[1] or min(q[1], q[3]) > s[3]:
                    continue
                if _through(q, s):
                    out.append((kind, f"{what} '{t.text}'", (t.box[0], t.box[1])))
                    break
    return out


@check("sch.render", "Schematic readability (plotted sheets)", "Schematic", needs=("svg", "sch"))
def sch_render(ctx):
    """Reads the sheets KiCad actually plots: each text's ink box is the extent of its own glyph
    strokes, then text/text overlaps and wires, symbol outlines, pins and block borders crossing
    text are reported. (An estimate of text width misses real overlaps; this does not.)"""
    out = []
    sheets = {sh.name_path: sh for sh in ctx.hier.sheets}
    for name_path, (doc, path) in sorted(ctx.svgs.items()):
        sh = sheets.get(name_path)
        rects = sh.sf.rects if sh else []
        for kind, msg, (x, y) in render_findings(doc, rects, name_path):
            out.append(Finding("sch.render", "warning", f"{kind}: {msg}",
                               {"sheet": name_path, "file": sh.filename if sh else "", "x": round(x, 2), "y": round(y, 2)},
                               key=f"sch.render:{name_path}:{kind}:{msg}"))
    examined(ctx, plural(len(ctx.svgs), "plotted sheet"))
    return out


def _collinear_overlap(a, b, c, d, tol=1e-6):
    """Do segments a-b and c-d lie on one line and share more than a point?"""
    (ax, ay), (bx, by), (cx, cy), (dx_, dy_) = a, b, c, d
    ux, uy = bx - ax, by - ay
    L = (ux * ux + uy * uy) ** 0.5
    if L < tol:
        return False
    for px, py in (c, d):
        if abs((px - ax) * uy - (py - ay) * ux) / L > 0.01:
            return False
    t = lambda p: ((p[0] - ax) * ux + (p[1] - ay) * uy) / L
    lo, hi = sorted((t(c), t(d)))
    return min(hi, L) - max(lo, 0.0) > 0.02


@check("sch.wiring", "Wires that silently join nets", "Schematic", needs=("sch",))
def sch_wiring(ctx):
    """Two wires lying on the same line and overlapping are one connection in KiCad, even when they
    were meant as two nets (a USB D+ jumper drawn along the D- wire merged the pair on a CM5
    board, and ERC said nothing because every pin involved was passive)."""
    out = []
    for sh in ctx.hier.sheets:
        segs = []
        for w in sh.sf.wires:
            segs += list(zip(w, w[1:]))
        segs.sort(key=lambda s: min(s[0][0], s[1][0]))
        for i, (a, b) in enumerate(segs):
            ax1 = max(a[0], b[0])
            for c, d in segs[i + 1:]:
                if min(c[0], d[0]) > ax1 + 0.01:
                    break
                if _collinear_overlap(a, b, c, d):
                    out.append(Finding("sch.wiring", "warning",
                                       f"overlapping collinear wires at ({a[0]:.2f}, {a[1]:.2f})-({b[0]:.2f}, {b[1]:.2f}) "
                                       f"join into one net", {"sheet": sh.name_path, "file": sh.filename,
                                                               "x": (a[0] + b[0]) / 2, "y": (a[1] + b[1]) / 2},
                                       hint="Route one of the wires on a different line or join them deliberately.",
                                       key=f"sch.wiring:{sh.name_path}:{a}:{b}"))
        if sh.sf is not None:
            pass
    examined(ctx, plural(sum(len(sh.sf.wires) for sh in ctx.hier.sheets), "wire") + " on " + plural(len(ctx.hier.sheets), "sheet"))
    return out


# ----------------------------------------------------------------------------- conventions
GROUNDS = re.compile(r"^(GND\w*|\w*_GND|AGND|DGND|PGND|SGND|VSS\w*|EARTH|CHASSIS)$", re.I)
PREFIX = [                                                    # (lib_id pattern, reference letters it may use)
    (re.compile(r"^Device:R_Pack|^Device:R_Array"), ("RN", "R")),
    (re.compile(r"^Device:(R|R_Small|R_US|R_Potentiometer\w*|Thermistor\w*|Varistor\w*)$"), ("R", "RV", "TH", "RT")),
    (re.compile(r"^Device:(C|C_Small|C_Polarized\w*|CP\w*|C_Feedthrough\w*)$"), ("C",)),
    (re.compile(r"^Device:(L|L_Small|L_Core\w*)$"), ("L",)),
    (re.compile(r"^Device:(FerriteBead\w*)$"), ("FB", "L")),
    (re.compile(r"^Device:(D|D_\w+|LED\w*)$"), ("D", "LED")),
    (re.compile(r"^Device:(Q_\w+)$"), ("Q",)),
    (re.compile(r"^Device:(Crystal\w*|Resonator\w*)$"), ("Y", "X")),
    (re.compile(r"^Device:(Fuse\w*|Polyfuse\w*)$"), ("F",)),
    (re.compile(r"^Device:(Battery\w*)$"), ("BT",)),
    (re.compile(r"^Connector\w*:(?!TestPoint)"), ("J", "P", "CN", "X")),
    (re.compile(r"^Switch:"), ("SW", "S")),
    (re.compile(r"^(Connector:TestPoint|TestPoint:)"), ("TP",)),
    (re.compile(r"^Mechanical:MountingHole"), ("H", "MH")),
    (re.compile(r"^Jumper:"), ("JP", "J")),
    (re.compile(r"^Relay\w*:"), ("K",)),
    (re.compile(r"^(MCU_|Regulator_|Sensor_|Interface\w*|Amplifier_|Memory_|Power_Management|Logic_|Driver_|RF_Module|Timer)"),
     ("U", "IC", "A", "MOD", "M", "PS", "VR")),
]


def _walk(node):
    if isinstance(node, list):
        yield node
        for c in node[1:]:
            yield from _walk(c)


def _font_size(node):
    for n in _walk(node):
        if n and n[0] == "size" and len(n) >= 3:
            try:
                return min(float(n[1]), float(n[2]))
            except (TypeError, ValueError):
                return None
    return None


@check("sch.style", "Drawn to schematic conventions", "Schematic", needs=("sch",))
def sch_style(ctx):
    """What professional sheets do (knowledge: schematic-conventions): theme colors only -- color
    must not carry meaning and the PDF must print in black and white -- and unfilled boxes; no four-
    way junctions; ground symbols pointing down and supplies up; text no smaller than 1 mm; a filled
    title block; reference letters that match the part (IEEE 315 / ASME Y14.44)."""
    out = []
    seen = set()
    many_sheets = len(ctx.hier.sheets) > 1
    style = ((ctx.cfg or {}).get("schematic") or {}).get("style")         # the project's choice (Schematic tab)
    supplies = {s_.value for sh in ctx.hier.sheets for s_ in sh.symbols if s_.is_power and s_.value}
    has_pins = any(c["pins"] for sh in ctx.hier.sheets for c in sh.children)
    for sh in ctx.hier.sheets:
        sf = sh.sf
        where = {"sheet": sh.name_path, "file": sh.filename}
        if sf.path not in seen:                                    # file-level: once per file
            seen.add(sf.path)
            colored, filled, small, globals_ = {}, 0, 0, []
            hier_labels = sum(1 for item in sf.tree[1:] if isinstance(item, list) and item and item[0] == "hierarchical_label")
            for item in sf.tree[1:]:
                if not isinstance(item, list) or item[0] in ("lib_symbols", "title_block"):
                    continue
                kind = item[0]
                for n in _walk(item):
                    if n and n[0] == "color" and len(n) >= 5:
                        try:
                            rgba = [float(v) for v in n[1:5]]
                        except (TypeError, ValueError):
                            continue
                        if any(rgba[:3]) and rgba[3] > 0:
                            colored[kind] = colored.get(kind, 0) + 1
                    if n and n[0] == "fill" and kind in ("rectangle", "text_box", "polyline", "circle", "arc"):
                        t = [c for c in n[1:] if isinstance(c, list) and c and c[0] == "type"]
                        if t and str(t[0][1]) in ("color", "background"):
                            filled += 1
                if kind in ("text", "label", "global_label", "hierarchical_label", "text_box"):
                    sz = _font_size(item)
                    if sz is not None and sz < 1.0 - 1e-6:
                        small += 1
                if kind == "global_label" and len(item) > 1:
                    globals_.append(str(item[1]))
            if colored:
                parts = ", ".join(f"{k} x{v}" for k, v in sorted(colored.items()))
                out.append(Finding("sch.style", "info", f"custom colors on {sum(colored.values())} items ({parts})", where,
                                   hint="Leave colors at the default so KiCad's theme draws them and the PDF prints in black "
                                        "and white; say it in a note instead of a color.", key=f"style:colour:{sf.path}"))
            if filled:
                out.append(Finding("sch.style", "info", f"{filled} filled boxes or shapes", where,
                                   hint="Group a function with a thin unfilled box and a title; tints hide wires in print.",
                                   key=f"style:fill:{sf.path}"))
            if small:
                out.append(Finding("sch.style", "warning", f"{small} texts or labels smaller than 1 mm", where,
                                   hint="1.27 mm for labels, fields and notes; 2 mm for block titles.",
                                   key=f"style:small:{sf.path}"))
            if many_sheets and globals_ and (style == "hierarchical" or (style is None and has_pins)):
                signals = sorted(set(globals_) - supplies)
                if signals:
                    out.append(Finding("sch.style", "info", f"{len(signals)} global labels ({', '.join(signals[:6])}) "
                                       + ("in a hierarchical schematic" if style else "alongside sheet pins"), where,
                                       hint="Sheet pins show which signals cross which sheets (`./tw style hierarchical` redraws "
                                            "them); or choose flat in the Schematic tab if that is how this design is drawn.",
                                       key=f"style:global:{sf.path}"))
            if many_sheets and style == "flat" and sh.parent is not None and hier_labels:
                out.append(Finding("sch.style", "info", f"{hier_labels} hierarchical labels in a flat schematic", where,
                                   hint="`./tw style flat` redraws them as global labels, checked against KiCad's netlist.",
                                   key=f"style:hier:{sf.path}"))
            tb = sf.title
            missing = [k for k in ("title", "rev", "date") if not tb.get(k)]
            if missing:
                out.append(Finding("sch.style", "warning" if set(missing) - {"date"} else "info",
                                   f"title block without {', '.join(missing)}", where,
                                   hint="Every sheet: title, revision, date, company or author.",
                                   key=f"style:tb:{sf.path}"))
            # four-way junctions: a dot where four or more wire arms / pins meet
            segs = [(a, b) for w in sf.wires for a, b in zip(w, w[1:])]
            pins = [(round(x, 2), round(y, 2)) for s_ in sh.symbols for (_, _, _, x, y) in s_.pins]
            for jx, jy in sf.junctions:
                arms = sum(1 for p in pins if abs(p[0] - jx) < 0.01 and abs(p[1] - jy) < 0.01)
                for a, b in segs:
                    if (abs(a[0] - jx) < 0.01 and abs(a[1] - jy) < 0.01) or (abs(b[0] - jx) < 0.01 and abs(b[1] - jy) < 0.01):
                        arms += 1
                    elif min(a[0], b[0]) - 0.01 <= jx <= max(a[0], b[0]) + 0.01 and \
                            min(a[1], b[1]) - 0.01 <= jy <= max(a[1], b[1]) + 0.01 and \
                            abs((b[0] - a[0]) * (jy - a[1]) - (b[1] - a[1]) * (jx - a[0])) < 0.01:
                        arms += 2
                if arms >= 4:
                    out.append(Finding("sch.style", "warning", f"four-way junction at ({jx:.2f}, {jy:.2f})",
                                       {**where, "x": jx, "y": jy},
                                       hint="Split it into two T junctions a grid step apart: a dot on a crossing is "
                                            "easy to miss, and a missed dot reads as two wires crossing.",
                                       key=f"style:4way:{sf.path}:{jx:.2f}:{jy:.2f}"))
        for s_ in sh.symbols:                                  # per instance: symbols and their designators
            if s_.is_power and s_.pins and s_.value and not s_.value.upper().startswith("PWR_FLAG"):
                (_, _, _, px, py) = s_.pins[0]
                cy = (s_.bbox[1] + s_.bbox[3]) / 2
                cx = (s_.bbox[0] + s_.bbox[2]) / 2
                if abs(cy - py) > abs(cx - px) + 0.1:              # points up or down (sideways is fine at a pin)
                    down = cy > py
                    if GROUNDS.match(s_.value) and not down:
                        out.append(Finding("sch.style", "warning", f"ground symbol {s_.value} points up at ({px:.2f}, {py:.2f})",
                                           {**where, "x": px, "y": py}, hint="Ground points down, supplies up.",
                                           key=f"style:gndup:{sh.path}:{px:.2f}:{py:.2f}"))
                    elif not GROUNDS.match(s_.value) and not s_.value.startswith("-") and down:
                        out.append(Finding("sch.style", "warning", f"supply symbol {s_.value} points down at ({px:.2f}, {py:.2f})",
                                           {**where, "x": px, "y": py}, hint="Positive supplies point up, ground down "
                                           "(negative rails down).", key=f"style:supdown:{sh.path}:{px:.2f}:{py:.2f}"))
                continue
            if s_.is_power or not s_.ref or s_.ref.startswith("#"):
                continue
            letters = re.match(r"[A-Za-z]+", s_.ref)
            for pat, allowed in PREFIX:
                if pat.search(s_.lib_id) or (s_.lib and pat.search("Device:" + s_.lib.name.split(":")[-1])
                                             and s_.lib_id.split(":")[0] not in ("power",)):
                    if letters and letters.group(0).upper() not in allowed:
                        out.append(Finding("sch.style", "warning",
                                           f"{s_.ref} is a {s_.lib_id.split(':')[-1]}: its designator should start with "
                                           f"{' or '.join(allowed[:2])}", {**where, "ref": s_.ref},
                                           hint="Class letters (IEEE 315 / ASME Y14.44): R resistor, C capacitor, L inductor, "
                                                "D diode or LED, Q transistor, U IC, J connector, SW switch, Y crystal, F fuse, "
                                                "FB ferrite, TP test point, H mounting hole.", key=f"style:ref:{s_.ref}"))
                    break
    examined(ctx, plural(len(ctx.hier.sheets), "sheet"))
    return out


# What a person writes on a schematic is short: a name, a value, why a part is there, a layout note.
# These read like a tool talking, a review checklist, or a README pasted onto the sheet.
NARRATION = [
    (re.compile(r"\bopen(ed)?\s+(it\s+)?to\s+(inspect|see|view)\b|\b(double[- ]?)?click\b|\bpin[- ]to[- ]net\b|"
                r"\bnamed\s+pin", re.I), "warning", "tells the reader how to use the tool"),
    (re.compile(r"\b(labels?|nets?)\s+(with|of)\s+the\s+same\s+name\b|\belectrically\s+connected\b|\bNC\s+marks?\b|"
                r"\bintentional(ly)?\s+unused\b", re.I), "warning", "explains how schematics work"),
    (re.compile(r"\b\d+\s+components?\b|\bsheet\s+\d+\s*/\s*\d+", re.I), "warning", "counts parts or pages"),
    (re.compile(r"\b(must|should|needs? to)\s+be\s+(bench[- ])?(validated|verified|measured|confirmed|checked)\b|"
                r"\bbefore\s+(deployment|unattended\s+use|release)\b|\bengineering\s+release\b|\bverification\s+with\b|"
                r"\brequires?\s+(a\s+)?bench\b", re.I), "info", "is a review note, not a drawing note"),
    (re.compile(r"\bfirmware\s+(must|should|shall)\b", re.I), "info", "is a firmware requirement"),
    (re.compile(r"\b(this|the)\s+(sheet|page)\s+(shows|contains|holds|covers)\b|\bare\s+shown\s+on\s+the\b", re.I),
     "info", "narrates the drawing"),
]


@check("sch.text", "Notes read like an engineer's", "Schematic", needs=("sch",))
def sch_text(ctx):
    """The words on the sheets: nothing inside a sheet symbol (it shows its name, file and pins); no notes
    that talk about the tool, explain how labels work, count parts, or hold review items and firmware
    requirements (those go in docs/); no wall of prose; supplies and flags drawn with KiCad's own power
    symbols, not boxes."""
    out, seen = [], set()
    for sh in ctx.hier.sheets:
        sf = sh.sf
        where = {"sheet": sh.name_path, "file": sh.filename}
        if sf.path in seen:
            continue
        seen.add(sf.path)
        for c in sh.children:
            (x, y), (w, hh) = c["at"], c["size"]
            inside = [t for t in sf.texts if x < t["x"] < x + w and y < t["y"] < y + hh]
            if inside:
                out.append(Finding("sch.text", "warning", f"text inside the sheet symbol for {c['name'] or c['file']}: "
                                   f"\u201c{inside[0]['text'].splitlines()[0][:60]}\u201d", {**where, "x": inside[0]["x"], "y": inside[0]["y"]},
                                   hint="A sheet symbol shows its name, its file and its pins. Say what the sheet holds on "
                                        "that sheet, or in the cover's contents list.", key=f"text:insheet:{sf.path}:{c['path']}"))
        total = 0
        for t in sf.texts:
            body = t["text"]
            total += len(body)
            for rx, sev, why in NARRATION:
                m = rx.search(body)
                if m:
                    line = next((l for l in body.splitlines() if rx.search(l)), body)[:90]
                    out.append(Finding("sch.text", sev, f"note {why}: \u201c{line.strip()}\u201d", {**where, "x": t["x"], "y": t["y"]},
                                       hint="Keep notes to what the drawing cannot show: why a value, a rating, a layout "
                                            "constraint, a short numbered list of general notes. Review items, firmware rules "
                                            "and background go in docs/.", key=f"text:{why[:12]}:{sf.path}:{t['x']:.1f}:{t['y']:.1f}"))
                    break
            longest = max((len(l) for l in body.splitlines()), default=0)
            if longest > 110:
                out.append(Finding("sch.text", "info", f"a {longest}-character line of text", {**where, "x": t["x"], "y": t["y"]},
                                   hint="A schematic note is a line or two beside what it explains.",
                                   key=f"text:long:{sf.path}:{t['x']:.1f}:{t['y']:.1f}"))
        long_notes = [t for t in sf.texts if len(t["text"]) > 40]
        if total > 1400 or len(long_notes) > 10:
            out.append(Finding("sch.text", "info", f"{len(sf.texts)} notes, {total} characters, on one sheet", where,
                               hint="This reads like a document. Keep the cover to what the board is, the sheet list, a short "
                                    "numbered list of general notes and the revisions; the rest belongs in docs/.",
                               key=f"text:wall:{sf.path}"))
        for s_ in sh.symbols:
            if s_.ref.startswith("#FLG") and s_.lib and any(k == "rectangle" for _, _, k, _ in s_.lib.graphics):
                out.append(Finding("sch.text", "warning", f"{s_.ref} is a box standing in for PWR_FLAG", {**where, "ref": s_.ref},
                                   hint="Use KiCad's power:PWR_FLAG: a small flag on the supply wire it marks.",
                                   key=f"text:flag:{s_.ref}"))
    examined(ctx, plural(len(seen), "sheet"))
    return out


ACTIVE_LOW = {                                    # how each convention writes an active-low name, and how the others do
    "_N": re.compile(r"^[A-Z0-9]+(_[A-Z0-9]+)*_N$"), "N_prefix": re.compile(r"^n[A-Z][A-Z0-9_]*$|^N(RST|RESET|EN|CS|OE|WE|IRQ|INT)\w*$"),
    "#": re.compile(r"#$"), "overbar": re.compile(r"~\{"),
}
IEC_VAL = re.compile(r"^\d+[RKMkmunpµ]\d+", re.U)            # 4k7, 2R2, 4u7
DEC_VAL = re.compile(r"^\d+\.\d+\s*[kKMmunpµ]?", re.U)       # 4.7k, 2.2uF


@check("sch.conventions", "Names and values follow the project's conventions", "Schematic", needs=("sch",))
def sch_conventions(ctx):
    """Only the conventions the project chose (Schematic settings): active-low nets written one way
    (RESET_N, NRESET, RESET# or an overbar), values in one notation (4k7 or 4.7k)."""
    from ..sch import conventions
    chosen = conventions.chosen(ctx.cfg)
    if not (set(chosen) & {"active_low", "values"}):
        raise NotApplicable("no naming conventions chosen for this project (Schematic settings)")
    out = []
    names = {str(l["text"]) for sh in ctx.hier.sheets for l in sh.sf.labels}
    if "active_low" in chosen:
        from .signal import find_pairs
        paired = {n for pr in find_pairs(list(names)) for n in pr}
        want = chosen["active_low"]
        for n in sorted(names):
            if n in paired:
                continue
            if re.search(r"(USB|D|DM|TX|RX|TD|RD|CLK|LANE\d*|DATA\d*|CSI\w*|DSI\w*|HDMI\w*|ETH\w*|MDI\d*|LVDS\w*)_N$", n, re.I):
                continue                                      # the negative half of a pair, not an active-low line
            for style, rx in ACTIVE_LOW.items():
                if style != want and rx.search(n):
                    label = {"_N": "RESET_N", "N_prefix": "NRESET", "#": "RESET#", "overbar": "an overbar"}
                    out.append(Finding("sch.conventions", "info", f"{n} is written like {label[style]}; this project writes "
                                       f"active-low nets like {label[want]}", {"net": n},
                                       hint="Rename the label to the project's convention (Schematic settings).",
                                       key=f"conv:low:{n}"))
                    break
    if "values" in chosen:
        want = chosen["values"]
        for sh, s_ in ctx.hier.all_symbols():
            if not re.match(r"^(R|C|L|FB)\d", s_.ref or ""):
                continue
            v = (s_.value or "").strip()
            bad = (want == "iec" and DEC_VAL.match(v)) or (want == "decimal" and IEC_VAL.match(v))
            if bad:
                out.append(Finding("sch.conventions", "info", f"{s_.ref} value {v}: the project writes values "
                                   f"{'like 4k7, 100n' if want == 'iec' else 'like 4.7k, 100nF'}", {"sheet": sh.name_path, "ref": s_.ref},
                                   hint="One notation throughout (Schematic settings).", key=f"conv:val:{s_.ref}"))
    examined(ctx, ", ".join(sorted(set(chosen) & {"active_low", "values"})).replace("active_low", "active-low names") + " on " + plural(len(ctx.netlist.nets), "net"))
    return out


def _canon(n):
    return re.sub(r"[^A-Z0-9]", "", n.upper().replace("+", "P"))


_RAIL = re.compile(r"^\+?(\d+)V(\d*)$|^\+?(\d+)\.(\d+)V$", re.I)


def _rail_value(n):
    m = _RAIL.match(n)
    if not m:
        return None
    if m.group(1) is not None:
        return float(m.group(1) + "." + (m.group(2) or "0"))
    return float(m.group(3) + "." + m.group(4))


@check("sch.nets", "Net names and single-connection nets", "Schematic", needs=("netlist",))
def sch_nets(ctx):
    """Names that differ only by case or punctuation (SDA / Sda / SDA_), one rail written two ways
    (+3V3 / 3V3 / +3.3V), and named nets that reach only one pin (usually a label typo)."""
    nl = ctx.netlist
    out = []
    short = collections.defaultdict(set)
    for full in nl.nets:
        if full.startswith("unconnected-") or full.startswith("Net-("):
            continue
        short[nl.short(full)].add(full)
    groups = collections.defaultdict(set)
    for s in short:
        groups[_canon(s)].add(s)
    for k, names in groups.items():
        if len(names) > 1:
            out.append(Finding("sch.nets", "warning", "net names differ only by case/punctuation: " + ", ".join(sorted(names)),
                               {"net": sorted(names)[0]}, key=f"sch.nets:similar:{k}"))
    rails = collections.defaultdict(set)
    for s in short:
        v = _rail_value(s)
        if v is not None:
            rails[v].add(s)
    for v, names in rails.items():
        if len(names) > 1:
            out.append(Finding("sch.nets", "warning", f"the {v:g} V rail has several names: " + ", ".join(sorted(names)),
                               {"net": sorted(names)[0]},
                               hint="If these are one rail, use one name; if not, make the names say how they differ.",
                               key=f"sch.nets:rail:{v}"))
    for full, nodes in nl.nets.items():
        if full.startswith("unconnected-") or full.startswith("Net-("):
            continue
        real = [(r, p) for r, p in nodes if not r.startswith("#")]
        if len(real) == 1:
            r, p = real[0]
            out.append(Finding("sch.nets", "warning", f"net {nl.short(full)} reaches only {r} pin {p}",
                               {"net": nl.short(full), "ref": r}, hint="A label with a typo, or a missing connection.",
                               key=f"sch.nets:single:{full}"))
    examined(ctx, plural(len(short), "named net"))
    return out
