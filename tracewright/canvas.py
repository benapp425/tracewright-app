"""The guided start's live canvas: what Claude has worked out during the intake, drawn beside the chat
as it forms -- the key requirements, a block diagram, the connectors with their pinouts, the parts (a
live BOM with JLC / LCSC stock and price), and, once the intake is done, the plan behind the Start
button. Kept in .tracewright/canvas.json; the `canvas` and `ready_to_start` agent tools write it.

A project's start (tracewright.json "start"): {"mode": "guided", "phase": "intake" | "ready" | "done"}.
Classic projects have no start block."""
import os, json, time, threading

SECTIONS = ("requirements", "diagram", "connectors", "floorplan", "parts", "plan", "proposal")
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
    if section == "floorplan":
        return clean_floorplan(data)
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


# ----------------------------------------------------------------------------- the floorplan
# The board as it is meant to come out, to scale, before the schematic exists: its size and corners, the
# mounting holes, each connector on its edge, the main blocks where they go, and keep-outs. Millimetres from
# the board's top-left corner, y down; a block's x, y is its centre; a connector's `at` is how far along its
# edge its centre is (from the top for left and right, from the left for top and bottom). The user can drag
# anything; what they moved keeps its place ("moved") when Claude sends the floorplan again.
def _num(v, lo=None, hi=None, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:                                        # NaN
        return default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return round(x, 2)


def clean_floorplan(data):
    b = data.get("board") if isinstance(data.get("board"), dict) else {}
    W = _num(b.get("w"), 5, 500, 50.0)
    H = _num(b.get("h"), 5, 500, 40.0)
    board = {"w": W, "h": H, "radius": _num(b.get("radius"), 0, min(W, H) / 2, 0.0)}
    holes = []
    for i, o in enumerate(data.get("holes") or []):
        if not isinstance(o, dict):
            continue
        d = _num(o.get("d"), 1, 10, 3.2)
        holes.append({"id": _s(o.get("id") or o.get("ref") or f"H{i + 1}", 16), "ref": _s(o.get("ref"), 12),
                      "x": _num(o.get("x"), d / 2, W - d / 2, d), "y": _num(o.get("y"), d / 2, H - d / 2, d), "d": d,
                      "moved": bool(o.get("moved")), "locked": bool(o.get("locked"))})
    items, ids = [], set()
    for o in data.get("items") or []:
        if not isinstance(o, dict):
            continue
        iid = _s(o.get("id") or o.get("ref") or o.get("label"), 32)
        if not iid or iid in ids:
            continue
        ids.add(iid)
        w, h = _num(o.get("w"), 1, W * 2, 8.0), _num(o.get("h"), 1, H * 2, 6.0)
        it = {"id": iid, "label": _s(o.get("label") or iid, 40), "ref": _s(o.get("ref"), 12),
              "kind": o.get("kind") if o.get("kind") in KINDS else ("connector" if o.get("edge") in EDGES[:4] else "other"),
              "w": w, "h": h, "note": _s(o.get("note"), 80), "moved": bool(o.get("moved")), "locked": bool(o.get("locked"))}
        if o.get("edge") in EDGES[:4]:
            it["edge"] = o["edge"]
            span = H if o["edge"] in ("left", "right") else W
            it["at"] = _num(o.get("at"), min(w / 2, span / 2), max(span - w / 2, span / 2), span / 2)
        else:
            it["x"] = _num(o.get("x"), 0, W, W / 2)
            it["y"] = _num(o.get("y"), 0, H, H / 2)
            it["rot"] = _rot(o.get("rot"))
        items.append(it)
    keepouts = []
    for i, o in enumerate(data.get("keepouts") or []):
        if isinstance(o, dict):
            keepouts.append({"id": _s(o.get("id") or f"K{i + 1}", 16), "label": _s(o.get("label"), 40),
                             "x": _num(o.get("x"), 0, W, W / 2), "y": _num(o.get("y"), 0, H, H / 2),
                             "w": _num(o.get("w"), 0.5, W, 5.0), "h": _num(o.get("h"), 0.5, H, 5.0), "rot": _rot(o.get("rot")),
                             "moved": bool(o.get("moved")), "locked": bool(o.get("locked"))})
    return {"board": board, "holes": holes[:12], "items": items[:24], "keepouts": keepouts[:8], "note": _s(data.get("note"), 200)}


def _rot(v):
    """A quarter turn: 0, 90, 180 or 270."""
    try:
        return int(round(float(v) / 90.0)) % 4 * 90
    except (TypeError, ValueError):
        return 0


def _keep_moves(old, new):
    """What the user moved, turned or locked keeps the user's place when Claude sends the floorplan again."""
    if not old:
        return new
    every = lambda fp: (fp.get("items") or []) + (fp.get("holes") or []) + (fp.get("keepouts") or [])
    before = {o["id"]: o for o in every(old) if o.get("moved") or o.get("locked")}
    for o in every(new):
        was = before.get(o.get("id"))
        if not was:
            continue
        for k in ("x", "y", "edge", "at", "rot"):
            o.pop(k, None)
            if k in was:
                o[k] = was[k]
        o["moved"] = bool(was.get("moved"))
        o["locked"] = bool(was.get("locked"))
    if (old.get("board") or {}).get("moved"):
        new["board"] = {**new["board"], "w": old["board"]["w"], "h": old["board"]["h"], "moved": True}
    return new


def move(root, what):
    """The user dragged something on the floorplan: {id, x, y} (a block or hole), {id, edge, at} (a connector) or
    {board: {w, h}} (the outline's corner). Returns (canvas, a line for Claude's next turn)."""
    with _lock:
        d = load(root)
        fp = d.get("floorplan")
        if not fp:
            raise ValueError("there is no floorplan yet")
        if isinstance(what.get("board"), dict):
            b = fp["board"]
            b["w"] = _num(what["board"].get("w"), 5, 500, b["w"])
            b["h"] = _num(what["board"].get("h"), 5, 500, b["h"])
            b["moved"] = True
            for m in what.get("holes") or []:                 # holes that kept to their corners
                o = next((x for x in fp.get("holes") or [] if x["id"] == str(m.get("id"))), None)
                if o:
                    o["x"] = _num(m.get("x"), o["d"] / 2, b["w"] - o["d"] / 2, o["x"])
                    o["y"] = _num(m.get("y"), o["d"] / 2, b["h"] - o["d"] / 2, o["y"])
            line = f"floorplan: the user made the board {b['w']:g} x {b['h']:g} mm (the holes kept to their corners)"
        else:
            iid = str(what.get("id") or "")
            for i, k in enumerate(fp.get("keepouts") or []):       # keep-outs saved before they had ids
                k.setdefault("id", f"K{i + 1}")
            o = next((x for x in (fp.get("items") or []) + (fp.get("holes") or []) + (fp.get("keepouts") or [])
                      if x.get("id") == iid), None)
            if o is None:
                raise ValueError(f"no {iid} on the floorplan")
            W, H = fp["board"]["w"], fp["board"]["h"]
            name = o.get("ref") or o.get("label") or o["id"]
            if "locked" in what and len(what) == 2:                  # {id, locked}: a lock, nothing moves
                o["locked"] = bool(what["locked"])
                d["floorplan"] = fp
                d["updated"] = time.time()
                _save(root, {k: v for k, v in d.items() if v is not None})
                return d, f"floorplan: the user {'locked' if o['locked'] else 'unlocked'} {name}" + \
                    (" (keep it where it is)" if o["locked"] else "")
            if "rot" in what and "edge" not in o:
                o["rot"] = _rot(what["rot"])
            if what.get("edge") in EDGES[:4]:
                o["edge"] = what["edge"]
                span = H if o["edge"] in ("left", "right") else W
                o["at"] = _num(what.get("at"), 0, span, span / 2)
                o.pop("x", None), o.pop("y", None)
                line = f"floorplan: the user put {o.get('ref') or o['label']} on the {o['edge']} edge, {o['at']:g} mm along it"
            else:
                o["x"] = _num(what.get("x"), 0, W, o.get("x", W / 2))
                o["y"] = _num(what.get("y"), 0, H, o.get("y", H / 2))
                if "edge" in o and "d" not in o:
                    o.pop("edge", None), o.pop("at", None)
                turned = f", turned {o['rot']}°" if o.get("rot") else ""
                line = f"floorplan: the user moved {name} to x {o['x']:g}, y {o['y']:g} mm{turned}"
            o["moved"] = True
        d["floorplan"] = fp
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d, line


# ----------------------------------------------------------------------------- a suggested layout
# The user asks Claude to suggest a floorplan layout, with notes ("USB-C on the left", "the antenna away from the
# motor driver"); Claude answers with moves (each with why) and a reply to each note -- followed or declined, with
# why -- drawn as ghosts over the floorplan for the user to accept all, some, or none. Locked items never move.
#   proposal: {"asked": t, "notes": [text], "moves": [{id, x, y | edge, at, rot?, why}], "replies": [{note, text,
#              outcome: followed | declined}], "summary": text, "ready": t}

def ask_layout(root, notes):
    """The user asked for a suggested layout: the request, with their notes, waits for Claude's answer."""
    notes = [_s(n, 300) for n in (notes or []) if _s(n)][:8]
    with _lock:
        d = load(root)
        if not (d.get("floorplan") or {}).get("board"):
            raise ValueError("there is no floorplan yet")
        d["proposal"] = {"asked": time.time(), "notes": notes, "moves": [], "replies": []}
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d


def propose(root, data):
    """Claude's suggestion: {moves: [{id, x, y | edge, at, rot?, why}], replies: [{note: 1.., text, outcome}], summary}.
    A locked item, or one not on the floorplan, is refused (ValueError)."""
    with _lock:
        d = load(root)
        fp = d.get("floorplan") or {}
        if not fp.get("board"):
            raise ValueError("there is no floorplan yet")
        W, H = fp["board"]["w"], fp["board"]["h"]
        for i, k in enumerate(fp.get("keepouts") or []):
            k.setdefault("id", f"K{i + 1}")
        every = {o["id"]: o for o in (fp.get("items") or []) + (fp.get("holes") or []) + (fp.get("keepouts") or []) if o.get("id")}
        prev = d.get("proposal") or {}
        moves, bad, locked = [], [], []
        for m in (data or {}).get("moves") or []:
            iid = str(m.get("id") or "")
            o = every.get(iid)
            if o is None:
                bad.append(iid or "?")
                continue
            if o.get("locked"):
                locked.append(o.get("ref") or o.get("label") or iid)
                continue
            mv = {"id": iid, "why": _s(m.get("why"), 200)}
            if o.get("edge") and m.get("edge") in EDGES[:4]:
                span = H if m["edge"] in ("left", "right") else W
                mv.update(edge=m["edge"], at=_num(m.get("at"), 0, span, span / 2))
            else:
                mv.update(x=_num(m.get("x"), 0, W, o.get("x", W / 2)), y=_num(m.get("y"), 0, H, o.get("y", H / 2)))
            if m.get("rot") is not None and not o.get("edge"):
                mv["rot"] = _rot(m["rot"])
            moves.append(mv)
        if bad or locked:
            raise ValueError("; ".join(([f"not on the floorplan: {', '.join(bad)}"] if bad else []) +
                                       ([f"locked by the user, leave them: {', '.join(locked)}"] if locked else [])))
        notes = prev.get("notes") or []
        replies = []
        for r in (data or {}).get("replies") or []:
            try:
                n = int(r.get("note"))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(notes):
                replies.append({"note": n, "text": _s(r.get("text"), 300),
                                "outcome": "declined" if str(r.get("outcome")).lower() in ("declined", "no", "not followed") else "followed"})
        d["proposal"] = {**prev, "moves": moves, "replies": replies, "summary": _s((data or {}).get("summary"), 300), "ready": time.time()}
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d


def accept(root, ids=None):
    """The user takes the suggestion, or some of it (ids): each accepted move is made as theirs. Returns (canvas,
    line for Claude)."""
    cur = load(root).get("proposal") or {}
    take = [m for m in cur.get("moves") or [] if ids is None or m["id"] in ids]
    if not take:
        raise ValueError("nothing to accept")
    names = []
    for m in take:
        what = {"id": m["id"], **({"edge": m["edge"], "at": m["at"]} if m.get("edge") else {"x": m["x"], "y": m["y"]})}
        if m.get("rot") is not None:
            what["rot"] = m["rot"]
        d, _ = move(root, what)
        o = next((x for x in (d["floorplan"].get("items") or []) + (d["floorplan"].get("holes") or []) + (d["floorplan"].get("keepouts") or [])
                  if x.get("id") == m["id"]), {})
        names.append(o.get("ref") or o.get("label") or m["id"])
    with _lock:
        d = load(root)
        left = [m for m in (d.get("proposal") or {}).get("moves") or [] if m["id"] not in {t["id"] for t in take}]
        if left:
            d["proposal"]["moves"] = left
        else:
            d.pop("proposal", None)
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
    return d, f"floorplan: the user accepted your suggestion for {', '.join(names)}" + (f" (not for {len(left)} more)" if left else "")


def dismiss(root):
    with _lock:
        d = load(root)
        d.pop("proposal", None)
        d["updated"] = time.time()
        _save(root, {k: v for k, v in d.items() if v is not None})
        return d


def update(root, section, data):
    """Replace one section; returns the whole canvas."""
    if section not in SECTIONS:
        raise ValueError(f"unknown canvas section {section}; sections: {', '.join(SECTIONS)}")
    with _lock:
        d = load(root)
        new = clean(section, data)
        if section == "proposal":
            raise ValueError("a suggested layout goes through the propose action")
        d[section] = _keep_moves(d.get("floorplan"), new) if section == "floorplan" else new
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
