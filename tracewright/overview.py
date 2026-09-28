"""A project at a glance, for the Overview tab: where the design is, the numbers that matter (checks,
board, parts, cost), what to do next, and what happened lately. Read from what the app already
keeps (the board, the BOM cache, the last check run, the history, the conversations) without running
anything slow."""
import os
from . import history


def _board_stats(rt):
    tw = rt.p.tw
    if not tw.has_pcb():
        return None
    b = rt.board()
    if b is None:
        return None
    s = b.summary()
    placed = [f for f in b.fp_list if f.pads and (not b.outline or b.on_board(f))]
    signal = {p.net for p in b.pads() if p.net}
    unrouted = rt._unrouted()
    return {"w": s["size_mm"][0], "h": s["size_mm"][1], "layers": s["copper_layers"], "parts": s["footprints"], "placed": len(placed),
            "off_board": len(s.get("off_board") or []), "tracks": s["tracks"], "vias": s["vias"], "nets": len(signal),
            "routed": len({t.net for t in b.tracks if t.net} & signal),
            "unrouted": None if unrouted is None else len(unrouted), "outline": s["outline_closed"]}


def _bom(rt):
    from . import bom as bomlib
    tw = rt.p.tw
    if not tw.has_sch():
        return None
    try:
        d = bomlib.bom_data(tw, rt.board() if tw.has_pcb() else None)
    except Exception:
        return None
    return None if d.get("empty") else d.get("totals")


AUTOMATIC = ("Before updating the toolkit",)          # housekeeping checkpoints: in History, not in Recent activity


def _activity(rt, app, n=6):
    out = []
    for c in [c for c in history.log(rt.p.root, 3 * n) if not c["message"].startswith(AUTOMATIC)][:n]:
        out.append({"kind": "checkpoint", "when": c["date"], "text": c["message"], "by": "claude" if c["message"].startswith(("Claude", "Before")) else "you"})
    a = app.agents.get(rt.p.id)
    idx = a.index() if a else _sessions_index(rt)
    for s in idx[:3]:
        out.append({"kind": "conversation", "when": s.get("updated"), "text": s.get("title") or "Conversation", "turns": s.get("turns", 0), "sid": s["sid"]})
    out.sort(key=lambda x: str(x.get("when") or ""), reverse=True)
    return out[:n]


def _sessions_index(rt):
    import json
    try:
        with open(os.path.join(rt.p.state_dir("sessions"), "index.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def check_count():
    """How many design checks run by default (the number the app quotes)."""
    try:
        from tw.checks import load_all
        return len([c for c in load_all() if c.default and not c.id.startswith("project.")])
    except Exception:
        return 0


def next_steps(p, board, bom, checks, sourcing):
    """Up to four things to do now, most useful first: {title, detail, action: {kind: chat | tab | command, ...}}."""
    tw = p.tw
    out = []
    ask = lambda text: {"kind": "chat", "text": text}
    if not tw.has_sch():
        out.append({"icon": "sparkles", "title": "Describe the board", "detail": "Claude writes the requirements and starts the schematic.",
                    "action": ask("Start the design from the brief: ask me only what changes the design, then write docs/requirements.md.")})
        return out
    if not tw.has_pcb():
        out.append({"icon": "circuit-board", "title": "Create the board", "detail": "From the schematic, with a proposed outline.",
                    "action": ask("Create the board from the schematic (sync_board), then propose an outline, mounting holes and connector positions before placing anything.")})
    elif board:
        loose = board["parts"] - board["placed"]
        if loose > 0:
            out.append({"icon": "move", "title": f"Place {loose} part{'s' if loose != 1 else ''}", "detail": "Not on the board yet.",
                        "action": ask("Place the parts that are not on the board yet, in functional groups, one group at a time, explaining each step.")})
        left = (board["nets"] - board["routed"]) if board["nets"] else 0
        if board["unrouted"]:
            out.append({"icon": "route", "title": f"Route {board['unrouted']} open connection{'s' if board['unrouted'] != 1 else ''}", "detail": "Found by the last DRC.",
                        "action": ask("Route the unrouted connections: supplies first, then signals, and run the routing checks afterwards.")})
        elif left > 0 and board["tracks"] == 0:
            out.append({"icon": "route", "title": "Route the board", "detail": f"{board['nets']} nets, no copper yet.",
                        "action": ask("Route the board: supplies first, then pairs, then signals. Stream it and run the routing checks afterwards.")})
    c = (checks or {}).get("counts") or {}
    if not checks:
        out.append({"icon": "list-checks", "title": "Run checks", "detail": f"{check_count()} design, fab and assembly checks.", "action": {"kind": "command", "name": "run-checks"}})
    elif c.get("error"):
        out.append({"icon": "circle-x", "title": f"Fix {c['error']} check error{'s' if c['error'] != 1 else ''}", "detail": "Errors block ordering.",
                    "action": ask("Explain the check errors, most important first, and fix what you can.")})
    elif c.get("warning"):
        out.append({"icon": "triangle-alert", "title": f"Review {c['warning']} warning{'s' if c['warning'] != 1 else ''}", "detail": "Fix or waive each before ordering.",
                    "action": {"kind": "tab", "tab": "checks"}})
    if bom and sourcing == "jlc" and bom.get("no_lcsc"):
        out.append({"icon": "microchip", "title": f"Find LCSC codes for {bom['no_lcsc']} part{'s' if bom['no_lcsc'] != 1 else ''}", "detail": "Needed for JLC assembly.",
                    "action": ask("Find in-stock LCSC codes (JLC Basic parts where possible) for every assembled part that has none, and add them to the schematic.")})
    if checks and not c.get("error") and tw.has_pcb():
        out.append({"icon": "shopping-cart", "title": "Order the board", "detail": "Generate the files and send them to the fab.", "action": {"kind": "tab", "tab": "outputs"}})
    return out[:4]


def overview(app, rt):
    from . import order
    p = rt.p
    board = _board_stats(rt)
    bom = _bom(rt)
    checks = p.checks_summary()
    sourcing = order.mode(p.tw)
    est = None
    if board and bom:
        try:
            est = order.estimate(order.specs(rt.board()), bom, sourcing, 5)["total"]
        except Exception:
            est = None
    return {"project": p.summary(), "board": board, "bom": bom, "checks": checks, "sourcing": sourcing, "estimate": est,
            "estimate_parts": bool(bom and bom.get("cost") is not None),
            "next": next_steps(p, board, bom, checks, sourcing), "activity": _activity(rt, app), "check_count": check_count(),
            "has_sch": p.tw.has_sch(), "has_pcb": p.tw.has_pcb()}
