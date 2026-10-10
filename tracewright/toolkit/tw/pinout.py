"""Connector pinouts from the design itself: every connector's pins and the net on each, read from KiCad's netlist, so the
document matches the schematic after any change (a pin plan, an edit) without anyone copying tables by hand.

    path = write(project)        docs/connectors.md
    rows = table(project)        {ref: [(pin, net or "", pin name)]}
"""
import os, re

CONN = re.compile(r"^(J|P|CN|X|CON)\d", re.I)


def _key(pin):
    m = re.match(r"^([A-Za-z]*)(\d+)$", pin)
    return (m.group(1), int(m.group(2))) if m else (pin, 0)


def table(project):
    from .netlist import Netlist
    nl = Netlist.load(os.path.join(project.build, f"{project.stem}.net"))
    out = {}
    for (ref, pin), net in nl.pin.items():
        if CONN.match(ref):
            out.setdefault(ref, []).append((pin, net, (nl.pin_info.get((ref, pin)) or {}).get("name", "")))
    for ref, part in nl.parts.items():                  # pins on no net: from the part's library pins
        if not CONN.match(ref):
            continue
        have = {r[0] for r in out.get(ref, [])}
        for pin, (name, _t) in (nl.libpins.get((part.get("lib", ""), part.get("part", ""))) or {}).items():
            if pin not in have:
                out.setdefault(ref, []).append((pin, "", name))
    for ref in out:
        out[ref].sort(key=lambda r: _key(r[0]))
    return dict(sorted(out.items(), key=lambda kv: _key(kv[0])))


def write(project):
    """docs/connectors.md from the netlist; returns its path."""
    from .netlist import Netlist
    nl = Netlist.load(os.path.join(project.build, f"{project.stem}.net"))
    lines = ["# Connector pinouts", "", "_Written by `./tw pinout` from KiCad's netlist: it matches the schematic as it is now._", ""]
    for ref, rows in table(project).items():
        part = nl.parts.get(ref, {})
        what = " ".join(x for x in (part.get("value"), part.get("description")) if x)
        lines += [f"## {ref}" + (f": {what}" if what else ""), "", "| Pin | Net |", "|---|---|"]
        for pin, net, name in rows:
            n = (net or "").rsplit("/", 1)[-1]
            if not n or n.startswith("unconnected-"):
                n = "not connected"
            lines.append(f"| {pin} | {n} |")
        lines.append("")
    os.makedirs(os.path.join(project.root, "docs"), exist_ok=True)
    path = os.path.join(project.root, "docs", "connectors.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path
