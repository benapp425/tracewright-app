"""A circuit block from the schematic, simulated: the chosen parts' lines from KiCad's SPICE export, cleaned (values
reduced to their number, nets to plain node names, ground to node 0), the parts without a model left out and named, and
the stimulus the user gives -- a supply or a signal on a net, a load, a few extra parts (a bus's capacitance) -- run by
tw/sim.py and kept with the other simulations (docs/sim).

    nl, notes, nodes = netlist(project, refs, sources=[...], loads=[...], extra=[...], analysis={...})
    info = run(project, name, refs, ..., probes=["I2C_SDA"])        # probes are net names

    sources: [{"net": "+3V3", "kind": "dc" | "step" | "pulse" | "sine", "v": 3.3, "freq": 1e3, "rise": 1e-9}]
    loads:   [{"net": "+3V3", "r": 33}] | [{"net": "+3V3", "i": 0.1}]
    extra:   [{"kind": "C" | "R" | "L", "net": "I2C_SDA", "to": "GND", "value": "100p"}]
    analysis: {"kind": "tran", "stop": 2e-6} | {"kind": "ac", "from": 10, "to": 1e7} | {"kind": "op"}
"""
import os, re

SI = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?:meg|[fpnumkgt]|µ)?", re.I)
KEEP = "RCLD"                      # parts whose lines run as they are (a diode gets the default diode model)


def _node(net, ground):
    if net in ground or (net or "").rsplit("/", 1)[-1] in ground:
        return "0"
    s = re.sub(r"[^A-Za-z0-9_]", "_", (net or "").rsplit("/", 1)[-1].lstrip("+")) or "n"
    if (net or "").lstrip("/").rsplit("/", 1)[-1].startswith("+"):
        s = "p" + s
    return s.lower()                                   # ngspice keeps node names in lower case


def _value(tokens):
    """The first token that reads as a SPICE value ('10u 25V' -> '10u', '4.7k' -> '4.7k', '4k7' -> '4.7k')."""
    for t in tokens:
        t = t.replace("µ", "u")
        m = re.fullmatch(r"(\d+)([kKmMuUnNpP])(\d+)", t)              # 4k7, 2u2
        if m:
            return f"{m.group(1)}.{m.group(3)}{m.group(2).lower()}"
        if SI.match(t):
            return SI.match(t).group(0)
    return None


def export(project):
    """{ref: (kind letter, [nets], value tokens)} from KiCad's SPICE export of the whole schematic."""
    from . import kicad
    out_path = os.path.join(project.build, "blocksim-export.cir")
    os.makedirs(project.build, exist_ok=True)
    kicad.cli("sch", "export", "netlist", "--format", "spice", "-o", out_path, project.sch, ok=(0,))
    parts = {}
    for line in open(out_path, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith(("*", ".")):
            continue
        tok = line.split()
        ref = tok[0]
        if len(tok) >= 2 and tok[1].startswith("__"):
            parts[ref] = (ref[:1].upper(), [], [])                    # no model: KiCad writes a placeholder
            continue
        parts[ref] = (ref[:1].upper(), tok[1:3], tok[3:])
    return parts


def netlist(project, refs, sources=(), loads=(), extra=(), analysis=None, ground=("GND", "GNDA", "GNDD", "0", "VSS")):
    """(netlist text, notes, {net: node}) for the block."""
    parts = export(project)
    ground = set(ground)
    lines, notes, nodes, used = [f"* {project.name}: {', '.join(refs)}"], [], {}, []
    diode = False

    def node(net):
        n = _node(net, ground)
        nodes[net.rsplit("/", 1)[-1]] = n
        return n
    for ref in refs:
        if ref not in parts:
            notes.append(f"{ref} is not in the schematic")
            continue
        kind, nets, vals = parts[ref]
        if kind not in KEEP or len(nets) != 2:
            notes.append(f"{ref} left out: it has no SPICE model (give it one in KiCad's simulation model fields)")
            continue
        v = _value(vals)
        if kind != "D" and not v:
            notes.append(f"{ref} left out: no value reads as a number ({' '.join(vals) or 'empty'})")
            continue
        if kind == "D":
            lines.append(f"{ref} {node(nets[0])} {node(nets[1])} Dtw")
            diode = True
        else:
            lines.append(f"{ref} {node(nets[0])} {node(nets[1])} {v}")
        used.append(ref)
    if not used:
        raise ValueError("none of the parts can be simulated: " + "; ".join(notes))
    for k, s in enumerate(sources or [], 1):
        n = node(s["net"])
        v = float(s.get("v", 3.3))
        kind = s.get("kind", "dc")
        if kind == "dc":
            spec = f"DC {v:g}"
        elif kind == "step":
            spec = f"PULSE(0 {v:g} 0 {float(s.get('rise', 1e-9)):g} {float(s.get('rise', 1e-9)):g} 1 2)"
        elif kind == "pulse":
            f = float(s.get("freq", 1e3))
            spec = f"PULSE(0 {v:g} 0 {float(s.get('rise', 1e-9)):g} {float(s.get('rise', 1e-9)):g} {0.5 / f:g} {1 / f:g})"
        elif kind == "sine":
            spec = f"SIN({float(s.get('offset', 0)):g} {v:g} {float(s.get('freq', 1e3)):g})"
        else:
            raise ValueError(f"source kind {kind}: dc, step, pulse or sine")
        lines.append(f"Vsrc{k} {n} 0 {spec} AC 1")
    for k, l in enumerate(loads or [], 1):
        n = node(l["net"])
        if l.get("r"):
            lines.append(f"Rload{k} {n} 0 {float(l['r']):g}")
        elif l.get("i"):
            lines.append(f"Iload{k} {n} 0 DC {float(l['i']):g}")
    for k, e in enumerate(extra or [], 1):
        kind = str(e.get("kind", "C")).upper()[:1]
        if kind not in "RCL":
            raise ValueError("extra parts: R, C or L")
        v = _value([str(e.get("value", ""))])
        if not v:
            raise ValueError(f"extra {kind}: no value")
        lines.append(f"{kind}x{k} {node(e['net'])} {node(e.get('to', 'GND'))} {v}")
    if diode:
        lines.append(".model Dtw D(IS=1e-14 N=1.05 RS=0.1)")
    a = analysis or {"kind": "tran", "stop": 1e-3}
    if a["kind"] == "tran":
        stop = float(a.get("stop", 1e-3))
        lines.append(f".tran {stop / 1000:g} {stop:g}")
    elif a["kind"] == "ac":
        lines.append(f".ac dec 40 {float(a.get('from', 10)):g} {float(a.get('to', 1e7)):g}")
    elif a["kind"] == "op":
        lines.append(".op")
    else:
        raise ValueError("analysis: tran, ac or op")
    lines.append(".end")
    return "\n".join(lines) + "\n", notes, nodes


def run(project, name, refs, sources=(), loads=(), extra=(), analysis=None, probes=(), title="", check=None):
    """Build, run and keep it (docs/sim/<name>): the saved run with its plot, and what was left out."""
    from . import sim
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "block"
    cir, notes, nodes = netlist(project, refs, sources, loads, extra, analysis)
    pr = []
    for p in probes or []:
        n = nodes.get(str(p).rsplit("/", 1)[-1])
        if n is None or n == "0":
            raise ValueError(f"no node for {p}: probe a net of the block")
        pr.append(f"v({n})")
    if not pr:
        pr = [f"v({n})" for n in sorted(set(nodes.values())) if n != "0"][:4]
    info = sim.simulate(os.path.join(project.root, "docs", "sim"), name, cir, pr, title or f"{', '.join(refs)}", check=check)
    info["notes"] = notes
    info["nodes"] = nodes
    return info
