"""Board stack-ups, 2 to 12 copper layers: what each layer is for (signal or plane), the net each plane carries, the
direction each signal layer is routed in, and the build between them (the fab's standard stack-up: dielectric
thicknesses and permittivity, which set trace impedance).

A plan lives in tracewright.json as "stackup":

    {"layers": 6, "preset": "JLC06161H-2116",
     "roles": ["signal", "plane", "signal", "plane", "plane", "signal"],      # F.Cu, In1.Cu ... B.Cu
     "planes": {"In1.Cu": "GND", "In3.Cu": "+3V3", "In4.Cu": "GND"},         # plane layer -> its net
     "directions": {"In2.Cu": "x"},                                           # signal layers: x | y | any
     "why": "two signal layers next to ground for the USB pair ..."}

The router routes on the signal layers only (each in its direction, vias through every layer), the planes are poured
over the whole board; the checks read the planes for return paths and impedance.

    from tw import stackup
    plan, problems = stackup.validate(plan, nets)        # problems: [(severity, text)]
    stackup.describe(plan)                               # one line per layer, in words
    stackup.apply(project, plan)                         # the board: layer count, plane layers, the build, the pours

Thicknesses are the fab's typical values (JLCPCB's standard builds); for impedance-controlled work check the fab's
current stack-up and its impedance calculator, then set the plan's preset to match."""
import os, re
from .sexp import parse, find

COUNTS = (2, 4, 6, 8, 10, 12)
ROLES = ("signal", "plane")
DIRS = ("x", "y", "any")

# The fab's standard 1.6 mm builds, top to bottom: ("copper", mm) | ("prepreg"|"core", mm, er, material)
PRESETS = {
    "JLC-2L-1.6": {"layers": 2, "title": "2 layers, 1.6 mm FR-4", "build": [
        ("copper", 0.035), ("core", 1.51, 4.5, "FR4"), ("copper", 0.035)]},
    "JLC04161H-7628": {"layers": 4, "title": "4 layers, 1.6 mm, 7628 prepreg (0.21 mm to the planes)", "build": [
        ("copper", 0.035), ("prepreg", 0.2104, 4.4, "7628"), ("copper", 0.0152), ("core", 1.065, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.2104, 4.4, "7628"), ("copper", 0.035)]},
    "JLC04161H-3313": {"layers": 4, "title": "4 layers, 1.6 mm, 3313 prepreg (0.10 mm to the planes: narrower 50 ohm tracks)", "build": [
        ("copper", 0.035), ("prepreg", 0.0994, 4.1, "3313"), ("copper", 0.0152), ("core", 1.265, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.0994, 4.1, "3313"), ("copper", 0.035)]},
    "JLC06161H-2116": {"layers": 6, "title": "6 layers, 1.6 mm, 2116 prepreg", "build": [
        ("copper", 0.035), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.55, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.55, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.035)]},
    "JLC08161H-2116": {"layers": 8, "title": "8 layers, 1.6 mm, 2116 prepreg", "build": [
        ("copper", 0.035), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.335, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.335, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.335, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.035)]},
    "JLC10161H-2116": {"layers": 10, "title": "10 layers, 1.6 mm, 2116 prepreg", "build": [
        ("copper", 0.035), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.21, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.21, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.21, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.21, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.035)]},
    "12L-1.6-2116": {"layers": 12, "title": "12 layers, 1.6 mm, 2116 prepreg (a typical build: confirm it with the fab)", "build": [
        ("copper", 0.035), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.145, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.145, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.145, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.145, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.0152), ("core", 0.145, 4.6, "FR4"),
        ("copper", 0.0152), ("prepreg", 0.1088, 4.16, "2116"), ("copper", 0.035)]},
}
DEFAULT_PRESET = {2: "JLC-2L-1.6", 4: "JLC04161H-7628", 6: "JLC06161H-2116", 8: "JLC08161H-2116", 10: "JLC10161H-2116",
                  12: "12L-1.6-2116"}

# Where to start: every signal layer next to a plane, ground planes paired with the fast layers.
#   G ground plane, P supply plane, S signal
TEMPLATES = {2: "SS", 4: "SGPS", 6: "SGSPGS", 8: "SGSGPSGS", 10: "SGSSGPSSGS", 12: "SGSGSPGSGSGS"}
# HDI with microvias: a signal layer under each outer layer, where the microvias from the pads land
TEMPLATES_HDI = {4: "SSGS", 6: "SSGPSS", 8: "SSGSPGSS", 10: "SSGSGPSGSS", 12: "SSGSGSPGSGSS"}


def names(n):
    """The copper layers top to bottom: F.Cu, In1.Cu ... B.Cu."""
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]


def short(n):
    return (n or "").rsplit("/", 1)[-1]


def _supply(nets):
    """The main supply: a 3.3 V rail if there is one, else the supply net with the most pads."""
    from .nettypes import classify
    kinds = classify(list(nets or {}))
    pw = [(n, c) for n, c in (nets or {}).items() if kinds.get(n, {}).get("kind") == "power"]
    for want in ("+3V3", "3V3", "+3.3V", "VCC", "+5V"):
        for n, _ in pw:
            if short(n).upper() == want.upper():
                return n
    return max(pw, key=lambda x: x[1])[0] if pw else ""


def _ground(nets):
    for n in nets or {}:
        if re.fullmatch(r"(GND|DGND|0V|VSS)", short(n), re.I):
            return n
    return "GND"


def default_plan(n, nets=None, hdi=False):
    """A sensible plan for n layers. nets: {net: pad count} (to pick the ground and supply nets); hdi: the microvia
    layout (signal layers under the outer ones)."""
    if n not in COUNTS:
        raise ValueError(f"{n} layers: choose 2, 4, 6, 8, 10 or 12")
    t = TEMPLATES_HDI[n] if hdi and n in TEMPLATES_HDI else TEMPLATES[n]
    ls = names(n)
    roles = ["signal" if c == "S" else "plane" for c in t]
    gnd, sup = _ground(nets), _supply(nets) or _ground(nets)
    planes = {l: (gnd if c == "G" else sup) for l, c in zip(ls, t) if c != "S"}
    directions, k = {}, 0
    for l, r in zip(ls, roles):
        if r == "signal":
            if l in ("F.Cu", "B.Cu"):
                directions[l] = "any"
            else:
                directions[l] = "x" if k % 2 == 0 else "y"
                k += 1
    return {"layers": n, "preset": DEFAULT_PRESET[n], "roles": roles, "planes": planes, "directions": directions}


def validate(plan, nets=None, limit=None, hdi_cfg=None):
    """(plan, problems): the plan filled in and tidied, and what is wrong with it -- [("error"|"warning", text)].
    nets: the board's or schematic's nets (names or {name: pads}); limit: the agreed copper layer count, if any;
    hdi_cfg: the project's HDI settings (microvias land on the layer under each outer one)."""
    p = dict(plan or {})
    out = []
    try:
        n = int(p.get("layers") or len(p.get("roles") or []) or 0)
    except (TypeError, ValueError):
        n = 0
    if n not in COUNTS:
        return p, [("error", f"{n or 'no'} copper layers: a board has 2, 4, 6, 8, 10 or 12")]
    p["layers"] = n
    micro = bool(hdi_cfg and hdi_cfg.get("on") and hdi_cfg.get("microvias"))
    base = default_plan(n, nets if isinstance(nets, dict) else {x: 1 for x in nets or []}, hdi=micro and bool(p.get("hdi_layout", True)))
    ls = names(n)
    roles = [str(r).lower() for r in (p.get("roles") or base["roles"])]
    roles = ["plane" if r in ("plane", "power", "ground", "gnd") else "signal" if r in ("signal", "mixed", "s") else r for r in roles]
    if len(roles) != n:
        out.append(("error", f"{len(roles)} roles for {n} layers: give one per layer, F.Cu first"))
        roles = base["roles"]
    for l, r in zip(ls, roles):
        if r not in ROLES:
            out.append(("error", f"{l}: '{r}' is not a role (signal or plane)"))
    if roles[0] != "signal" or roles[-1] != "signal":
        out.append(("error", "the outer layers carry the parts: F.Cu and B.Cu must be signal layers"))
    p["roles"] = roles
    known = set(nets or [])
    planes = {}
    for l, r in zip(ls, roles):
        if r != "plane":
            continue
        net = (p.get("planes") or {}).get(l) or base["planes"].get(l) or _ground(nets)
        if known and net not in known and not any(short(k) == short(net) for k in known):
            out.append(("error", f"{l}: no net {short(net)} on this board"))
        planes[l] = next((k for k in known if short(k) == short(net)), net) if known else net
    p["planes"] = planes
    dirs = {}
    for l, r in zip(ls, roles):
        if r != "signal":
            continue
        d = str((p.get("directions") or {}).get(l) or base["directions"].get(l) or "any").lower()
        if d in ("h", "horizontal"):
            d = "x"
        elif d in ("v", "vertical"):
            d = "y"
        if d not in DIRS:
            out.append(("error", f"{l}: direction '{d}' (x, y or any)"))
            d = "any"
        dirs[l] = d
    p["directions"] = dirs
    pre = p.get("preset") or DEFAULT_PRESET[n]
    if pre not in PRESETS:
        out.append(("error", f"no stack-up {pre}: " + ", ".join(k for k, v in PRESETS.items() if v["layers"] == n)))
        pre = DEFAULT_PRESET[n]
    elif PRESETS[pre]["layers"] != n:
        out.append(("error", f"{pre} is a {PRESETS[pre]['layers']}-layer build, not {n}"))
        pre = DEFAULT_PRESET[n]
    p["preset"] = pre
    if limit and int(limit) != n:
        out.append(("error", f"the agreed limit is {limit} copper layers: ask the user before changing it"))
    if n >= 4:
        if not planes:
            out.append(("warning", "no plane: fast signals get no return path and the board radiates"))
        for i, (l, r) in enumerate(zip(ls, roles)):
            if r == "signal" and not any(0 <= j < n and roles[j] == "plane" for j in (i - 1, i + 1)):
                out.append(("warning", f"{l} has no plane next to it: its traces have no reference (impedance, return current)"))
        for i in range(n - 1):
            if roles[i] == roles[i + 1] == "signal" and 0 < i < n - 2:
                out.append(("warning", f"{ls[i]} and {ls[i + 1]} are signal layers side by side: route them crosswise (x and y)"))
                if dirs.get(ls[i]) == dirs.get(ls[i + 1]) and dirs.get(ls[i]) != "any":
                    out.append(("warning", f"{ls[i]} and {ls[i + 1]} both run along {dirs[ls[i]]}: broadside coupling"))
        if not any(short(v).upper() in ("GND", "DGND", "VSS", "0V") for v in planes.values()):
            out.append(("warning", "no ground plane"))
        if micro:
            for l in (ls[1],):                       # where the top side's microvias land (most BGAs sit on top)
                if roles[ls.index(l)] == "plane":
                    out.append(("warning", f"microvias land on {l}, a {short(planes.get(l, ''))} plane: ground balls drop "
                                           f"straight onto it, but signals cannot escape there (make {l} a signal layer)"))
    if p.get("why") is not None:
        p["why"] = str(p["why"])[:600]
    return p, out


def describe(plan):
    """The plan in words, one line per layer."""
    if not plan:
        return "no stack-up chosen (2 signal layers; inner layers, if any, are planes)"
    pre = PRESETS.get(plan.get("preset"), {})
    lines = [f"{plan['layers']} copper layers: {pre.get('title', plan.get('preset', ''))} ({plan.get('preset', '')})"]
    for l, r in zip(names(plan["layers"]), plan["roles"]):
        if r == "plane":
            lines.append(f"  {l}: {short(plan['planes'].get(l, ''))} plane")
        else:
            d = plan["directions"].get(l, "any")
            lines.append(f"  {l}: signal" + ("" if d == "any" else f", routed along {d}"))
    return "\n".join(lines)


def routing_layers(plan, copper=None):
    """The layers the router may put tracks on: the plan's signal layers, else the outer two (inner = planes)."""
    if plan and plan.get("roles"):
        ls = [l for l, r in zip(names(plan["layers"]), plan["roles"]) if r == "signal"]
        if copper:
            ls = [l for l in ls if l in copper]
        if ls:
            return ls
    return ["F.Cu", "B.Cu"]


def directions(plan):
    """layer -> "x" | "y" | "any" for the routing layers (the router's preference)."""
    if plan and plan.get("directions"):
        return dict(plan["directions"])
    return {"F.Cu": "any", "B.Cu": "x"}


def get(cfg):
    """The project's plan (tracewright.json "stackup"), or None."""
    p = (cfg or {}).get("stackup")
    return p if isinstance(p, dict) and p.get("layers") else None


def board_roles(b, plan=None):
    """{layer: {"role": "signal" | "plane", "net"?, "dir"?}} for every copper layer of the board: from the plan once
    it is on the board, else as the router sees it (outer layers signal, inner layers planes: the net of the biggest
    pour on it, if any)."""
    out = {}
    if plan and len(b.copper) == plan.get("layers"):
        for l, r in zip(names(plan["layers"]), plan["roles"]):
            out[l] = {"role": r, "net": plan["planes"].get(l)} if r == "plane" else {"role": r, "dir": plan["directions"].get(l, "any")}
        return out
    biggest = {}
    for z in b.zones:
        if z.is_rule_area or not z.net or not z.outline:
            continue
        a = abs(_area(z.outline[0]))
        for l in z.layers:
            if a > biggest.get(l, (0, ""))[0]:
                biggest[l] = (a, z.net)
    for l in b.copper:
        if l in ("F.Cu", "B.Cu"):
            out[l] = {"role": "signal", "dir": "any"}
        else:
            out[l] = {"role": "plane", "net": biggest.get(l, (0, None))[1]}
    return out


def identify(b):
    """The fab build the board's stack-up is (its preset id), or None."""
    diel = [round(l["thickness"], 3) for l in b.stackup if l["type"] in ("prepreg", "core")]
    if not diel:
        return None
    for k, v in PRESETS.items():
        if v["layers"] == len(b.copper) and [round(x[1], 3) for x in v["build"] if x[0] != "copper"] == diel:
            return k
    return None


# ------------------------------------------------------------------ the board
def stackup_text(plan, indent="\t\t"):
    """The board file's (stackup ...) block for the plan's build."""
    pre = PRESETS[plan["preset"]]
    ls = names(plan["layers"])
    i, k = 0, 0
    t2 = indent + "\t"
    rows = [f'{t2}(layer "F.SilkS" (type "Top Silk Screen"))', f'{t2}(layer "F.Paste" (type "Top Solder Paste"))',
            f'{t2}(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))']
    for item in pre["build"]:
        if item[0] == "copper":
            rows.append(f'{t2}(layer "{ls[i]}" (type "copper") (thickness {item[1]:g}))')
            i += 1
        else:
            k += 1
            kind, th, er, mat = item
            rows.append(f'{t2}(layer "dielectric {k}" (type "{kind}") (thickness {th:g}) (material "{mat}") (epsilon_r {er:g}) (loss_tangent 0.02))')
    rows += [f'{t2}(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))', f'{t2}(layer "B.Paste" (type "Bottom Solder Paste"))',
             f'{t2}(layer "B.SilkS" (type "Bottom Silk Screen"))', f'{t2}(copper_finish "None")', f'{t2}(dielectric_constraints no)']
    return "(stackup\n" + "\n".join(rows) + "\n" + indent + ")"


def thickness(plan):
    """The board's nominal thickness (what the fab quotes; the build adds up to within its tolerance)."""
    return PRESETS[plan["preset"]].get("nominal", 1.6)


def write_build(pcb_path, plan):
    """Put the plan's build into the board file: (setup (stackup ...)) and (general (thickness ...))."""
    with open(pcb_path, encoding="utf-8") as f:
        text = f.read()
    t = parse(text, spans=True)
    edits = []
    setup = find(t, "setup")
    if setup is None:
        raise ValueError("the board file has no setup section")
    su = find(setup, "stackup")
    block = stackup_text(plan)
    if su is not None:
        edits.append((su, block))
    else:                                              # the first thing in setup
        pos = setup.span[0] + len("(setup")
        edits.append(((pos, pos), "\n\t\t" + block))
    gen = find(t, "general")
    th = find(gen, "thickness") if gen is not None else None
    if th is not None:
        edits.append((th, f"(thickness {thickness(plan):g})"))
    from .sexp import splice
    text = splice(text, edits)
    tmp = pcb_path + ".tw-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, pcb_path)


def apply(project, plan, board=None, live="auto"):
    """The plan onto the board: the copper layer count, the plane layers marked as such (KiCad's power type), the
    fab's build, and a pour of each plane's net over the whole board on its layer. Returns {ok, did: [...], error}.
    A layer that would go must have no copper on it."""
    from .board import Board
    from .pcb import client
    b = board or Board.load(project.pcb)
    n = plan["layers"]
    want = names(n)
    gone = [l for l in b.copper if l not in want]
    busy = sorted({t.layer for t in b.tracks if t.layer in gone} | {l for z in b.zones for l in z.layers if l in gone})
    if busy:
        return {"ok": False, "error": f"there is copper on {', '.join(busy)}: take it off before going to {n} layers"}
    did = []
    ops = []
    if len(b.copper) != n:
        ops.append({"op": "layers", "copper": n})
        did.append(f"{len(b.copper)} -> {n} copper layers")
    ops.append({"op": "layer_types", "types": {l: ("power" if r == "plane" else "signal") for l, r in zip(want, plan["roles"])}})
    res = client.apply(project, ops, live=live)
    if not res.get("ok"):
        return {"ok": False, "error": "; ".join(r.get("error", "") for r in res.get("results", []) if not r.get("ok")) or res.get("error")}
    write_build(project.pcb, plan)
    did.append(f"build {plan['preset']}, {thickness(plan):g} mm")
    _kicad_reload(project)
    b = Board.load(project.pcb)
    if b.outline:
        outer = max(b.outline, key=lambda o: abs(_area(o)))
        zops = []
        # the planes an earlier plan poured (named "<net> plane <layer>") on a layer that is now a signal layer, or
        # now another net's plane, come off: left there, a pour fills the signal layer the router is given
        keep = {f"{short(plan['planes'].get(l))} plane {l}" for l, r in zip(want, plan["roles"]) if r == "plane"}
        stale = sorted({z.name for z in b.zones if not z.is_rule_area and re.match(r"^\S+ plane \S+\.Cu$", z.name or "")
                        and z.name not in keep})
        if stale:
            zops.append({"op": "delete", "kinds": ["zone"], "names": stale})
            did.append("taken off: " + ", ".join(stale))
        for l, r in zip(want, plan["roles"]):
            if r != "plane":
                continue
            net = plan["planes"].get(l)
            zops.append({"op": "zone", "net": net, "layers": [l], "polygon": [list(q) for q in outer], "name": f"{short(net)} plane {l}",
                         "priority": 0, "clearance": 0.25, "min_width": 0.2, "connect": "thermal"})
            did.append(f"{l}: {short(net)} plane")
        if zops:
            zops.append({"op": "fill"})
            res = client.apply(project, zops, live=live)
            if not res.get("ok"):
                return {"ok": False, "did": did, "error": "the plane pours: " + "; ".join(r.get("error", "") for r in res.get("results", []) if not r.get("ok"))}
    else:
        did.append("no board outline yet: the plane pours come once there is one (apply again)")
    return {"ok": True, "did": did}


def _kicad_reload(project):
    """KiCad, when it has the board open, shows the file as it is now."""
    try:
        from . import live as livemod
        link = livemod.link_for(project.pcb)
        if link is not None:
            link.revert()
    except Exception:
        pass


def _area(pts):
    a = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        a += x0 * y1 - x1 * y0
    return a / 2
