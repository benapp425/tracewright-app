"""The pin plan: on a breakout-style design, which connector pin each free GPIO lands on, chosen from the layout. A
schematic script fixes it before anything is placed; once a dense part is broken out (tw.breakout) its signals leave on
known layers at known places, and putting each on the connector pin that lies the same way round turns a tangle into
runs side by side.

    plan = propose(project)      {"swaps": {ref: {old pin: new pin}}, "moves": [{net, ref, from, to}], "groups",
                                  "crossings_before", "crossings_after", "lines"}
    apply(project, plan)          design/pin-plan.json (the schematic generator draws each net on its planned pin),
                                  the schematic generated again, the board updated from it, docs/pin-plan.md

What may move: a net that joins one connector pin to one pin of a chip and is named after that chip pin (GPIO_AD_18 on
ball N13 named GPIO_AD_18: a pin brought out as it is). Nets with another name carry a function (UART1_TX, USB2_D_P) and
stay. A net moves only among the pins of its own connector and the same unit of its symbol (a DF40's row), and
tracewright.json "pinplan": {"fixed": ["GPIO_AD_24", "J701"]} keeps nets or whole connectors as they are.

The cost is the crossings between nets that leave on the same layer (nets on different layers cross freely), then a
little length; a pairwise-swap search from the current plan and from an ordered start keeps the better.
"""
import json, os, re, collections, subprocess, sys

from . import geom

LAMBDA = 0.02           # cost of a mm of length against one crossing


def _short(n):
    return (n or "").rsplit("/", 1)[-1]


def _named_after(net, pin_name):
    s, p = _short(net).upper(), (pin_name or "").upper()
    if not p or len(p) < 3:
        return False
    s = re.sub(r"_(1V8|3V3|2V5|1V2|5V)$", "", s)
    return s == p or s.endswith("_" + p) or p.endswith(s) and len(s) >= 5


def _units(project):
    """{ref: {pin: unit}} from the schematic (a symbol drawn in several units)."""
    from .schematic import Hierarchy
    out = collections.defaultdict(dict)
    try:
        h = Hierarchy.load(project.sch)
    except Exception:
        return out
    for _, s in h.all_symbols():
        for pin in s.pins:
            out[s.ref][str(pin[0])] = getattr(s, "unit", 1)
    return out


def groups(project, board=None):
    """[{ref, unit, pins: [pin], nets: {pin: net}, ends: {net: (ref, pin)}}]: the pins whose nets may trade places."""
    from .board import Board
    b = board or Board.load(project.pcb)
    cfg = getattr(project, "cfg", None) or {}
    fixed = set((cfg.get("pinplan") or {}).get("fixed") or [])
    pads = collections.defaultdict(list)
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net and not p.net.startswith("unconnected-"):
                pads[p.net].append((fp, p))
    units = _units(project)
    names = {}                                          # (ref, pin) -> the symbol pin's name (KiCad's netlist)
    try:
        from .netlist import Netlist
        nl = Netlist.load(os.path.join(project.build, f"{project.stem}.net"))
        names = {k: v.get("name", "") for k, v in nl.pin_info.items()}
    except Exception:
        pass
    try:
        from . import netmodel
        kinds = {n: (r or {}).get("kind") for n, r in netmodel.for_project(project).records.items()}
    except Exception:
        kinds = {}
    out = collections.defaultdict(lambda: {"pins": [], "nets": {}, "ends": {}})
    for net, ps in pads.items():
        if len(ps) != 2 or _short(net) in fixed or net in fixed:
            continue
        if kinds.get(net) in ("pair", "clock", "power", "ground", "rf"):
            continue
        (fa, pa), (fb, pb) = ps
        for conn, cp, chip, ip in ((fa, pa, fb, pb), (fb, pb, fa, pa)):
            if not conn.ref.startswith(("J", "P", "CN", "X")) or conn.ref in fixed or chip.ref.startswith(("J", "P", "CN", "X")):
                continue
            if not _named_after(net, ip.pinfunction or names.get((chip.ref, ip.num), "")):
                continue
            unit = units.get(conn.ref, {}).get(cp.num, 1)
            g = out[(conn.ref, unit)]
            g["pins"].append(cp.num)
            g["nets"][cp.num] = net
            g["ends"][net] = (chip.ref, ip.num)
            break
    return [{"ref": k[0], "unit": k[1], **v} for k, v in sorted(out.items(), key=lambda kv: str(kv[0])) if len(v["pins"]) >= 2]


def _cross(a, b, c, d):
    return geom.segments_cross(a, b, c, d)


def propose(project, board=None, log=print):
    """The plan: for each group, which net goes on which of its pins."""
    from .board import Board
    from . import breakout
    b = board or Board.load(project.pcb)
    gs = groups(project, b)
    if not gs:
        return {"swaps": {}, "moves": [], "groups": [], "crossings_before": 0, "crossings_after": 0,
                "lines": ["no connector pins carry free GPIO (nets named after the chip pin they bring out)"]}
    # where each net leaves its chip (the breakout's port and layer), and where each connector pin is reached
    rep = breakout.run(project, apply=False, log=lambda m: None)
    port, ball = {}, {}
    field = {}
    for part in rep["parts"]:
        for e in part.get("escapes", []):
            port[e["net"]] = (tuple(e["at"]), e["layer"])
            ball[e["net"]] = (e["ref"], e["pad"])
        for e in part.get("fields", []):
            field[(e["ref"], e["pad"])] = tuple(e["at"])
    pad_at = {(fp.ref, p.num): (p.x, p.y) for fp in b.fp_list for p in fp.pads}
    end_of = {}
    for g in gs:
        for net, (cref, cpin) in g["ends"].items():
            end_of[net] = port.get(net, (pad_at[(cref, cpin)], None))
    # every other two-ended net: fixed segments the planned ones should not cross on their layer
    fixed = []
    swappable = {n for g in gs for n in g["nets"].values()}
    nets = collections.defaultdict(list)
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net and not p.net.startswith("unconnected-"):
                nets[p.net].append((fp.ref, p.num))
    for net, eps in nets.items():
        if net in swappable or len(eps) != 2 or net not in port:
            continue
        a, layer = port[net]
        far = next((e for e in eps if e != ball[net]), None)
        if far:
            fixed.append((a, field.get(far, pad_at[far]), layer))
    target = lambda ref, pin: field.get((ref, pin), pad_at[(ref, pin)])

    def segs(assign):
        out = {}
        for g in gs:
            for pin, net in assign[(g["ref"], g["unit"])].items():
                a, layer = end_of[net]
                out[net] = (a, target(g["ref"], pin), layer)
        return out

    def cost(S):
        items = list(S.values()) + fixed
        c = 0
        by = collections.defaultdict(list)
        for a, t, l in items:
            by[l].append((a, t))
        for l, ss in by.items():
            for i in range(len(ss)):
                for j in range(i + 1, len(ss)):
                    if _cross(ss[i][0], ss[i][1], ss[j][0], ss[j][1]):
                        c += 1
        return c, sum(geom.dist(a, t) for a, t, _ in S.values())

    cur = {(g["ref"], g["unit"]): dict(g["nets"]) for g in gs}
    c0, l0 = cost(segs(cur))

    def search(assign):
        S = segs(assign)
        layer_items = collections.defaultdict(list)

        def crossings_of(net, seg, S):
            a, t, l = seg
            n = 0
            for m, (a2, t2, l2) in S.items():
                if m != net and l2 == l and _cross(a, t, a2, t2):
                    n += 1
            for a2, t2, l2 in fixed:
                if l2 == l and _cross(a, t, a2, t2):
                    n += 1
            return n
        improved = True
        rounds = 0
        while improved and rounds < 12:
            improved, rounds = False, rounds + 1
            for g in gs:
                key = (g["ref"], g["unit"])
                pins = sorted(assign[key], key=lambda p_: (len(p_), p_))
                for i in range(len(pins)):
                    for j in range(i + 1, len(pins)):
                        pi, pj = pins[i], pins[j]
                        ni, nj = assign[key][pi], assign[key][pj]
                        si, sj = S[ni], S[nj]
                        before = crossings_of(ni, si, S) + crossings_of(nj, sj, S) - (1 if si[2] == sj[2] and _cross(si[0], si[1], sj[0], sj[1]) else 0)
                        ti = (si[0], target(g["ref"], pj), si[2])
                        tj = (sj[0], target(g["ref"], pi), sj[2])
                        S2 = dict(S)
                        S2[ni], S2[nj] = ti, tj
                        after = crossings_of(ni, ti, S2) + crossings_of(nj, tj, S2) - (1 if ti[2] == tj[2] and _cross(ti[0], ti[1], tj[0], tj[1]) else 0)
                        dl = (geom.dist(ti[0], ti[1]) + geom.dist(tj[0], tj[1]) - geom.dist(si[0], si[1]) - geom.dist(sj[0], sj[1]))
                        if (after - before) + LAMBDA * dl < -1e-9:
                            assign[key][pi], assign[key][pj] = nj, ni
                            S = S2
                            improved = True
        return assign
    best = search({k: dict(v) for k, v in cur.items()})
    cb, lb = cost(segs(best))
    # an ordered start: in each group, the nets in the order their chip ends lie along the connector's pins
    alt = {}
    for g in gs:
        key = (g["ref"], g["unit"])
        pins = list(g["nets"])
        P = [target(g["ref"], p) for p in pins]
        ax = 0 if (max(q[0] for q in P) - min(q[0] for q in P)) >= (max(q[1] for q in P) - min(q[1] for q in P)) else 1
        pins_sorted = sorted(pins, key=lambda p: target(g["ref"], p)[ax])
        nets_sorted = sorted(g["nets"].values(), key=lambda n: end_of[n][0][ax])
        alt[key] = dict(zip(pins_sorted, nets_sorted))
    alt = search(alt)
    ca, la = cost(segs(alt))
    if (ca, la) < (cb, lb):
        best, cb, lb = alt, ca, la
    swaps, moves = {}, []
    for g in gs:
        key = (g["ref"], g["unit"])
        was = {net: pin for pin, net in g["nets"].items()}
        for pin, net in best[key].items():
            if was[net] != pin:
                swaps.setdefault(g["ref"], {})[was[net]] = pin
                moves.append({"net": net, "ref": g["ref"], "from": was[net], "to": pin})
    lines = [f"{len(moves)} of {sum(len(g['nets']) for g in gs)} free GPIO move to another pin of their connector: "
             f"{c0} crossings on the same layer before, {cb} after"]
    log(lines[0])
    return {"swaps": swaps, "moves": moves, "groups": [{"ref": g["ref"], "unit": g["unit"], "pins": len(g["pins"])} for g in gs],
            "crossings_before": c0, "crossings_after": cb, "length_before": round(l0, 1), "length_after": round(lb, 1),
            "lines": lines}


def load(root):
    """{ref: {old pin: new pin}} from design/pin-plan.json, or {}."""
    try:
        with open(os.path.join(root, "design", "pin-plan.json")) as f:
            d = json.load(f)
        return {r: {str(k): str(v) for k, v in m.items()} for r, m in (d.get("swaps") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def apply(project, plan, regenerate=True, log=print):
    """Save the plan (merged with any earlier one), draw the schematic again with it, and update the board."""
    old = load(project.root)
    # an earlier plan moved pin a -> b; this one is in terms of today's pins: compose them
    merged = {r: dict(m) for r, m in old.items()}
    for ref, m in plan["swaps"].items():
        prev = merged.get(ref, {})
        inv = {v: k for k, v in prev.items()}               # today's pin b came from the script's pin a
        new = dict(prev)
        for b_, c in m.items():
            a = inv.get(b_, b_)
            new[a] = c
        merged[ref] = {k: v for k, v in new.items() if k != v}
    os.makedirs(os.path.join(project.root, "design"), exist_ok=True)
    with open(os.path.join(project.root, "design", "pin-plan.json"), "w") as f:
        json.dump({"swaps": merged, "why": "pins chosen from the layout by tw.pinplan: each free GPIO on the connector pin "
                                           "that lies the way its escape leaves the chip",
                   "crossings": [plan.get("crossings_before"), plan.get("crossings_after")]}, f, indent=1)
    doc = ["# Pin plan", "", "_Written by `./tw pins`: free GPIO moved to the connector pin that lies the way they leave the chip._", "",
           "| Net | Connector | Was pin | Now pin |", "|---|---|---|---|"]
    for m in sorted(plan["moves"], key=lambda m: (m["ref"], int(m["to"]) if m["to"].isdigit() else 0)):
        doc.append(f"| {_short(m['net'])} | {m['ref']} | {m['from']} | {m['to']} |")
    os.makedirs(os.path.join(project.root, "docs"), exist_ok=True)
    with open(os.path.join(project.root, "docs", "pin-plan.md"), "w") as f:
        f.write("\n".join(doc) + "\n")
    out = {"saved": True, "moves": len(plan["moves"])}
    script = os.path.join(project.root, "design", "schematic.py")
    if regenerate and os.path.exists(script):
        cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")
        r = subprocess.run([sys.executable, cli, "schematic"], cwd=project.root, capture_output=True, text=True, timeout=1800)
        last = [l for l in r.stdout.strip().splitlines() if l.strip()][-1:] or [""]
        out["schematic"] = last[0]
        if r.returncode != 0 or not last[0].startswith("Schematic done"):
            out["error"] = "the schematic did not generate: " + (r.stderr[-800:] or last[0])
            return out
        from .pcb import client
        s = client.sync(project)
        out["board"] = "updated" if s.get("ok", True) else s
        try:                                             # the connectors' pin tables from the netlist as it is now
            from . import pinout
            out["pinout"] = os.path.relpath(pinout.write(project), project.root)
        except Exception:
            pass
    return out
