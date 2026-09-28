"""The guided start's live canvas: what Claude has worked out during the intake, drawn beside the chat
as it forms -- the key requirements, a block diagram, the connectors with their pinouts, the parts (a
live BOM with JLC / LCSC stock and price), and, once the intake is done, the plan behind the Start
button. Kept in .tracewright/canvas.json; the `canvas` and `ready_to_start` agent tools write it.

A project's start (tracewright.json "start"): {"mode": "guided", "phase": "intake" | "ready" | "done"}.
Classic projects have no start block."""
import os, json, time, threading

SECTIONS = ("requirements", "diagram", "connectors", "parts", "plan")
KINDS = ("power", "mcu", "sensor", "connector", "io", "rf", "memory", "display", "motor", "audio", "other")
EDGES = ("left", "right", "top", "bottom", "")
_lock = threading.Lock()


def path(root):
    return os.path.join(root, ".tracewright", "canvas.json")


def load(root):
    try:
        with open(path(root)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {k: d.get(k) for k in SECTIONS} | {"updated": d.get("updated"), "pending": d.get("pending") or []}


def set_pending(root, codes, on):
    """Mark part codes as being looked up (the live BOM shows a spinner for them), or done."""
    with _lock:
        d = load(root)
        cur = set(d.get("pending") or [])
        cur = (cur | set(codes)) if on else (cur - set(codes))
        d["pending"] = sorted(cur)
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d


def _save(root, d):
    os.makedirs(os.path.dirname(path(root)), exist_ok=True)
    tmp = path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path(root))


def _s(x, n=120):
    # Models sometimes over-escape a quote (0.96\" for 0.96"); show the text as they meant it.
    return str(x or "").replace('\\"', '"').strip()[:n]


def clean(section, data):
    """The section's data, validated and trimmed (Claude writes it; the page draws it)."""
    data = data or {}
    if section == "requirements":
        items = [{"label": _s(i.get("label"), 40), "value": _s(i.get("value"), 160)} for i in data.get("items") or []
                 if isinstance(i, dict) and _s(i.get("label"))]
        return {"items": items[:16]}
    if section == "diagram":
        blocks, ids = [], set()
        for b in data.get("blocks") or []:
            if not isinstance(b, dict) or not _s(b.get("id"), 32):
                continue
            bid = _s(b.get("id"), 32)
            ids.add(bid)
            blocks.append({"id": bid, "label": _s(b.get("label") or bid, 40), "kind": b.get("kind") if b.get("kind") in KINDS else "other",
                           "note": _s(b.get("note"), 80)})
        links = [{"from": _s(l.get("from"), 32), "to": _s(l.get("to"), 32), "label": _s(l.get("label"), 32),
                  "kind": l.get("kind") if l.get("kind") in ("power", "signal", "bus") else "signal"}
                 for l in data.get("links") or [] if isinstance(l, dict) and _s(l.get("from")) in ids and _s(l.get("to")) in ids]
        return {"blocks": blocks[:18], "links": links[:40]}
    if section == "connectors":
        items = []
        for c in data.get("items") or []:
            if not isinstance(c, dict) or not _s(c.get("name")):
                continue
            pins = [{"n": _s(p.get("n"), 8), "signal": _s(p.get("signal"), 32)} for p in c.get("pins") or [] if isinstance(p, dict)]
            items.append({"name": _s(c.get("name"), 40), "type": _s(c.get("type"), 60), "ref": _s(c.get("ref"), 12),
                          "edge": c.get("edge") if c.get("edge") in EDGES else "", "pins": pins[:40]})
        size = data.get("board") or {}
        board = {"w": float(size.get("w") or 0) or None, "h": float(size.get("h") or 0) or None} if isinstance(size, dict) else {}
        return {"items": items[:12], "board": board}
    if section == "parts":
        items = []
        for p in data.get("items") or []:
            if not isinstance(p, dict) or not (_s(p.get("mpn")) or _s(p.get("role"))):
                continue
            code = _s(p.get("lcsc"), 16).upper()
            items.append({"role": _s(p.get("role"), 48), "mpn": _s(p.get("mpn"), 60), "lcsc": code if code.startswith("C") else "",
                          "package": _s(p.get("package"), 32), "qty": int(p.get("qty") or 1), "why": _s(p.get("why"), 120)})
        return {"items": items[:40]}
    if section == "plan":
        steps = [_s(s, 120) for s in data.get("steps") or [] if _s(s)]
        return {"summary": _s(data.get("summary"), 400), "steps": steps[:10]}
    raise ValueError(f"unknown canvas section {section}; sections: {', '.join(SECTIONS)}")


def update(root, section, data):
    """Replace one section; returns the whole canvas."""
    if section not in SECTIONS:
        raise ValueError(f"unknown canvas section {section}; sections: {', '.join(SECTIONS)}")
    with _lock:
        d = load(root)
        d[section] = clean(section, data)
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d


def enrich(root, cv):
    """The canvas with each part's stock, price and JLC library from the sourcing cache."""
    parts = (cv.get("parts") or {}).get("items") or []
    if not parts:
        return cv
    from .bom import sourcing_index
    idx = sourcing_index(root)
    pending = set(cv.get("pending") or [])
    out = []
    for p in parts:
        e = idx.get(p.get("lcsc") or "") or {}
        out.append({**p, "jlc_stock": e.get("jlc_stock"), "lcsc_stock": e.get("lcsc_stock"), "lib": e.get("lib"),
                    "price": e.get("jlc_price") if e.get("jlc_price") is not None else e.get("lcsc_price"),
                    "name": e.get("name") or "", "found": bool(e), "pending": p.get("lcsc") in pending and not e})
    return {**cv, "parts": {"items": out}}


def missing_codes(root, cv):
    from .bom import sourcing_index
    idx = sourcing_index(root)
    return [p["lcsc"] for p in ((cv.get("parts") or {}).get("items") or []) if p.get("lcsc") and p["lcsc"] not in idx]


# ----------------------------------------------------------------------------- the start
def start_of(cfg):
    st = cfg.get("start") or {}
    return st if st.get("mode") == "guided" else None


def phase(cfg):
    """'intake' | 'ready' for a guided project that has not started; None otherwise."""
    st = start_of(cfg)
    return st.get("phase") if st and st.get("phase") in ("intake", "ready") else None
