"""The placement plan: why each part sits where it does, the order it was placed in, and the constraints it keeps.
Claude writes a reason with every part it places (one plain line); the user adds constraints in the app; the app shows
the reason when a part is picked ("why here") and the score of the placement as it stands.

    hardware/<board>/placement-plan.json
    {"stages": {"fixed": iso | null, "main": ..., "support": ..., "rest": ...},
     "parts": {"C12": {"why": "2 mm from U3 pin 7, GND via beside it", "stage": "support", "by": "claude", "at": iso}},
     "constraints": [{"id": "k1", "kind": "near", "ref": "C12", "to": "U3.7", "max_mm": 3, "by": "user", "why": ""},
                     {"kind": "together", "refs": [..], "max_mm": 12}, {"kind": "away", "ref": "Y1", "from": ["U5"], "min_mm": 8},
                     {"kind": "edge", "ref": "J1", "edge": "left" | "any", "max_mm": 1}, {"kind": "side", "ref": "J7", "side": "B"}]}

    load(project) / set_why(project, refs_whys, stage, by) / mark_stage / add_constraint / remove_constraint
    evaluate(board, plan) -> [{constraint, ok, text}]       score(board, netlist, plan) -> {total, parts}
"""
import json, math, os, re, time

FILE = "placement-plan.json"
STAGES = ("fixed", "main", "support", "rest")
STAGE_TEXT = {"fixed": "connectors, holes and what the floorplan fixed", "main": "the main chips",
              "support": "their support parts (decoupling, crystals, pull-ups)", "rest": "everything else"}
KINDS = ("near", "together", "away", "edge", "side")
CONN = re.compile(r"^(J|P|CN|X|XS|USB|CONN)\d")


def path(project):
    return os.path.join(getattr(project, "hw", project), FILE)


def load(project):
    try:
        with open(path(project), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("stages", {})
    d.setdefault("parts", {})
    d.setdefault("constraints", [])
    return d


def save(project, d):
    p = path(project)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def set_why(project, whys, stage=None, by="claude"):
    """whys: {ref: one plain line}. The reason a part is where it is (and the stage it was placed in)."""
    d = load(project)
    for ref, why in whys.items():
        if why:
            d["parts"][ref] = {"why": str(why).strip()[:160], "stage": stage if stage in STAGES else (d["parts"].get(ref) or {}).get("stage"),
                               "by": by, "at": now()}
    save(project, d)
    return d


def mark_stage(project, stage):
    d = load(project)
    if stage in STAGES:
        d["stages"][stage] = now()
        save(project, d)
    return d


def add_constraint(project, c, by="user"):
    if c.get("kind") not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    d = load(project)
    n = max([int(m.group(1)) for x in d["constraints"] if (m := re.match(r"k(\d+)$", str(x.get("id", ""))))] or [0])
    keep = {k: v for k, v in c.items() if k in ("kind", "ref", "refs", "to", "from", "max_mm", "min_mm", "edge", "side", "why")}
    for k in ("max_mm", "min_mm"):
        if k in keep:
            keep[k] = float(keep[k])
    keep.update(id=f"k{n + 1}", by=by, at=now())
    d["constraints"].append(keep)
    save(project, d)
    return keep


def remove_constraint(project, cid):
    d = load(project)
    keep = [c for c in d["constraints"] if c.get("id") != cid]
    if len(keep) == len(d["constraints"]):
        raise KeyError(cid)
    d["constraints"] = keep
    save(project, d)


# ----------------------------------------------------------------------------- geometry on the board
def _box(fp):
    return fp.bbox()


def _gap(a, b):
    """Edge-to-edge distance between two boxes (0 when they touch or overlap)."""
    return math.hypot(max(b[0] - a[2], a[0] - b[2], 0), max(b[1] - a[3], a[1] - b[3], 0))


def _target(board, spec):
    """'U3' -> its box; 'U3.7' -> pad 7's position (a tiny box)."""
    ref, _, pin = str(spec).partition(".")
    fp = board.footprints.get(ref)
    if fp is None:
        return None
    if pin:
        pd = next((p for p in fp.pads if str(p.num) == pin), None)
        if pd is not None:
            return (pd.x, pd.y, pd.x, pd.y)
    return _box(fp)


def describe(c):
    k = c.get("kind")
    if k == "near":
        return f"{c.get('ref')} within {c.get('max_mm', 3):g} mm of {c.get('to')}"
    if k == "together":
        return f"{', '.join(c.get('refs') or [])} within {c.get('max_mm', 15):g} mm of each other"
    if k == "away":
        return f"{c.get('ref') or ', '.join(c.get('refs') or [])} at least {c.get('min_mm', 5):g} mm from {', '.join(c.get('from') or [])}"
    if k == "edge":
        return f"{c.get('ref')} at the {'board' if c.get('edge', 'any') == 'any' else c['edge']} edge"
    if k == "side":
        return f"{c.get('ref')} on the {'bottom' if c.get('side') == 'B' else 'top'}"
    return str(c)


def evaluate(board, plan):
    """Each constraint, kept or not, with what it measures."""
    out = []
    outline = board.outline_bbox() if hasattr(board, "outline_bbox") else None
    if outline is None and getattr(board, "outline", None):
        xs = [p[0] for l in board.outline for p in l]
        ys = [p[1] for l in board.outline for p in l]
        outline = (min(xs), min(ys), max(xs), max(ys))
    for c in plan.get("constraints") or []:
        k, ok, val = c.get("kind"), None, ""
        try:
            if k == "near":
                fp, t = board.footprints.get(c["ref"]), _target(board, c["to"])
                if fp is not None and t is not None:
                    dist = _gap(_box(fp), t)
                    ok, val = dist <= float(c.get("max_mm", 3)), f"{dist:.1f} mm"
            elif k == "together":
                fps = [board.footprints[r] for r in c.get("refs") or [] if r in board.footprints]
                if len(fps) >= 2:
                    spread = max(_gap(_box(a), _box(b)) for a in fps for b in fps if a is not b)
                    ok, val = spread <= float(c.get("max_mm", 15)), f"{spread:.1f} mm apart at most"
            elif k == "away":
                mine = [board.footprints[r] for r in ([c["ref"]] if c.get("ref") else c.get("refs") or []) if r in board.footprints]
                others = [board.footprints[r] for r in c.get("from") or [] if r in board.footprints]
                if mine and others:
                    near = min(_gap(_box(a), _box(b)) for a in mine for b in others)
                    ok, val = near >= float(c.get("min_mm", 5)), f"{near:.1f} mm"
            elif k == "edge" and outline:
                fp = board.footprints.get(c["ref"])
                if fp is not None:
                    b = _box(fp)
                    d = {"left": b[0] - outline[0], "top": b[1] - outline[1], "right": outline[2] - b[2], "bottom": outline[3] - b[3]}
                    e = c.get("edge", "any")
                    dist = min(d.values()) if e == "any" else d.get(e, 99)
                    ok, val = dist <= float(c.get("max_mm", 1.0)), f"{max(dist, 0):.1f} mm from the {'nearest' if e == 'any' else e} edge"
            elif k == "side":
                fp = board.footprints.get(c["ref"])
                if fp is not None:
                    ok, val = fp.side == c.get("side", "F"), "bottom" if fp.side == "B" else "top"
        except (KeyError, TypeError, ValueError):
            ok = None
        out.append({"constraint": c, "ok": ok, "text": describe(c) + (f": {val}" if val else ""), "measured": val})
    return out


# ----------------------------------------------------------------------------- the score
def _mst(pts):
    """Edges of a minimum spanning tree over points (Prim, O(n^2): nets are small)."""
    if len(pts) < 2:
        return []
    inside, edges = {0}, []
    best = {i: (math.dist(pts[0], pts[i]), 0) for i in range(1, len(pts))}
    while best:
        i = min(best, key=lambda k: best[k][0])
        d, j = best.pop(i)
        edges.append((pts[j], pts[i]))
        inside.add(i)
        for k in best:
            dk = math.dist(pts[i], pts[k])
            if dk < best[k][0]:
                best[k] = (dk, i)
    return edges


def _cross(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    if max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0]) or max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1]):
        return False
    return (o(a, b, c) * o(a, b, d) < -1e-9) and (o(c, d, a) * o(c, d, b) < -1e-9)


def score(board, nl=None, plan=None, grounds=None, hot=None):
    """How good the placement is, from parts anyone can check: short connections, few crossings, decoupling at the pins,
    connectors at the edge, constraints kept, hot parts apart. {total 0..100, parts: [{id, name, score, detail}]}."""
    grounds = set(grounds or ())
    nets = {}
    for fp in board.fp_list:
        for pd in fp.pads:
            if pd.net and not pd.net.startswith("unconnected-"):
                nets.setdefault(pd.net, []).append((fp.ref, (pd.x, pd.y)))
    signal = {n: v for n, v in nets.items() if n.rsplit("/", 1)[-1] not in grounds and not re.match(r"^(/)?(GND|AGND|DGND|PGND|VSS)\b", n.rsplit("/", 1)[-1], re.I)}
    edges = []
    for n, v in signal.items():
        pts = list({p for _, p in v})
        if 2 <= len(pts) <= 40:
            edges += [(n, a, b) for a, b in _mst(pts)]
    parts = []
    if getattr(board, "outline", None):
        xs = [p[0] for l in board.outline for p in l]
        ys = [p[1] for l in board.outline for p in l]
        ob = (min(xs), min(ys), max(xs), max(ys))
    else:
        bxs = [_box(f) for f in board.fp_list] or [(0, 0, 1, 1)]
        ob = (min(b[0] for b in bxs), min(b[1] for b in bxs), max(b[2] for b in bxs), max(b[3] for b in bxs))
    diag = math.hypot(ob[2] - ob[0], ob[3] - ob[1]) or 1.0
    if edges:
        avg = sum(math.dist(a, b) for _, a, b in edges) / len(edges)
        r = avg / diag
        parts.append({"id": "length", "name": "Short connections", "score": round(max(0, min(100, 100 * (0.45 - r) / 0.30))),
                      "detail": f"{avg:.1f} mm a connection on average ({100 * r:.0f} % of the board's diagonal)"})
        segs = edges[:1500]
        crossings = sum(1 for i in range(len(segs)) for j in range(i + 1, len(segs)) if segs[i][0] != segs[j][0] and _cross(segs[i][1], segs[i][2], segs[j][1], segs[j][2]))
        parts.append({"id": "crossings", "name": "Few crossings", "score": round(max(0, 100 * (1 - crossings / max(8, len(segs))))),
                      "detail": f"{crossings} crossing{'s' if crossings != 1 else ''} among {len(segs)} connections"})
    if nl is not None:
        try:
            from .checks.power import power_pins
            pins = power_pins(nl, grounds)
        except Exception:
            pins = []
        caps = [f for f in board.fp_list if f.ref[:1] == "C" and f.ref[1:2].isdigit()]
        ok, far = 0, []
        for ref, pin, net in pins:
            fp = board.footprints.get(ref)
            pd = next((p for p in fp.pads if str(p.num) == str(pin)), None) if fp is not None else None
            if pd is None:
                continue
            ds = [math.dist((pd.x, pd.y), (q.x, q.y)) for c in caps for q in c.pads if q.net and q.net.rsplit("/", 1)[-1] == net]
            if ds and min(ds) <= 3.5:
                ok += 1
            else:
                far.append(f"{ref} pin {pin}")
        if pins:
            parts.append({"id": "decoupling", "name": "Decoupling at the pins", "score": round(100 * ok / len(pins)),
                          "detail": f"{ok} of {len(pins)} supply pins have a capacitor within 3.5 mm" + (f"; not {', '.join(far[:4])}" if far else "")})
    conns = [f for f in board.fp_list if CONN.match(f.ref)]
    if conns:
        at = [f for f in conns if min(_box(f)[0] - ob[0], _box(f)[1] - ob[1], ob[2] - _box(f)[2], ob[3] - _box(f)[3]) <= 1.5]
        parts.append({"id": "edge", "name": "Connectors at the edge", "score": round(100 * len(at) / len(conns)),
                      "detail": f"{len(at)} of {len(conns)} connectors at the board edge" +
                                (f"; not {', '.join(f.ref for f in conns if f not in at)[:60]}" if len(at) < len(conns) else "")})
    ev = evaluate(board, plan or {})
    judged = [e for e in ev if e["ok"] is not None]
    if judged:
        kept = sum(1 for e in judged if e["ok"])
        parts.append({"id": "constraints", "name": "Constraints kept", "score": round(100 * kept / len(judged)),
                      "detail": f"{kept} of {len(judged)} kept" + "".join(f"; broken: {e['text']}" for e in judged if not e["ok"])[:200]})
    if hot:
        hfp = [board.footprints[r] for r in hot if r in board.footprints]
        close = [(a.ref, b.ref) for i, a in enumerate(hfp) for b in hfp[i + 1:] if _gap(_box(a), _box(b)) < 4.0]
        parts.append({"id": "heat", "name": "Hot parts apart", "score": max(0, 100 - 35 * len(close)),
                      "detail": "the parts that get warm are apart" if not close else "close together: " + ", ".join(f"{a}/{b}" for a, b in close[:4])})
    total = round(sum(p["score"] for p in parts) / len(parts)) if parts else None
    return {"total": total, "parts": parts}
