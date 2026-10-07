"""Design notes: the reasoning behind the schematic, kept off the sheet. Each note is tied to exactly what it explains --
a part, one of its pins, or a net -- so the app can show it right there (hover the marker beside the part) and Claude
can read it with the part. The sheet keeps a short line in plain words; this keeps the why, the numbers and the source.

    hardware/<board>/design-notes.json
    {"notes": [{"id": "n3", "anchor": {"ref": "R202"} | {"ref": "U201", "pin": "8"} | {"net": "EN"},
                "short": "Boot straps: IO2, IO8 high", "why": "IO2 must be high at reset (ESP32-C3 datasheet ch. 4) ...",
                "sheet": "mcu.kicad_sch", "by": "script" | "claude" | "user", "at": "2026-10-07T21:30:00"}]}

    load(project) / add(project, anchor, why, short="", sheet="", by="claude") / remove(project, nid)
    for_ref(project, ref) / for_net(project, net) / replace_script(hw_dir, notes)   # the schematic script's own notes

Notes by the script (tw.sch.auto: g.note(..., why=...), g.why(...)) are rewritten each time the script runs; the ones
Claude or the user add (the notes tool, the app) stay."""
import json, os, re, time

FILE = "design-notes.json"


def path(project_or_dir):
    hw = getattr(project_or_dir, "hw", None) or project_or_dir
    return os.path.join(hw, FILE)


def load(project_or_dir):
    try:
        with open(path(project_or_dir), encoding="utf-8") as f:
            d = json.load(f)
        return [n for n in d.get("notes") or [] if isinstance(n, dict) and n.get("anchor")]
    except (OSError, ValueError):
        return []


def _save(project_or_dir, notes):
    p = path(project_or_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"notes": notes}, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def anchor_of(near, pin=None):
    """{"ref"} / {"ref", "pin"} / {"net"} from what the caller has: a part (with .ref), a reference, "net:NAME"."""
    if isinstance(near, dict):
        return {k: str(v) for k, v in near.items() if k in ("ref", "pin", "net") and v}
    if hasattr(near, "ref"):
        near = near.ref
    near = str(near or "")
    if near.startswith("net:"):
        return {"net": near[4:]}
    out = {"ref": near} if near else {}
    if pin is not None and out:
        out["pin"] = str(pin)
    return out


def _next_id(notes):
    n = max([int(m.group(1)) for x in notes if (m := re.match(r"n(\d+)$", str(x.get("id", ""))))] or [0])
    return f"n{n + 1}"


def add(project_or_dir, anchor, why, short="", sheet="", by="claude"):
    notes = load(project_or_dir)
    a = anchor_of(anchor)
    if not a:
        raise ValueError("a note needs what it explains: a part (ref), a part's pin, or a net")
    n = {"id": _next_id(notes), "anchor": a, "short": str(short or "").strip()[:120], "why": str(why or "").strip()[:2000],
         "sheet": sheet, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    notes.append(n)
    _save(project_or_dir, notes)
    return n


def remove(project_or_dir, nid):
    notes = load(project_or_dir)
    keep = [n for n in notes if n.get("id") != nid]
    if len(keep) == len(notes):
        raise KeyError(nid)
    _save(project_or_dir, keep)


def replace_script(hw_dir, script_notes):
    """The schematic script's notes, written fresh each run; notes from anyone else are kept."""
    keep = [n for n in load(hw_dir) if n.get("by") != "script"]
    out = list(keep)
    for n in script_notes:
        out.append({"id": _next_id(out), "anchor": n["anchor"], "short": n.get("short", ""), "why": n.get("why", ""),
                    "sheet": n.get("sheet", ""), "by": "script", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(hw_dir, out)
    return out


def for_ref(project_or_dir, ref):
    return [n for n in load(project_or_dir) if n["anchor"].get("ref") == ref]


def for_net(project_or_dir, net):
    short = str(net).rsplit("/", 1)[-1]
    return [n for n in load(project_or_dir) if n["anchor"].get("net") in (net, short)]
