"""Rules for one area of the board. A region is a KiCad rule area named "TW region <name>" with a custom DRC rule scoped
to it, so KiCad's DRC and the router both follow it:

    keepout   no tracks and no vias (an antenna's clearance, under a crystal, a mounting area)
    novias    tracks may pass, no vias (under a part's belly, a shield's fence line)
    neck      finer tracks and spacing than the net classes (under a dense connector or a fine-pitch part)
    spacing   more clearance than the net classes (high voltage, a sensitive analog area); no pour inside it (KiCad's
              pour filler does not apply a region's spacing, so the pours stay out)

    add(project, name, polygon, kind, layers=None, track=None, clearance=None, by="user") -> ops for the board
    remove(project, name) -> ops; listing(project, board=None) -> [{name, kind, text, box, by}]
    for_router(project, board) -> [(name, box, kind, track, clearance)]

The regions are kept in tracewright.json "regions" (what each is for, in words); the rule areas are on the board.
"""
import re

from . import geom

KINDS = {"keepout": "No tracks or vias", "novias": "No vias", "neck": "Finer tracks", "spacing": "More spacing"}


def _name_ok(name):
    name = re.sub(r"\s+", " ", str(name or "").strip())
    if not name or len(name) > 40 or not re.fullmatch(r"[\w .+-]+", name):
        raise ValueError("a region's name: letters, digits, spaces, . + - (40 at most)")
    return name


def _cfg(project):
    return getattr(project, "cfg", None) or {}


def _save(project):
    for m in ("save", "save_cfg"):
        f = getattr(project, m, None)
        if callable(f):
            return f()


def listing(project, board=None):
    out = []
    boxes = {}
    if board is not None:
        boxes = {z.name: geom.bbox([q for pl in z.outline for q in pl]) for z in board.zones if z.is_rule_area and z.outline}
    for r in _cfg(project).get("regions") or []:
        e = dict(r)
        e["text"] = describe(r)
        b = boxes.get("TW region " + r["name"])
        if b:
            e["box"] = [round(v, 3) for v in b]
        e["on_board"] = b is not None or board is None
        out.append(e)
    return out


def describe(r):
    k = r["kind"]
    ls = "" if not r.get("layers") else " on " + ", ".join(r["layers"])
    if k == "keepout":
        return f"{r['name']}: no tracks or vias{ls}"
    if k == "novias":
        return f"{r['name']}: no vias{ls}"
    if k == "neck":
        return f"{r['name']}: tracks down to {r['track']:g} mm, {r['clearance']:g} mm apart{ls}"
    return f"{r['name']}: {r['clearance']:g} mm between nets, no pour{ls}"


def dru_rule(r):
    """The region's custom DRC rule (None for a keep-out: the rule area itself forbids)."""
    area = f"TW region {r['name']}"
    if r["kind"] == "neck":
        return (f"tw region {r['name']}", f'''# Tracewright region: finer tracks in {r['name']}
(rule "tw region {r['name']}"
  (condition "A.intersectsArea('{area}')")
  (constraint track_width (min {r['track']:g}mm))
  (constraint clearance (min {r['clearance']:g}mm)))''')
    if r["kind"] == "spacing":
        return (f"tw region {r['name']}", f'''# Tracewright region: more spacing in {r['name']}
(rule "tw region {r['name']}"
  (condition "A.intersectsArea('{area}') && A.Net != B.Net")
  (constraint clearance (min {r['clearance']:g}mm)))''')
    return None


def add(project, name, polygon, kind, layers=None, track=None, clearance=None, by="user", copper=None):
    """Save the region and return the board ops that draw its rule area (the DRC rule is written here)."""
    from .pcb import rules as dru
    name = _name_ok(name)
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    pts = [[float(x), float(y)] for x, y in polygon or []]
    if len(pts) < 3 or abs(geom.area(pts)) < 0.25:
        raise ValueError("a region needs an area (three corners or more, 0.25 mm² at least)")
    layers = [str(l) for l in layers] if layers else None
    if layers and copper and any(l not in copper for l in layers):
        raise ValueError(f"not a copper layer of this board: {', '.join(l for l in layers if l not in copper)}")
    r = {"name": name, "kind": kind, "layers": layers, "by": by}
    if kind == "neck":
        track, clearance = float(track or 0.1), float(clearance or 0.1)
        if not (0.05 <= track <= 1 and 0.05 <= clearance <= 1):
            raise ValueError("finer tracks: width and clearance between 0.05 and 1 mm")
        r.update(track=track, clearance=clearance)
    elif kind == "spacing":
        clearance = float(clearance or 0.5)
        if not 0.1 <= clearance <= 10:
            raise ValueError("more spacing: a clearance between 0.1 and 10 mm")
        r["clearance"] = clearance
    cfg = project.cfg
    regs = [x for x in cfg.get("regions") or [] if x.get("name") != name]
    regs.append(r)
    cfg["regions"] = regs
    _save(project)
    rule = dru_rule(r)
    if rule:
        dru.ensure_rules(project, dict([rule]), replace=True)
    return [{"op": "rule_area", "name": f"TW region {name}", "layers": layers or ["*.Cu"], "polygon": pts,
             "no_tracks": kind == "keepout", "no_vias": kind in ("keepout", "novias"), "no_pour": kind == "spacing",
             "no_footprints": False}]


def remove(project, name):
    """Forget the region; returns the ops that take its rule area off the board."""
    from .pcb import rules as dru
    cfg = project.cfg
    regs = cfg.get("regions") or []
    if not any(x.get("name") == name for x in regs):
        raise KeyError(name)
    cfg["regions"] = [x for x in regs if x.get("name") != name]
    _save(project)
    path = dru.dru_path(project)
    try:
        txt = open(path, encoding="utf-8").read()
        new = dru._strip_rule(txt, f"tw region {name}")
        if new != txt:
            open(path, "w", encoding="utf-8").write(new)
    except OSError:
        pass
    return [{"op": "delete", "kinds": ["zone"], "names": [f"TW region {name}"]}]


def for_router(project, board):
    """[(name, box, kind, track, clearance)] for the regions the router narrows or widens in."""
    out = []
    for r in listing(project, board):
        if r["kind"] in ("neck", "spacing") and r.get("box"):
            out.append((r["name"], tuple(r["box"]), r["kind"], r.get("track"), r.get("clearance")))
    return out
