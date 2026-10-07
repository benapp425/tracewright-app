"""My blocks: a circuit built once and wanted again -- a USB-C input, a buck converter, a crystal and its capacitors.
A block keeps its parts (values, footprints, part numbers), how they connect inside, the nets it shares with the rest
of the design (its ports), its design notes, and where its parts sat relative to each other on the board. It lives with
the app (data dir/blocks), not in a project; Claude draws it into another design's schematic and places it as it was.

    save(project, refs, name, description="") -> the block    (from a project's parts, usually one sheet's)
    items() / get(bid) / remove(bid)
    text(block) -> the block as Claude reads it
"""
import json, os, re, threading, time

from . import config

_lock = threading.Lock()
FIELDS = ("MPN", "LCSC", "Manufacturer")


def _dir():
    return os.path.join(config.data_dir(), "blocks")


def items():
    out = []
    d = _dir()
    for bid in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        try:
            with open(os.path.join(d, bid, "block.json"), encoding="utf-8") as f:
                b = json.load(f)
            out.append({k: b.get(k) for k in ("id", "name", "description", "from", "saved")} |
                       {"parts": len(b.get("parts") or []), "ports": [p["name"] for p in b.get("ports") or []]})
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: x.get("saved") or "", reverse=True)


def get(bid):
    if not re.fullmatch(r"[a-z0-9-]{1,60}", str(bid or "")):
        raise KeyError(bid)
    try:
        with open(os.path.join(_dir(), bid, "block.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise KeyError(bid)


def remove(bid):
    import shutil
    get(bid)
    shutil.rmtree(os.path.join(_dir(), bid), ignore_errors=True)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:40] or "block"


def save(project, refs, name, description=""):
    """A block from parts of the project (its schematic, and the board if there is one)."""
    from tw.checks.context import Context
    from tw.sch import notes as dnotes
    tw = project.tw
    if not tw.has_sch():
        raise ValueError("no schematic to take a block from")
    name = re.sub(r"\s+", " ", str(name or "").strip())
    if not name or len(name) > 60:
        raise ValueError("a block needs a name (60 characters at most)")
    ctx = Context(tw, offline=True)
    nl = ctx.netlist
    refs = [r for r in dict.fromkeys(str(r).strip() for r in refs or []) if r]
    missing = [r for r in refs if r not in nl.parts]
    if missing or not refs:
        raise ValueError(f"not in the schematic: {', '.join(missing) or 'no parts given'}")
    inside = set(refs)
    grounds, power = set(ctx.ground_nets()), set(ctx.power_nets())
    parts, used = [], {}
    for r in refs:
        p = nl.parts[r]
        f = p.get("fields") or {}
        pins = {}
        for pin in nl.pins_of(r):
            net = nl.net_of(r, pin)
            if net and not net.startswith("unconnected-"):
                pins[str(pin)] = net.rsplit("/", 1)[-1]
                used.setdefault(net, []).append(f"{r}.{pin}")
        parts.append({"ref": r, "value": p.get("value"), "footprint": p.get("footprint"), "symbol": p.get("lib_id") or p.get("symbol"),
                      "fields": {k: f.get(k) for k in FIELDS if f.get(k)}, "pins": pins,
                      "pin_names": {str(pin): nl.pin_name(r, pin) for pin in nl.pins_of(r) if nl.pin_name(r, pin)}})
    nets, ports = [], []
    for net, pins in sorted(used.items()):
        short = net.rsplit("/", 1)[-1]
        outside = [f"{a}.{b}" for a, b in nl.nets.get(net, []) if a not in inside]
        kind = "ground" if short in grounds else "power" if short in power else "signal"
        if outside or kind in ("ground", "power"):
            ports.append({"name": short, "kind": kind, "pins": pins})
        else:
            nets.append({"name": short, "pins": pins})
    notes = []
    for r in refs:
        for n in dnotes.for_ref(tw, r):
            notes.append({"ref": r, "pin": n["anchor"].get("pin"), "short": n.get("short"), "why": n.get("why")})
    placement = []
    if tw.has_pcb():
        b = ctx.board
        fps = [b.footprints[r] for r in refs if r in b.footprints]
        if fps:
            cx, cy = sum(f.x for f in fps) / len(fps), sum(f.y for f in fps) / len(fps)
            placement = [{"ref": f.ref, "dx": round(f.x - cx, 3), "dy": round(f.y - cy, 3), "rot": round(f.angle, 1), "side": f.side}
                         for f in fps]
    bid = f"{_slug(name)}-{time.strftime('%y%m%d%H%M%S')}"
    block = {"id": bid, "name": name, "description": str(description or "")[:400], "parts": parts, "nets": nets, "ports": ports,
             "notes": notes, "placement": placement, "from": {"project": project.name, "refs": refs},
             "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with _lock:
        d = os.path.join(_dir(), bid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "block.json"), "w", encoding="utf-8") as f:
            json.dump(block, f, indent=1, ensure_ascii=False)
    return block


def text(b):
    """The block as Claude reads it to draw it into a design."""
    out = [f"BLOCK {b['name']} ({b['id']}): {b.get('description') or ''}".rstrip(": "),
           f"from {b['from']['project']} ({', '.join(b['from']['refs'])})", "PARTS"]
    for p in b["parts"]:
        f = ", ".join(f"{k} {v}" for k, v in p["fields"].items())
        out.append(f"  {p['ref']}: {p['value']}, {p['footprint']}" + (f", {f}" if f else ""))
    out.append("CONNECTIONS INSIDE")
    out += [f"  {n['name']}: {', '.join(n['pins'])}" for n in b["nets"]] or ["  (none)"]
    out.append("PORTS (nets shared with the rest of the design: name them after the new design's nets)")
    out += [f"  {p['name']} ({p['kind']}): {', '.join(p['pins'])}" for p in b["ports"]]
    if b.get("notes"):
        out.append("DESIGN NOTES")
        out += [f"  {n['ref']}{'.' + n['pin'] if n.get('pin') else ''}: {n.get('short') or ''} -- {n.get('why') or ''}" for n in b["notes"]]
    if b.get("placement"):
        out.append("PLACEMENT (mm from the block's centre, rotation, side)")
        out += [f"  {p['ref']}: ({p['dx']:+.2f}, {p['dy']:+.2f}) {p['rot']:g}° {p['side']}" for p in b["placement"]]
    return "\n".join(out)
