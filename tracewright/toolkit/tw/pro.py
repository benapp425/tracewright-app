"""The .kicad_pro settings the checks need: net classes, their patterns, and the board rules."""
import json, fnmatch, re

DEFAULT_CLASS = {"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3,
                 "diff_pair_width": 0.2, "diff_pair_gap": 0.25}


class ProjectSettings:
    def __init__(self, data=None):
        self.data = data or {}
        ns = self.data.get("net_settings", {})
        self.classes = {c.get("name", "Default"): c for c in ns.get("classes", [])} or {"Default": dict(DEFAULT_CLASS)}
        self.patterns = [(p.get("pattern", ""), p.get("netclass", "Default")) for p in ns.get("netclass_patterns", []) or []]
        self.assignments = ns.get("netclass_assignments") or {}
        ds = self.data.get("board", {}).get("design_settings", {})
        self.rules = ds.get("rules", {})
        self.track_widths = [w for w in ds.get("track_widths", []) if w]
        self.via_dims = [v for v in ds.get("via_dimensions", []) if v.get("diameter")]
        self.severities = ds.get("rule_severities", {})

    @classmethod
    def load(cls, path):
        try:
            with open(path) as f:
                return cls(json.load(f))
        except (OSError, ValueError):
            return cls({})

    def class_of(self, net, netlist_class=None):
        """KiCad's rule: explicit assignment, else the first matching pattern, else the netlist's class."""
        if net in self.assignments:
            a = self.assignments[net]
            return a[0] if isinstance(a, list) and a else a
        for pat, cls in self.patterns:
            if _match(pat, net):
                return cls
        if netlist_class and netlist_class in self.classes:
            return netlist_class
        return "Default"

    def cls(self, name):
        c = dict(DEFAULT_CLASS)
        c.update(self.classes.get("Default", {}))
        c.update(self.classes.get(name, {}))
        return c

    def rule(self, key, default=None):
        return self.rules.get(key, default)


def _match(pat, net):
    if not pat:
        return False
    if pat.startswith("/") and pat.endswith("/") and len(pat) > 2:     # regex patterns are written /re/
        try:
            return re.search(pat[1:-1], net) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(net, pat) or fnmatch.fnmatchcase(net.rsplit("/", 1)[-1], pat)
