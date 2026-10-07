"""The routing plan: which nets the router takes on its own, which it routes with rules it must keep, and which are left
for a person (or Claude in the editor) -- each with the reason and the rules, so nobody has to guess.

    plan = classify(project)     {net: {"mode": "auto" | "guided" | "hand", "why", "rules": [..], "order", "kind",
                                         "layers": [..] | None, "suggest": {"layers", "why"} | None}}
    set_mode(project, net, mode) / set_layers(project, net, layers) / preset(project) / set_preset(project, name)
    PRESETS: how the router weighs vias against length and bends

Hand: RF feeds (an antenna's line wants its impedance kept, short and straight), current-sense lines (a Kelvin pair
read at the resistor's pads), high voltage (creepage). Guided: differential pairs (routed together at their gap and
impedance), clocks and crystals (short, no vias), switching nodes (short and wide), heavy currents (wide), length
groups. Auto: the rest. The user can move any net to another mode (tracewright.json route.modes), and the router
follows: hand nets are left alone, guided ones go first with their rules.

The layer plan (route.layers {net: [layers]}): the layers a net is routed on, with a short escape on its pads' own layer.
Pairs, fast lines and RF get a suggestion (the signal layers beside a ground plane); it is kept only once set.
"""
import re

MODES = ("auto", "guided", "hand")
PRESETS = {
    "balanced": {"label": "Balanced", "via": 1000.0, "bend45": 40, "bend90": 140, "why": "A via costs about 10 mm of track."},
    "dense": {"label": "Dense", "via": 450.0, "bend45": 30, "bend90": 110, "why": "Vias are cheap: for crowded boards and fan-outs."},
    "clean": {"label": "Few vias", "via": 2200.0, "bend45": 60, "bend90": 220, "why": "Stays on one layer and turns less, at some length."},
    "short": {"label": "Shortest", "via": 700.0, "bend45": 20, "bend90": 80, "why": "The shortest tracks, more corners and vias."},
}
SAYS = {"auto": "the router takes it", "guided": "it is routed first with its rules", "hand": "it is left for hand routing"}
I2C = re.compile(r"(^|[_/])(SCL|SDA|I2C\w*)([_\d]|$)", re.I)       # a bus clock, but slow: no special care
SENSE = re.compile(r"(^|[_/])(I?SENSE|ISNS|SNS|CS[PN+-]?|KELVIN)([_\d]|$)", re.I)


def _cfg(project):
    return getattr(project, "cfg", None) or {}


def _save(project):
    """The app's project saves with save(), the toolkit's with save_cfg()."""
    for m in ("save", "save_cfg"):
        f = getattr(project, m, None)
        if callable(f):
            return f()


def preset(project):
    p = (_cfg(project).get("route") or {}).get("preset")
    return p if p in PRESETS else "balanced"


def set_preset(project, name):
    if name not in PRESETS:
        raise ValueError(f"preset: one of {', '.join(PRESETS)}")
    project.cfg.setdefault("route", {})["preset"] = name
    _save(project)


def overrides(project):
    return dict((_cfg(project).get("route") or {}).get("modes") or {})


def layer_plan(project):
    return dict((_cfg(project).get("route") or {}).get("layers") or {})


def set_layers(project, net, layers, copper=None):
    """The layers `net` is routed on (None: any). copper: the board's layers, to check the names."""
    lp = project.cfg.setdefault("route", {}).setdefault("layers", {})
    if not layers:
        lp.pop(net, None)
    else:
        layers = [str(l) for l in layers]
        bad = [l for l in layers if not l.endswith(".Cu") or (copper and l not in copper)]
        if bad:
            raise ValueError(f"not a copper layer of this board: {', '.join(bad)}")
        lp[net] = layers
    _save(project)


def _beside_ground(project):
    """Signal layers next to a ground plane, from the stack-up plan (None without one)."""
    from . import stackup
    plan = stackup.get(_cfg(project))
    if not plan or plan.get("layers", 2) < 4:
        return None
    ls = stackup.names(plan["layers"])
    gnd = lambda n: bool(re.fullmatch(r"(GND|DGND|VSS|0V|AGND)", _short(n), re.I))
    out = []
    for i, (l, r) in enumerate(zip(ls, plan["roles"])):
        if r == "signal" and any(0 <= j < len(ls) and plan["roles"][j] == "plane" and gnd(plan["planes"].get(ls[j], ""))
                                 for j in (i - 1, i + 1)):
            out.append(l)
    return out or None


def set_mode(project, net, mode):
    if mode not in MODES and mode is not None:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    modes = project.cfg.setdefault("route", {}).setdefault("modes", {})
    if mode is None:
        modes.pop(net, None)
    else:
        modes[net] = mode
    _save(project)


def _short(n):
    return str(n).rsplit("/", 1)[-1]


def classify(project, nl=None, model=None):
    """Every routed net's mode, reason and rules. nl: the netlist (pins), model: the net model (kinds, currents)."""
    from .checks.context import Context
    ctx = None
    if nl is None or model is None:
        ctx = Context(project, offline=True)
        nl = nl or ctx.netlist
        if model is None:
            from .netmodel import for_context
            model = for_context(ctx)
    recs = model.records if model is not None else {}
    over = overrides(project)
    lplan = layer_plan(project)
    beside = _beside_ground(project)
    parts = getattr(nl, "parts", {}) or {}
    out = {}
    for net, nodes in (nl.nets or {}).items():
        if not net or net.startswith("unconnected-") or len(nodes) < 2:
            continue
        r = recs.get(net) or {}
        kind = r.get("kind") or "signal"
        if kind == "ground" or (kind == "power" and not r.get("current")):
            continue                                   # planes and pours carry these
        refs = {ref for ref, _ in nodes}
        name = _short(net)
        amps = r.get("current")
        volts = r.get("voltage")
        mode, why, rules, order = "auto", "", [], 3
        if kind == "rf":
            mode, why = "hand", "RF line: its impedance kept the whole way, short and straight, nothing beside it"
            rules = [f"{r.get('impedance', 50):g} Ω", "no vias", "ground on both sides"]
        elif SENSE.search(name) and kind in ("analog", "signal"):
            mode, why = "hand", "current sense: a Kelvin pair read at the resistor's pads, away from the current path"
            rules = ["from the resistor's pads", "side by side", "away from switching nodes"]
        elif volts is not None and abs(float(volts)) >= 60:
            mode, why = "hand", f"{volts:g} V: creepage and clearance set by the voltage"
            rules = ["wide clearance", "no thin necks"]
        elif kind == "pair":
            mode, order = "guided", 0
            why = f"differential pair with {_short(r.get('pair', ''))}"
            rules = ["routed together", "at the pair's gap"] + ([f"{r['impedance']:g} Ω"] if r.get("impedance") else []) + ["lengths matched"]
        elif any(ref[:1] in ("Y", "X") and ref[1:2].isdigit() for ref in refs):
            mode, order = "guided", 1
            why, rules = "crystal line: short, no vias, kept clear of other signals", ["no vias", "short", "ground guard"]
        elif kind == "clock" and not I2C.search(name):
            mode, order = "guided", 1
            why, rules = "clock: short, no stubs, one layer", ["no stubs", "few vias"]
        elif _switch_node(net, nodes, parts):
            mode, order = "guided", 0
            why, rules = "switching node: short and wide (it radiates)", ["short", "wide", "no vias"]
        elif amps is not None and float(amps) >= 1.0:
            mode, order = "guided", 2
            why, rules = f"{amps:g} A: wide copper", ["width for the current"]
        elif kind == "fast":
            mode, order = "guided", 2
            why, rules = "fast signal: over a solid plane, few vias", ["over its plane", "few vias"]
        elif kind == "power":
            why = (f"a supply ({amps:g} A): routed first, at its class's width" if amps is not None
                   else "a supply: routed first, at its class's width")
        elif I2C.search(name):
            why = "I2C: slow edges, the router takes it"
        else:
            why = "an ordinary signal"
        if net in over or name in over:
            m = over.get(net) or over.get(name)
            if m in MODES and m != mode:
                why = f"set by you; by rule {SAYS[mode]} ({why})"
                mode = m
        layers = lplan.get(net) or lplan.get(name)
        suggest = None
        if beside and kind in ("pair", "fast", "clock", "rf"):
            sl = beside[:1] if kind == "rf" else beside
            suggest = {"layers": sl, "why": "over a ground plane: a clean return path" + (" and its impedance" if kind in ("pair", "rf") else "")}
        out[net] = {"mode": mode, "why": why, "rules": rules, "order": order, "kind": kind, "layers": layers, "suggest": suggest}
    return out


def _switch_node(net, nodes, parts):
    """A net on an inductor and a converter's switch pin."""
    refs = {ref for ref, _ in nodes}
    if not any(ref[:1] == "L" and ref[1:2].isdigit() for ref in refs):
        return False
    return bool(re.search(r"(^|[_/])(SW|LX|PH)(\d|_|$)", _short(net), re.I))
