"""The board's design rules for the Rules tab: the board constraints, net classes and their patterns,
DRC severities and presets from the .kicad_pro, and the custom rules (.kicad_dru), each next to what
the fab can make and what it actually applies to. Saving checks the custom rules (and asks KiCad
whether it can read them: it silently ignores a file with one mistake), takes a checkpoint, then
writes the files the way KiCad does.
"""
import os, re, json, fnmatch, collections

from tw import dru as twdru, dfm
from tw.pro import ProjectSettings

# key: (label, help, fab profile key, kind)
CONSTRAINTS = [
    ("min_clearance", "Clearance", "Copper to copper, between different nets.", "min_space", "mm"),
    ("min_track_width", "Track width", "The narrowest track allowed anywhere.", "min_track", "mm"),
    ("min_connection", "Connection width", "The narrowest copper that still counts as connected (zone necks, thermal spokes).", None, "mm"),
    ("min_via_diameter", "Via diameter", "Outer copper diameter of a via.", "min_via_diameter", "mm"),
    ("min_via_annular_width", "Via annular ring", "Copper left around a via's hole.", "min_annular", "mm"),
    ("min_through_hole_diameter", "Hole diameter", "The smallest drilled hole (vias and pads).", "min_via_drill", "mm"),
    ("min_hole_to_hole", "Hole to hole", "Between the edges of two drilled holes.", "min_hole_to_hole", "mm"),
    ("min_hole_clearance", "Hole clearance", "From a hole to copper of another net.", None, "mm"),
    ("min_copper_edge_clearance", "Copper to board edge", "From copper to the board outline (and slots).", "min_edge_clearance", "mm"),
    ("min_microvia_diameter", "Microvia diameter", "Laser-drilled microvias (HDI boards only).", None, "mm"),
    ("min_microvia_drill", "Microvia hole", "", None, "mm"),
    ("min_silk_clearance", "Silkscreen clearance", "From silkscreen to pads and exposed copper.", None, "mm"),
    ("min_text_height", "Silk text height", "Smaller text prints blurred.", "silk_text_height", "mm"),
    ("min_text_thickness", "Silk line width", "Thinner strokes may not print.", "silk_line_width", "mm"),
    ("solder_mask_to_copper_clearance", "Solder mask to copper", "Mask openings beside copper of another net.", None, "mm"),
    ("min_groove_width", "Groove width", "The narrowest milled slot.", None, "mm"),
    ("min_resolved_spokes", "Thermal spokes", "Spokes a thermal relief must keep after the fill.", None, "count"),
    ("max_error", "Arc approximation error", "How closely arcs and pours follow the true shape.", None, "mm"),
]
CLASS_FIELDS = [("clearance", "Clearance"), ("track_width", "Track"), ("via_diameter", "Via"), ("via_drill", "Via hole"),
                ("diff_pair_width", "Pair width"), ("diff_pair_gap", "Pair gap"), ("diff_pair_via_gap", "Pair via gap"),
                ("microvia_diameter", "uVia"), ("microvia_drill", "uVia hole")]
SEV_GROUPS = [
    ("Electrical", ["clearance", "hole_clearance", "shorting_items", "tracks_crossing", "unconnected_items", "net_conflict",
                    "connection_width", "starved_thermal", "copper_sliver", "isolated_copper", "creepage", "zones_intersect",
                    "items_not_allowed", "track_dangling", "via_dangling"]),
    ("Manufacturing", ["annular_width", "drill_out_of_range", "microvia_drill_out_of_range", "hole_to_hole", "holes_co_located",
                       "copper_edge_clearance", "track_width", "invalid_outline", "solder_mask_bridge", "padstack",
                       "track_angle", "track_segment_length", "track_not_centered_on_via", "track_on_post_machined_layer",
                       "through_hole_pad_without_hole", "item_on_disabled_layer", "text_on_edge_cuts"]),
    ("High speed", ["diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long", "length_out_of_range",
                    "skew_out_of_range", "too_many_vias", "missing_tuning_profile", "tuning_profile_track_geometries"]),
    ("Footprints and placement", ["courtyards_overlap", "missing_courtyard", "malformed_courtyard", "pth_inside_courtyard",
                                  "npth_inside_courtyard", "duplicate_footprints", "extra_footprint", "missing_footprint",
                                  "footprint", "footprint_type_mismatch", "lib_footprint_issues", "lib_footprint_mismatch",
                                  "footprint_symbol_mismatch", "footprint_symbol_field_mismatch", "footprint_filters_mismatch"]),
    ("Silkscreen and text", ["silk_edge_clearance", "silk_over_copper", "silk_overlap", "text_height", "text_thickness",
                             "mirrored_text_on_front_layer", "nonmirrored_text_on_back_layer", "unresolved_variable"]),
]
SEV_LABEL = {
    "unconnected_items": "Unrouted connections", "shorting_items": "Items shorting two nets", "tracks_crossing": "Tracks crossing",
    "net_conflict": "Net conflicts", "copper_sliver": "Copper slivers", "isolated_copper": "Isolated copper",
    "starved_thermal": "Starved thermal reliefs", "items_not_allowed": "Items in a keepout", "zones_intersect": "Zones overlapping",
    "annular_width": "Annular ring", "drill_out_of_range": "Hole size", "microvia_drill_out_of_range": "Microvia hole size",
    "hole_to_hole": "Hole to hole", "holes_co_located": "Holes on top of each other", "copper_edge_clearance": "Copper to edge",
    "invalid_outline": "Board outline broken", "solder_mask_bridge": "Solder mask bridges", "padstack": "Padstack problems",
    "track_not_centered_on_via": "Track off a via's centre", "through_hole_pad_without_hole": "Through-hole pad without a hole",
    "item_on_disabled_layer": "Item on a disabled layer", "diff_pair_gap_out_of_range": "Pair gap",
    "diff_pair_uncoupled_length_too_long": "Pair uncoupled length", "length_out_of_range": "Length",
    "skew_out_of_range": "Skew", "too_many_vias": "Via count", "courtyards_overlap": "Courtyards overlapping",
    "missing_courtyard": "Missing courtyard", "malformed_courtyard": "Malformed courtyard",
    "pth_inside_courtyard": "Plated hole in a courtyard", "npth_inside_courtyard": "Unplated hole in a courtyard",
    "duplicate_footprints": "Duplicate footprints", "extra_footprint": "Footprint not in the schematic",
    "missing_footprint": "Footprint missing from the board", "footprint": "Footprint problems",
    "footprint_type_mismatch": "Footprint type (SMD / THT) mismatch", "lib_footprint_issues": "Library footprint missing",
    "lib_footprint_mismatch": "Footprint differs from its library", "footprint_symbol_mismatch": "Footprint and symbol disagree",
    "footprint_symbol_field_mismatch": "Footprint and symbol fields differ", "footprint_filters_mismatch": "Footprint filters",
    "silk_edge_clearance": "Silk to board edge", "silk_over_copper": "Silk over copper", "silk_overlap": "Silk overlapping",
    "mirrored_text_on_front_layer": "Mirrored text on the front", "nonmirrored_text_on_back_layer": "Unmirrored text on the back",
    "unresolved_variable": "Unresolved text variable", "via_dangling": "Via connected on one side only",
    "track_dangling": "Track with a free end", "track_on_post_machined_layer": "Track on a post-machined layer",
    "text_on_edge_cuts": "Text on the board edge layer", "missing_tuning_profile": "Missing tuning profile",
    "tuning_profile_track_geometries": "Tuning pattern geometry",
}


def _paths(tw):
    stem = os.path.splitext(tw.pcb)[0] if tw.pcb else None
    return tw.pro, (stem + ".kicad_dru") if stem else None


def _load_pro(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _match(pat, name):
    if pat.startswith("/") and pat.endswith("/") and len(pat) > 2:
        try:
            return re.search(pat[1:-1], name) is not None
        except re.error:
            return False
    short = name.rsplit("/", 1)[-1]
    return fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(short, pat)


def state(tw, board=None, netlist=None):
    """Everything the Rules tab shows."""
    pro_path, dru_path = _paths(tw)
    data = _load_pro(pro_path) if pro_path else {}
    ds = data.get("board", {}).get("design_settings", {})
    rules = ds.get("rules", {})
    layers = tw.setting("fab.layers") or (len(board.copper) if board is not None else 2)
    fab = dfm.profile(tw.setting("fab.house", "jlcpcb"), int(layers or 2))
    fab.update(tw.setting("fab.rules", {}) or {})
    cons = []
    for key, label, help_, fk, kind in CONSTRAINTS:
        if key not in rules and not fk:
            continue
        v = rules.get(key)
        lo = fab.get(fk) if fk else None
        rec = fab.get(fk.replace("min_", "rec_")) if fk and fk.startswith("min_") else None
        status = "ok"
        if isinstance(v, (int, float)) and lo is not None and v + 1e-9 < lo:
            status = "below"
        elif isinstance(v, (int, float)) and rec is not None and v + 1e-9 < rec:
            status = "tight"
        cons.append({"key": key, "label": label, "help": help_, "value": v, "kind": kind, "fab_min": lo, "fab_rec": rec,
                     "status": status})
    ns = data.get("net_settings", {})
    nets = sorted(board.nets) if board is not None else []
    nl_class = {}
    if netlist is not None:
        for full, cls in netlist.net_class.items():
            nl_class[full.rsplit("/", 1)[-1]] = cls
    ps = ProjectSettings(data)
    members = collections.defaultdict(list)
    for n in nets:
        members[ps.class_of(n, nl_class.get(n.rsplit("/", 1)[-1]))].append(n.rsplit("/", 1)[-1])
    classes = []
    for c in ns.get("classes", []) or [{"name": "Default"}]:
        name = c.get("name", "Default")
        classes.append({"name": name, **{k: c.get(k) for k, _ in CLASS_FIELDS}, "priority": c.get("priority"),
                        "nets": sorted(members.get(name, [])), "count": len(members.get(name, []))})
    patterns = []
    for p in ns.get("netclass_patterns", []) or []:
        pat, cls = p.get("pattern", ""), p.get("netclass", "")
        hits = [n.rsplit("/", 1)[-1] for n in nets if _match(pat, n)]
        patterns.append({"pattern": pat, "netclass": cls, "matches": len(hits), "examples": hits[:6],
                         "known_class": cls in {c["name"] for c in classes}})
    sev = ds.get("rule_severities", {})
    groups, seen = [], set()
    for title, keys in SEV_GROUPS:
        rows = [{"key": k, "label": SEV_LABEL.get(k, k.replace("_", " ").capitalize()), "severity": sev[k]} for k in keys if k in sev]
        seen |= {k for k in keys}
        if rows:
            groups.append({"title": title, "rows": rows})
    rest = [{"key": k, "label": SEV_LABEL.get(k, k.replace("_", " ").capitalize()), "severity": v} for k, v in sorted(sev.items())
            if k not in seen]
    if rest:
        groups.append({"title": "Other", "rows": rest})
    text = ""
    if dru_path and os.path.exists(dru_path):
        with open(dru_path, encoding="utf-8") as f:
            text = f.read()
    druls, problems = twdru.check(text) if text.strip() else ([], [])
    return {
        "files": {"project": os.path.relpath(pro_path, tw.root) if pro_path else None,
                  "rules": os.path.relpath(dru_path, tw.root) if dru_path else None, "rules_exists": bool(text)},
        "fab": {"house": fab.get("_house"), "layers": fab.get("_layers"), "source": fab.get("_source"), "date": fab.get("_date")},
        "constraints": cons,
        "booleans": {k: v for k, v in rules.items() if isinstance(v, bool)},
        "classes": classes, "patterns": patterns, "severities": groups,
        "presets": {"track_widths": [w for w in ds.get("track_widths", []) if w],
                    "via_dimensions": [v for v in ds.get("via_dimensions", []) if v.get("diameter")],
                    "diff_pair_dimensions": [d for d in ds.get("diff_pair_dimensions", []) if d.get("width")]},
        "dru": {"text": text, "rules": druls, "problems": problems},
        "unassigned": sorted(members.get("Default", []))[:40],
    }


def save(tw, body, probe=True):
    """Apply {constraints: {key: value}, classes: [...], patterns: [...], severities: {key: sev},
    presets: {...}, dru: text}. Returns (ok, messages); nothing is written unless everything checks out."""
    pro_path, dru_path = _paths(tw)
    msgs = []
    data = _load_pro(pro_path) if pro_path else None
    if data is None or not pro_path:
        return False, ["this project has no .kicad_pro to write the rules into"]
    before = json.dumps(data, sort_keys=True)
    ds = data.setdefault("board", {}).setdefault("design_settings", {})
    rules = ds.setdefault("rules", {})
    for k, v in (body.get("constraints") or {}).items():
        if k not in {c[0] for c in CONSTRAINTS} and not isinstance(rules.get(k), bool):
            return False, [f"unknown rule {k}"]
        if isinstance(rules.get(k), bool):
            rules[k] = bool(v)
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False, [f"{k}: '{v}' is not a number"]
        if fv < 0 or fv > 50:
            return False, [f"{k}: {fv} is out of range"]
        rules[k] = int(fv) if k == "min_resolved_spokes" else round(fv, 6)
    if "severities" in body:
        sev = ds.setdefault("rule_severities", {})
        for k, v in body["severities"].items():
            if v not in ("error", "warning", "ignore"):
                return False, [f"{k}: severity must be error, warning or ignore"]
            sev[k] = v
    ns = data.setdefault("net_settings", {})
    if "classes" in body:
        old = {c.get("name"): c for c in ns.get("classes", [])}
        out = []
        names = set()
        for c in body["classes"]:
            name = str(c.get("name", "")).strip()
            if not name or name in names:
                return False, [f"net class names must be unique and not empty ({name!r})"]
            names.add(name)
            base = dict(old.get(name) or old.get("Default") or {})
            base["name"] = name
            for k, _ in CLASS_FIELDS:
                if c.get(k) not in (None, ""):
                    try:
                        base[k] = round(float(c[k]), 6)
                    except (TypeError, ValueError):
                        return False, [f"net class {name}: {k} '{c[k]}' is not a number"]
            if "priority" not in base:
                base["priority"] = 2147483647 if name == "Default" else len(out)
            out.append(base)
        if "Default" not in names:
            return False, ["the Default net class cannot be removed"]
        ns["classes"] = out
    if "patterns" in body:
        known = {c.get("name") for c in ns.get("classes", [])}
        pats = []
        for p in body["patterns"]:
            pat, cls = str(p.get("pattern", "")).strip(), str(p.get("netclass", "")).strip()
            if not pat:
                continue
            if cls not in known:
                return False, [f"pattern {pat}: net class {cls} does not exist"]
            if pat.startswith("/") and pat.endswith("/"):
                try:
                    re.compile(pat[1:-1])
                except re.error as e:
                    return False, [f"pattern {pat}: {e}"]
            pats.append({"netclass": cls, "pattern": pat})
        ns["netclass_patterns"] = pats
    for k in ("track_widths", "via_dimensions", "diff_pair_dimensions"):
        if k in (body.get("presets") or {}):
            ds[k] = body["presets"][k]
    text = body.get("dru")
    if text is not None:
        _, problems = twdru.check(text) if text.strip() else ([], [])
        errs = [p for p in problems if p["severity"] == "error"]
        if errs:
            return False, [f"custom rules, line {p['line']}: {p['message']}" for p in errs[:8]]
        if probe and text.strip() and tw.pcb and os.path.exists(tw.pcb):
            ok, why = twdru.kicad_accepts(tw.pcb, text)
            if not ok:
                return False, [f"custom rules: {why}"]
        msgs += [f"custom rules, line {p['line']}: {p['message']}" for p in problems if p["severity"] == "warning"]
    if json.dumps(data, sort_keys=True) != before:          # KiCad's own layout: two spaces, no final newline
        with open(pro_path + ".tmp", "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(pro_path + ".tmp", pro_path)
    if text is not None and dru_path:
        body_txt = text if text.strip().startswith("(version") or not text.strip() else "(version 1)\n" + text
        with open(dru_path + ".tmp", "w", encoding="utf-8") as f:
            f.write(body_txt.rstrip() + "\n")
        os.replace(dru_path + ".tmp", dru_path)
    return True, msgs
