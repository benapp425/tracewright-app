"""KiCad custom design rules (.kicad_dru): read them into plain data, check them, and ask KiCad itself
whether it accepts them.

KiCad's rule parser is strict and silent: one mistake anywhere -- a number without a unit, a misspelt
constraint, property or function, a bad severity, double quotes inside a condition -- and kicad-cli
ignores the *whole* file without a word, so every custom rule stops applying (found 2026-09-27: the
demo board went from 0 to 32 DRC violations, exit code 0). `check()` catches the usual mistakes with
line numbers as you type; `kicad_accepts()` settles it with KiCad: the rules plus a probe rule go onto
an empty copy of the board, and the probe's violation shows that KiCad read the file.

    rules, problems = check(text)          # problems: [{"line", "severity", "message"}]
    ok, why = kicad_accepts(pcb_path, text)
"""
import os, re, json, shutil, subprocess, tempfile, difflib

CONSTRAINTS = {
    "annular_width", "assertion", "bridged_mask", "clearance", "connection_width", "courtyard_clearance", "creepage",
    "diff_pair_gap", "diff_pair_uncoupled", "disallow", "edge_clearance", "hole_clearance", "hole_size", "hole_to_hole",
    "length", "min_resolved_spokes", "physical_clearance", "physical_hole_clearance", "silk_clearance", "skew",
    "solder_mask_expansion", "solder_paste_abs_margin", "solder_paste_rel_margin", "text_height", "text_thickness",
    "thermal_relief_gap", "thermal_spoke_width", "track_angle", "track_segment_length", "track_width", "via_count",
    "via_diameter", "zone_connection", "via_dangling", "track_dangling", "mechanical_clearance", "mechanical_hole_clearance",
}
UNITLESS = {"via_count", "min_resolved_spokes", "solder_paste_rel_margin"}
DISALLOW = {"track", "via", "through_via", "blind_via", "micro_via", "buried_via", "pad", "zone", "text", "graphic",
            "hole", "footprint", "buried", "blind"}
SEVERITIES = {"error", "warning", "ignore", "exclusion"}
ZONE_CONNECTION = {"solid", "thermal_reliefs", "none"}
UNIT = re.compile(r"^-?\d+(\.\d+)?(mm|mil|mils|in|um|deg|%)$", re.I)
FUNCTIONS = {
    "existsOnLayer", "isPlated", "insideCourtyard", "insideFrontCourtyard", "insideBackCourtyard", "intersectsCourtyard",
    "intersectsFrontCourtyard", "intersectsBackCourtyard", "insideArea", "intersectsArea", "enclosedByArea",
    "isMicroVia", "isBlindBuriedVia", "isBlindVia", "isBuriedVia", "isCoupledDiffPair", "inDiffPair", "memberOf",
    "memberOfGroup", "memberOfFootprint", "memberOfSheet", "memberOfSheetOrChildren", "fromTo", "getField",
    "hasNetclass", "hasExactNetclass", "hasComponentClass", "isDiffPair",
}
PROPERTIES = {
    "Type", "Layer", "Net", "NetName", "NetClass", "Net_Class", "Name", "Reference", "Value", "Width", "Length",
    "Pad_Type", "Pad_Shape", "Pad_Number", "Pin_Name", "Pin_Type", "Hole_Size", "Hole_Size_X", "Hole_Size_Y",
    "Hole_Diameter", "Via_Type", "Fabrication_Property", "Zone_Name", "Priority", "Parent", "Library_Link",
    "Library_Description", "Keywords", "Orientation", "Position_X", "Position_Y", "Size_X", "Size_Y", "Thickness",
    "Text", "Locked", "Visible", "Do_not_Populate", "Exclude_From_BOM", "Exclude_From_Position_Files",
    "Clearance_Override", "Component_Class", "ComponentClass", "Diameter", "Drill", "Start_X", "Start_Y", "End_X",
    "End_Y", "Shape", "Hole_Shape", "Shape_Type", "Line_Width", "Filled", "Pad_To_Die_Length", "Pad_Clearance",
    "Soldermask_Margin_Override", "Solderpaste_Margin_Override", "Solderpaste_Margin_Ratio_Override", "Zone_Connection",
    "Thermal_Relief_Width", "Thermal_Relief_Gap", "Keepout", "Is_Rule_Area", "Board_Side", "Side",
}
PROBE = "(rule \"tw_probe\"\n  (constraint disallow via))"


def _tokens(text):
    """[(kind, value, line)]: '(' ')' 'str' 'atom'; comments (#) skipped. Raises ValueError(line, msg)."""
    out, i, line, n = [], 0, 1, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif c == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "()":
            out.append((c, c, line))
            i += 1
        elif c == '"':
            j, start = i + 1, line
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append("\\" + text[j + 1])
                    j += 2
                    continue
                if text[j] == "\n":
                    line += 1
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ValueError((start, "a string is not closed (missing \")"))
            out.append(("str", "".join(buf), start))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            out.append(("atom", text[i:j], line))
            i = j
    return out


def _tree(tokens):
    stack, root = [], []
    cur = root
    for kind, val, line in tokens:
        if kind == "(":
            node = {"items": [], "line": line}
            cur.append(node)
            stack.append(cur)
            cur = node["items"]
        elif kind == ")":
            if not stack:
                raise ValueError((line, "a ')' closes nothing"))
            cur = stack.pop()
        else:
            cur.append({"kind": kind, "val": val, "line": line})
    if stack:
        raise ValueError((tokens[-1][2] if tokens else 1, "a '(' is not closed"))
    return root


def _head(node):
    it = node.get("items") or []
    return it[0]["val"] if it and "val" in it[0] else None


def _near(word, words):
    low = {w.lower(): w for w in words}
    m = difflib.get_close_matches(word.lower(), sorted(low), n=1, cutoff=0.75)
    return low[m[0]] if m else None


def lint_condition(expr, line, out):
    """Light checks of a rule condition; KiCad's parser is the final word (kicad_accepts)."""
    if '\\"' in expr or '"' in expr:
        out.append({"line": line, "severity": "error", "message": "use single quotes inside a condition ('Default', not \"Default\")"})
    if not expr.strip():
        out.append({"line": line, "severity": "error", "message": "the condition is empty"})
        return
    depth = 0
    for ch in re.sub(r"'[^']*'", "''", expr):
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            break
    if depth != 0:
        out.append({"line": line, "severity": "error", "message": "the condition's parentheses do not balance"})
    if expr.count("'") % 2:
        out.append({"line": line, "severity": "error", "message": "a quoted name in the condition is not closed (')"})
    if re.search(r"(==|!=|&&|\|\||<=|>=|<|>)\s*$", expr) or re.match(r"^\s*(==|!=|&&|\|\|)", expr):
        out.append({"line": line, "severity": "error", "message": "the condition ends (or starts) with an operator"})
    bare = re.sub(r"'[^']*'", "''", expr)
    for m in re.finditer(r"\b[AB]\.([A-Za-z_][A-Za-z0-9_]*)\s*(\()?", bare):
        name, call = m.group(1), m.group(2)
        if call and name not in FUNCTIONS:
            near = _near(name, FUNCTIONS)
            out.append({"line": line, "severity": "error" if near else "warning",
                        "message": f"KiCad has no function {name}()" + (f" -- {near}()?" if near else " that Tracewright knows of")})
        elif not call and name not in PROPERTIES and name not in FUNCTIONS:
            near = _near(name, PROPERTIES)
            out.append({"line": line, "severity": "error" if near else "warning",
                        "message": f"unknown property {name}" + (f" -- {near}?" if near else " (KiCad drops the whole file if it does not know it)")})


def check(text):
    """(rules, problems) for .kicad_dru text: rules [{name, line, condition, layer, severity, constraints:
    [{type, min, max, opt, items, line}]}], problems [{line, severity: error | warning, message}]."""
    problems, rules = [], []
    try:
        tree = _tree(_tokens(text))
    except ValueError as e:
        line, msg = e.args[0]
        return [], [{"line": line, "severity": "error", "message": msg}]
    for node in tree:
        if "items" not in node:
            problems.append({"line": node["line"], "severity": "error", "message": f"'{node['val']}' outside any rule"})
            continue
        head = _head(node)
        if head == "version":
            continue
        if head != "rule":
            problems.append({"line": node["line"], "severity": "error", "message": f"expected (rule ...) or (version 1), found ({head} ...)"})
            continue
        items = node["items"][1:]
        if not items or "val" not in items[0]:
            problems.append({"line": node["line"], "severity": "error", "message": "a rule needs a name: (rule \"name\" ...)"})
            continue
        rule = {"name": items[0]["val"], "line": node["line"], "condition": None, "layer": None, "severity": None,
                "constraints": []}
        for ch in items[1:]:
            if "items" not in ch:
                problems.append({"line": ch["line"], "severity": "error", "message": f"unexpected '{ch['val']}' in rule {rule['name']}"})
                continue
            h = _head(ch)
            args = ch["items"][1:]
            if h == "condition":
                if len(args) != 1 or args[0].get("kind") != "str":
                    problems.append({"line": ch["line"], "severity": "error", "message": "a condition is one quoted expression"})
                    continue
                rule["condition"] = args[0]["val"]
                lint_condition(args[0]["val"], ch["line"], problems)
            elif h == "layer":
                rule["layer"] = args[0]["val"] if args and "val" in args[0] else None
            elif h == "severity":
                sev = args[0]["val"] if args and "val" in args[0] else ""
                rule["severity"] = sev
                if sev not in SEVERITIES:
                    problems.append({"line": ch["line"], "severity": "error",
                                     "message": f"severity '{sev}' is not one of {', '.join(sorted(SEVERITIES))}"})
            elif h == "constraint":
                c = _constraint(ch, args, problems)
                if c:
                    rule["constraints"].append(c)
            else:
                problems.append({"line": ch["line"], "severity": "error", "message": f"unknown clause ({h} ...) in rule {rule['name']}"})
        if not rule["constraints"]:
            problems.append({"line": node["line"], "severity": "error", "message": f"rule {rule['name']} has no constraint"})
        rules.append(rule)
    names = [r["name"] for r in rules]
    for n in {x for x in names if names.count(x) > 1}:
        problems.append({"line": next(r["line"] for r in rules if r["name"] == n), "severity": "warning",
                         "message": f"two rules are named {n}"})
    return rules, problems


def _constraint(ch, args, problems):
    if not args or "val" not in args[0]:
        problems.append({"line": ch["line"], "severity": "error", "message": "a constraint needs a type: (constraint clearance (min 0.2mm))"})
        return None
    typ = args[0]["val"]
    c = {"type": typ, "line": ch["line"]}
    if typ not in CONSTRAINTS:
        near = _near(typ, CONSTRAINTS)
        problems.append({"line": ch["line"], "severity": "error" if near else "warning",
                         "message": f"unknown constraint '{typ}'" + (f" -- {near}?" if near else "")})
    rest = args[1:]
    if typ == "disallow":
        items = [a.get("val") for a in rest]
        c["items"] = items
        for it in items:
            if it not in DISALLOW:
                problems.append({"line": ch["line"], "severity": "error",
                                 "message": f"'{it}' is not something KiCad can disallow ({', '.join(sorted(DISALLOW))})"})
        if not items:
            problems.append({"line": ch["line"], "severity": "error", "message": "disallow what? (track, via, zone, ...)"})
        return c
    if typ == "zone_connection":
        v = rest[0].get("val") if rest else None
        c["value"] = v
        if v not in ZONE_CONNECTION:
            problems.append({"line": ch["line"], "severity": "error", "message": f"zone_connection takes {', '.join(sorted(ZONE_CONNECTION))}"})
        return c
    if typ == "assertion":
        if not rest or rest[0].get("kind") != "str":
            problems.append({"line": ch["line"], "severity": "error", "message": "an assertion takes one quoted expression"})
        else:
            c["expr"] = rest[0]["val"]
            lint_condition(rest[0]["val"], ch["line"], problems)
        return c
    for a in rest:
        if "items" not in a:
            problems.append({"line": a["line"], "severity": "error", "message": f"expected (min ...), (max ...) or (opt ...), found '{a['val']}'"})
            continue
        k = _head(a)
        vals = a["items"][1:]
        v = vals[0]["val"] if vals and "val" in vals[0] else None
        if k not in ("min", "max", "opt", "within_diff_pairs"):
            problems.append({"line": a["line"], "severity": "error", "message": f"unknown limit ({k} ...): use min, max or opt"})
            continue
        if k == "within_diff_pairs":
            c[k] = True
            continue
        if v is None:
            problems.append({"line": a["line"], "severity": "error", "message": f"({k}) needs a value"})
            continue
        if typ in UNITLESS:
            if not re.match(r"^-?\d+(\.\d+)?%?$", v):
                problems.append({"line": a["line"], "severity": "error", "message": f"'{v}' is not a number"})
        elif not UNIT.match(v):
            problems.append({"line": a["line"], "severity": "error",
                             "message": f"'{v}' needs a unit (e.g. {v}mm): KiCad drops the whole file for a bare number"
                             if re.match(r"^-?\d+(\.\d+)?$", v) else f"'{v}' is not a length (0.2mm, 8mil, 45deg)"})
        c[k] = v
    if "min" in c and "max" in c:
        a, b = to_mm(c["min"]), to_mm(c["max"])
        if a is not None and b is not None and a > b:
            problems.append({"line": ch["line"], "severity": "warning", "message": f"min {c['min']} is above max {c['max']}: everything will fail it"})
    return c


def to_mm(v):
    m = re.match(r"^(-?\d+(?:\.\d+)?)(mm|mil|mils|in|um)?$", str(v or ""), re.I)
    if not m:
        return None
    x, u = float(m.group(1)), (m.group(2) or "mm").lower()
    return x * {"mm": 1, "mil": 0.0254, "mils": 0.0254, "in": 25.4, "um": 0.001}[u]


# ----------------------------------------------------------------------------- KiCad's verdict
ITEMS = re.compile(r"^\t\((footprint|segment|arc|via|zone|gr_|group|dimension|generated|image|table|target|embedded_fonts|"
                   r"embedded_files|point)", re.M)


def kicad_accepts(pcb, text, cli=None, timeout=120):
    """(True, "") if KiCad reads these rules, else (False, why). The rules plus a probe rule that
    disallows vias go next to an empty copy of the board (its layers and setup, no items) with one via;
    kicad-cli's DRC reports the probe's violation only when it parsed the file."""
    from . import kicad as twk, env
    cli = cli or env.require("cli")
    with open(pcb, encoding="utf-8") as f:
        board = f.read()
    m = ITEMS.search(board)
    head = board[:m.start()] if m else board.rstrip().rstrip(")")
    via = '\t(via\n\t\t(at 0 0)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n\t)\n'
    tmp = tempfile.mkdtemp(prefix="tw-dru-")
    try:
        stem = os.path.join(tmp, "probe")
        with open(stem + ".kicad_pcb", "w", encoding="utf-8") as f:
            f.write(head.rstrip() + "\n" + via + ")\n")
        pro = os.path.splitext(pcb)[0] + ".kicad_pro"
        if os.path.exists(pro):
            shutil.copy(pro, stem + ".kicad_pro")
        with open(stem + ".kicad_dru", "w", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n" + PROBE + "\n")
        out = stem + ".json"
        r = subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", out, stem + ".kicad_pcb"],
                           capture_output=True, text=True, timeout=timeout, cwd=tmp)
        if not os.path.exists(out):
            return False, (r.stderr or r.stdout or "kicad-cli did not run").strip()[-300:]
        with open(out) as f:
            d = json.load(f)
        if any("tw_probe" in json.dumps(v) for v in d.get("violations", [])):
            return True, ""
        return False, "KiCad could not read the rules file, so it would ignore every rule in it"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def format_rule(name, constraints, condition=None, layer=None, severity=None, comment=None):
    """The text of one rule, as KiCad writes them."""
    lines = []
    if comment:
        lines += [f"# {c}" for c in comment.splitlines()]
    lines.append(f'(rule "{name}"')
    if severity:
        lines.append(f"  (severity {severity})")
    if layer:
        lines.append(f"  (layer {layer})")
    if condition:
        lines.append(f'  (condition "{condition}")')
    for c in constraints:
        lines.append(f"  (constraint {c})")
    lines[-1] += ")"
    return "\n".join(lines)
