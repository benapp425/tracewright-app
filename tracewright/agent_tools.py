"""The agent's design tools (an in-process MCP server). Each tool acts on the project, tells the UI
what happened through the project's event hub, and returns a short text (or an image) to the agent.
Board edits go live into KiCad when it has the board open (tw.pcb.client), else into the file."""
import os, io, json, base64, asyncio, time, traceback, math, threading
from claude_agent_sdk import tool, create_sdk_mcp_server

from tw import env as twenv, geom
from tw.board import Board
from tw.pcb import client as pcb
from tw import live as twlive
from . import knowledge, history

TOOL_NAMES = []
AGENDA_STATUS = ("todo", "active", "done", "skipped")


def _text(s, error=False):
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if error else {})}


def _json(obj, limit=24000):
    s = json.dumps(obj, indent=1, default=str)
    if len(s) > limit:
        s = s[:limit] + f"\n... ({len(s) - limit} more characters; narrow the query)"
    return s


def _img(png_bytes, caption):
    return {"content": [{"type": "text", "text": caption},
                        {"type": "image", "data": base64.b64encode(png_bytes).decode(), "mimeType": "image/png"}]}


def build(rt, app):
    """The MCP server config for one project runtime. The SDK sends every key but `instance` to the
    Claude CLI as JSON, so nothing else may go in it."""
    return create_sdk_mcp_server("tw", version="0.1.0", tools=tool_list(rt, app))


def tool_list(rt, app):
    """The Tracewright tools for one project runtime (SdkMcpTool: .name, .handler)."""
    p = rt.p
    hub = rt.hub

    def proj():
        p.reload()
        return p.tw

    async def run(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    def wrap(fn):
        async def inner(args):
            try:
                return await fn(args or {})
            except Exception as e:
                traceback.print_exc()
                return _text(f"{type(e).__name__}: {e}", error=True)
        inner.__name__ = fn.__name__
        return inner

    tools = []

    def reg(name, desc, schema):
        def deco(fn):
            t = tool(name, desc, schema)(wrap(fn))
            tools.append(t)
            TOOL_NAMES.append(name)
            return t
        return deco

    # ------------------------------------------------------------------ status
    @reg("status", "The whole picture: stages, board summary, last check verdict, KiCad link, the user's recent "
         "edits and selection. Call at the start of work and whenever unsure.", {"type": "object", "properties": {}})
    async def status(args):
        tw = proj()
        out = {"project": p.name, "folder": p.root, "kind": p.cfg.get("kind"), "stages": [
            f"{s['title']}: {s['status']}" + (f" ({s['note']})" if s["note"] else "") for s in p.stages()]}
        if tw.has_pcb():
            b = await run(rt.board)
            out["board"] = b.summary()
            unplaced = [f.ref for f in b.fp_list if b.outline and not b.on_board(f)]
            out["board"]["unplaced"] = unplaced[:40]
            drc = os.path.join(tw.build, "drc.json")
            if os.path.exists(drc) and os.path.getmtime(drc) >= os.path.getmtime(tw.pcb) - 1:
                d = json.load(open(drc))
                out["board"]["drc"] = {"violations": len(d.get("violations", [])), "unrouted": len(d.get("unconnected_items", [])),
                                       "parity": len(d.get("schematic_parity", []))}
            else:
                out["board"]["drc"] = "stale or not run: run_checks with only ['drc'] for routing completeness"
        else:
            out["board"] = "no board yet (sync_board creates it from the schematic)"
        out["schematic"] = [f"{s['name_path']} ({s['file']}, {len(s['symbols'])} symbols)" for s in
                            (await run(rt.schematic_json) or {}).get("sheets", [])] if tw.has_sch() else "no schematic yet"
        cs = p.checks_summary()
        out["checks"] = cs or "not run yet"
        k = twenv.kicad()
        out["kicad"] = {"version": k.get("version"), "cli": k.get("cli")}      # installed KiCad (the link may be off)
        out["kicad_link"] = rt.live
        out["user_edits_pending"] = list(rt.user_changes)
        out["selection"] = rt.selection
        from . import scaffold
        out["toolkit"] = {"version": p.cfg.get("toolkit"), "matches_app": not scaffold.toolkit_outdated(p.root)}
        return _text(_json(out))

    # ------------------------------------------------------------------ board queries
    @reg("design", "The design in one read, compact: every part (value, part number and LCSC code, footprint, sheet, "
         "where it is on the board, not fitted) and every net (its kind -- a supply's voltage and current, a pair's partner "
         "-- and its pins). Read it first instead of querying part by part. ref: one part with every pin's name and net; "
         "net: one net with every pin.",
         {"type": "object", "properties": {"ref": {"type": "string"}, "net": {"type": "string"}}})
    async def design_q(args):
        from tw import design
        tw = proj()
        if not tw.has_sch():
            return _text("There is no schematic yet.", error=True)
        return _text(await run(design.text, tw, args.get("ref") or None, args.get("net") or None))

    @reg("notes", "Design notes: the reasoning behind a part, one of its pins, or a net, kept off the sheet. The app shows "
         "each beside what it explains (the user hovers it), and the user adds their own. The sheet keeps one plain line "
         "an engineer reads at a glance; the why, the numbers and the source go here. action: list (ref?: one part's) | "
         "add (ref, pin?, or net; why; short?: the sheet's line it goes with) | remove (id). The schematic script's own "
         "(g.note(..., why=...), g.why(...)) are rewritten each time it runs.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]}, "ref": {"type": "string"},
                                           "pin": {"type": "string"}, "net": {"type": "string"}, "why": {"type": "string"},
                                           "short": {"type": "string"}, "id": {"type": "string"}}, "required": ["action"]})
    async def notes_t(args):
        from tw.sch import notes as dn
        tw = proj()
        a = args["action"]
        if a == "list":
            ns = await run(dn.for_ref, tw, args["ref"]) if args.get("ref") else await run(dn.load, tw)
            if not ns:
                return _text("no design notes" + (f" on {args['ref']}" if args.get("ref") else ""))
            where = lambda x: x.get("ref", "") + (f" pin {x['pin']}" if x.get("pin") else "") if x.get("ref") else "net " + x.get("net", "")
            return _text("\n".join(f"{n['id']} [{n.get('by')}] {where(n['anchor'])}: {n.get('short') + ' -- ' if n.get('short') else ''}{n['why']}"
                                   for n in ns))
        if a == "remove":
            try:
                await run(dn.remove, tw, args.get("id") or "")
            except KeyError:
                return _text(f"no note {args.get('id')}", error=True)
            hub.emit("schematic.notes")
            return _text(f"removed {args.get('id')}")
        anchor = {"net": args["net"]} if args.get("net") else {"ref": args.get("ref"), "pin": args.get("pin")}
        if len(str(args.get("why") or "").strip()) < 8:
            return _text("why: the reasoning, in a sentence or two", error=True)
        try:
            n = await run(dn.add, tw, anchor, args["why"], args.get("short") or "", "", "claude")
        except ValueError as e:
            return _text(str(e), error=True)
        hub.emit("schematic.notes")
        return _text(f"noted {n['id']} on {n['anchor']}")

    @reg("board", "Query the board. what: summary | placement (the placement score and its parts, the constraints kept or "
         "broken, the stages, parts with no reason) | routing (the routing plan: each net auto, guided or hand, why, its "
         "rules, the router preset and how each net went last time) | escape (each BGA and fine-pitch part: tracks "
         "between its pads, whether a via fits between its balls, rings per layer, signal layers needed, whether it "
         "needs HDI) | footprints (ref, value, footprint, x, y, rot, side) | footprint (one "
         "ref: pads with nets and positions) | nets (net -> pads) | net (one net: pads, tracks, vias, length) | "
         "outline | unrouted (from the last DRC) | zones. Coordinates in mm (KiCad axes, y down). When KiCad has the "
         "board open, positions include its unsaved edits.",
         {"type": "object", "properties": {"what": {"type": "string"}, "ref": {"type": "string"},
                                           "net": {"type": "string"},
                                           "region": {"type": "array", "items": {"type": "number"}}},
          "required": ["what"]})
    async def board_q(args):
        tw = proj()
        if not tw.has_pcb():
            return _text("There is no board yet. Use sync_board to create it from the schematic.", error=True)
        b = await run(rt.board)
        w = args.get("what", "summary")
        live = rt._live_place if rt.live.get("board_open") else None
        region = args.get("region")

        def inreg(x, y):
            return not region or (region[0] <= x <= region[2] and region[1] <= y <= region[3])
        if w == "summary":
            return _text(_json(b.summary()))
        if w == "placement":                          # the score, the constraints, the reasons: what the user sees
            from tw import placeplan
            from tw.checks.context import Context
            plan = await run(placeplan.load, tw)
            nl, grounds = None, set()
            if tw.has_sch():
                ctx = Context(tw, offline=True)
                nl, grounds = await run(lambda: ctx.netlist), await run(ctx.ground_nets)
            sc = await run(placeplan.score, b, nl, plan, grounds)
            ev = placeplan.evaluate(b, plan)
            lines = [f"placement score {sc['total']}"] + [f"  {p_['name']}: {p_['score']} ({p_['detail']})" for p_ in sc["parts"]]
            lines += [f"constraint {'KEPT' if e['ok'] else 'BROKEN' if e['ok'] is False else '?'} [{e['constraint'].get('by')}]: {e['text']}" for e in ev]
            lines += [f"stage {k}: {'done' if plan['stages'].get(k) else 'not yet'}" for k in placeplan.STAGES]
            missing = sorted(f.ref for f in b.fp_list if f.ref not in plan["parts"] and not f.ref.startswith(("#", "H", "MH", "FID")))
            if missing:
                lines.append(f"no reason recorded for {len(missing)} parts: {', '.join(missing[:30])}")
            return _text("\n".join(lines))
        if w == "escape":                             # how the dense parts get their pins out
            from tw import escape as esc, hdi as hdimod
            plans = await run(esc.plan, tw, b)
            if not plans:
                return _text("no ball-grid or fine-pitch parts on the board")
            out = [hdimod.describe(tw.cfg)]
            for pl in plans:
                out += pl["lines"]
                if pl.get("method") and pl["kind"] == "array" and pl["method"] != "top":
                    out.append(f"  fan it out: fanout ref={pl['ref']} (method {pl['method']})")
            return _text("\n".join(out))
        if w == "routing":                            # who routes each net, as the user sees it in Board > Routing
            from tw import routeplan
            if not tw.has_sch():
                return _text("no schematic: no nets to plan", error=True)
            plan = await run(routeplan.classify, tw)
            try:
                rep = json.load(open(os.path.join(tw.build, "route-report.json"))).get("nets") or {}
            except (OSError, ValueError):
                rep = {}
            pre = routeplan.preset(tw)
            lines = [f"router preset: {routeplan.PRESETS[pre]['label']} ({routeplan.PRESETS[pre]['why']})"]
            for mode, title in (("hand", "HAND (the router leaves these; route them with copper, keeping the rules)"),
                                ("guided", "GUIDED (routed first, keeping their rules)")):
                rows = [(n, m) for n, m in sorted(plan.items()) if m["mode"] == mode]
                if rows:
                    lines.append(title)
                    for n, m in rows:
                        r = rep.get(n) or {}
                        last = f"; last route: {r['status']}" + (f", {r['note']}" if r.get("note") else "") if r.get("status") else ""
                        lines.append(f"  {n.rsplit('/', 1)[-1]}: {m['why']}" + (f" [{', '.join(m['rules'])}]" if m["rules"] else "") + last)
            auto = sorted(n.rsplit("/", 1)[-1] for n, m in plan.items() if m["mode"] == "auto")
            if auto:
                lines.append(f"AUTO ({len(auto)}): {', '.join(auto[:60])}{' ...' if len(auto) > 60 else ''}")
            return _text("\n".join(lines))
        if w == "footprints":
            rows = []
            for f in b.fp_list:
                x, y, r, sd = f.x, f.y, f.angle, f.side
                if live and f.ref in live:
                    x, y, r, sd = live[f.ref]["x"], live[f.ref]["y"], live[f.ref]["rot"], live[f.ref]["side"]
                if inreg(x, y):
                    bb = f.bbox()
                    rows.append(f"{f.ref:<7} {f.value[:18]:<18} {f.lib_id.split(':')[-1][:34]:<34} x={x:.2f} y={y:.2f} "
                                f"rot={r:g} {sd} size={bb[2] - bb[0]:.1f}x{bb[3] - bb[1]:.1f}")
            return _text("\n".join(rows) or "no footprints in that region")
        if w == "footprint":
            f = b.footprints.get(args.get("ref", ""))
            if not f:
                return _text(f"no footprint {args.get('ref')}", error=True)
            pads = [f"pad {pd.num}: {pd.net or '-'} at ({pd.x:.3f}, {pd.y:.3f}) {pd.shape} {pd.w:.2f}x{pd.h:.2f} "
                    f"{'/'.join(l for l in pd.layers if l.endswith('.Cu'))}" for pd in f.pads]
            cy = f.courtyard()
            return _text(f"{f.ref} {f.value} {f.lib_id} at ({f.x:.3f}, {f.y:.3f}) rot {f.angle:g} side {f.side}"
                         f"{' locked' if f.locked else ''}\nbbox {tuple(round(v, 2) for v in f.bbox())}"
                         f"{' courtyard ' + str(tuple(round(v, 2) for v in geom.bbox(cy[0]))) if cy else ''}\n" + "\n".join(pads))
        if w == "nets":
            np_ = b.net_pads()
            return _text("\n".join(f"{n}: " + " ".join(f"{q.ref}.{q.num}" for q in ps) for n, ps in sorted(np_.items())))
        if w == "net":
            name = args.get("net", "")
            full = [n for n in b.nets if n == name or n.rsplit("/", 1)[-1] == name]
            if not full:
                return _text(f"no net {name}", error=True)
            n = full[0]
            pads = [f"{q.ref}.{q.num}({q.x:.2f},{q.y:.2f})" for q in b.pads() if q.net == n]
            tr = [t for t in b.tracks if t.net == n]
            vi = [v for v in b.vias if v.net == n]
            return _text(f"{n}: {len(pads)} pads {' '.join(pads)}\n{len(tr)} tracks, {sum(t.length() for t in tr):.1f} mm, "
                         f"widths {sorted({round(t.w, 3) for t in tr})}, layers {sorted({t.layer for t in tr})}; {len(vi)} vias")
        if w == "outline":
            return _text(_json({"bbox": b.bbox(), "size": b.size(), "loops": [[(round(x, 2), round(y, 2)) for x, y in l][:64]
                                                                            for l in b.outline], "open_pieces": len(b.outline_open)}))
        if w == "zones":
            return _text("\n".join(f"{'rule area' if z.is_rule_area else 'zone'} '{z.name}' net={z.net or '-'} layers={z.layers} "
                                   f"priority={z.priority} keepout={z.keepout}" for z in b.zones) or "no zones")
        if w == "unrouted":
            f = os.path.join(tw.build, "drc.json")
            if not os.path.exists(f):
                return _text("no DRC yet: run_checks with only ['drc']")
            d = json.load(open(f))
            rows = [" to ".join(i.get("description", "")[:60] for i in u.get("items", [])[:2]) for u in d.get("unconnected_items", [])]
            return _text(f"{len(rows)} unrouted (DRC {d.get('date', '')}):\n" + "\n".join(rows[:200]))
        return _text(f"unknown 'what': {w}", error=True)

    # ------------------------------------------------------------------ pointing at things
    @reg("show", "Point at parts, nets or places on the user's screen (the board or schematic view highlights them and "
         "zooms there; KiCad selects them when the board is open). Use it whenever you talk about specific parts.",
         {"type": "object", "properties": {
             "refs": {"type": "array", "items": {"type": "string"}}, "nets": {"type": "array", "items": {"type": "string"}},
             "points": {"type": "array", "items": {"type": "object"}}, "region": {"type": "array", "items": {"type": "number"}},
             "note": {"type": "string"}, "view": {"type": "string", "enum": ["board", "schematic"]},
             "select_in_kicad": {"type": "boolean"}}})
    async def show(args):
        refs, nets = args.get("refs") or [], args.get("nets") or []
        hub.emit("highlight", refs=refs, nets=nets, points=args.get("points") or [], region=args.get("region"),
                 note=args.get("note", ""), view=args.get("view", "board"))
        n = 0
        if (refs or nets) and args.get("select_in_kicad", True) and rt.live.get("board_open"):
            link = await run(twlive.link_for, p.tw.pcb)
            if link:
                try:
                    n = await run(link.highlight, refs, nets)
                except Exception as e:
                    return _text(f"shown in the app; KiCad selection failed: {e}")
        return _text("shown" + (f"; {n} items selected in KiCad" if n else ""))

    @reg("annotate", "Pin notes on the board view (proposals, questions, issues) at board coordinates; they stay until "
         "cleared. items: [{x, y, text, kind: note|proposal|issue}]",
         {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}},
                                           "clear": {"type": "boolean"}}})
    async def annotate(args):
        path = os.path.join(p.state_dir(), "annotations.json")
        cur = [] if args.get("clear") or not os.path.exists(path) else json.load(open(path))
        for it in args.get("items") or []:
            cur.append({"x": float(it["x"]), "y": float(it["y"]), "text": str(it.get("text", "")),
                        "kind": it.get("kind", "note"), "t": time.time()})
        json.dump(cur, open(path, "w"), indent=1)
        hub.emit("annotations", items=cur)
        return _text(f"{len(cur)} annotations on the board")

    @reg("review", "The user's review flags: comments they left on the board, schematic or 3D view, or picked from the "
         "checks, each a request (do it) or a question (your opinion), some with marks they drew (a route sketch, an "
         "area, an arrow), each a thread. action: list (status: active (default: open or sent to you) | open | sent | done "
         "| declined | answered | all) | reply (id, text: what you changed, or your answer and reasons, outcome: done (you "
         "made the change) | declined (you disagree: nothing changed, say why) | answered (a question that needed no "
         "change)) | add (text, view: board | schematic, x, y, sheet, refs: flag something for the user to look at). "
         "Reply to every flag you were sent once you have dealt with it; the user sees each turn green (done), red "
         "(declined) or blue (answered).",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "reply", "resolve", "add"]},
                                           "id": {"type": "string"}, "outcome": {"type": "string", "enum": ["done", "declined", "answered"]},
                                           "status": {"type": "string"}, "note": {"type": "string"},
                                           "text": {"type": "string"}, "view": {"type": "string"}, "x": {"type": "number"},
                                           "y": {"type": "number"}, "sheet": {"type": "string"},
                                           "refs": {"type": "array", "items": {"type": "string"}}},
          "required": ["action"]})
    async def review_t(args):
        from .review import Review, OLD
        r = Review(p)
        a = args["action"]
        if a == "list":
            fl = r.flags(args.get("status") or "active")
            return _text("\n\n".join(Review.describe(f) + f"\n  status: {f['status']}" for f in fl) or "no flags with that status")
        if a in ("reply", "resolve"):
            outcome = args.get("outcome") or OLD.get(args.get("status"), args.get("status")) or "done"
            text = (args.get("text") or args.get("note") or "").strip()
            if len(text) < 3:
                return _text("text: what you changed, or your answer and why", error=True)
            try:
                f = r.reply(args.get("id", ""), text, who="claude", outcome=outcome)
            except KeyError:
                return _text(f"no flag {args.get('id')}: list them first", error=True)
            except ValueError as e:
                return _text(str(e), error=True)
            hub.emit("review.changed", flags=r.flags())
            left = len(r.flags("active"))
            return _text(f"{f['id']} {outcome}" + (f"; {left} flag(s) still open or sent" if left else "; no flags left"))
        where = {"x": args.get("x"), "y": args.get("y"), "sheet": args.get("sheet"), "refs": args.get("refs")}
        f = r.add(args.get("view") or "board", args.get("text", ""), where, source="claude")
        hub.emit("review.changed", flags=r.flags())
        return _text(f"added {f['id']} for the user")

    # ------------------------------------------------------------------ editing the board
    async def _apply(ops, what, pace=None):
        tw = proj()
        rt.mark_self(10)
        pace = app.settings.get("live_pace_s") if pace is None else pace
        link = await run(twlive.link_for, tw.pcb) if rt.live.get("board_open") else None
        if link and all(o.get("op") in pcb.LIVE_OPS for o in ops):
            res = await run(link.apply, ops, None, pace if any(o.get("op") == "move" for o in ops) else 0.0)
            res["via"] = "live"
            if res.get("ok"):
                await run(link.save)
        else:
            res = await run(pcb.apply, tw, ops)
        rt.mark_self(4)
        moves = [c for c in res.get("changes", []) if c.get("kind") == "move"]
        if moves:
            hub.emit("board.moves", moves=moves, via=res.get("via"))
        hub.emit("board.edit", what=what, via=res.get("via"), ok=res.get("ok"))
        return res

    @reg("place", "Move footprints: moves [{ref, x, y, rot?, side?: F|B, locked?, why}] (mm, KiCad axes, rot CCW degrees). "
         "why: one plain line the user reads when they pick the part (\"2 mm from U3 pin 7, GND via beside it\"). Place in "
         "stages: fixed (connectors, holes, what the floorplan fixed), main (the main chips), support (their decoupling, "
         "crystals, pull-ups), rest -- stage: the one these moves belong to; done: true when that stage is finished. "
         "constraints: [{kind: near (ref, to: U3 or U3.7, max_mm) | together (refs, max_mm) | away (ref, from: [refs], "
         "min_mm) | edge (ref, edge: left|right|top|bottom|any) | side (ref, side: F|B), why}] to keep from now on; the "
         "user sets theirs in the app, and a move that breaks one is reported. Shown animated in the app and, with the "
         "board open in KiCad, applied live as one undo step. Returns overlaps and off-board parts among the moved ones. "
         "Locked parts are refused: the user's stay where they are; one you locked yourself, unlock first (copper, op "
         "lock). propose: true when the user asked you to suggest placement: nothing moves; the moves (each with why) are "
         "drawn as ghosts for them to take; replies: [{note: its number, text, outcome: followed | declined}], one per "
         "note; summary.",
         {"type": "object", "properties": {"moves": {"type": "array", "items": {"type": "object"}}, "propose": {"type": "boolean"},
                                           "stage": {"type": "string", "enum": ["fixed", "main", "support", "rest"]}, "done": {"type": "boolean"},
                                           "constraints": {"type": "array", "items": {"type": "object"}},
                                           "replies": {"type": "array", "items": {"type": "object"}}, "summary": {"type": "string"}},
          "required": ["moves"]})
    async def place(args):
        from tw import placeplan
        moves = args.get("moves") or []
        b0 = await run(rt.board)
        tw_ = proj()
        for c in args.get("constraints") or []:
            try:
                await run(placeplan.add_constraint, tw_, c, "claude")
            except ValueError as e:
                return _text(f"constraint: {e}", error=True)
        if args.get("propose"):                          # a suggestion for the user: nothing moves
            from . import boardedit
            try:
                d = await run(boardedit.propose, p, b0, moves, args.get("replies"), args.get("summary") or "")
            except ValueError as e:
                return _text(str(e), error=True)
            hub.emit("board.proposal", proposal=d)
            missing = [i + 1 for i in range(len(d.get("notes") or [])) if not any(r["note"] == i + 1 for r in d.get("replies") or [])]
            return _text(f"suggested {len(d['moves'])} moves for the user to take" +
                         (f"; reply to note{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))} too" if missing else ""))
        locked = sorted(m.get("ref") for m in moves if b0 is not None and m.get("ref") in b0.footprints and b0.footprints[m["ref"]].locked)
        if locked:                                       # the user's: never touched (ask them, or leave it)
            return _text(f"{', '.join(locked)} {'is' if len(locked) == 1 else 'are'} locked: leave {'it' if len(locked) == 1 else 'them'} where "
                         "they are (the user's), or unlock first if you locked it yourself", error=True)
        ops = [dict(op="move", **{k: v for k, v in m.items() if k in ("ref", "x", "y", "rot", "side", "locked")}) for m in moves]
        res = await _apply(ops, f"placed {len(ops)} parts")
        if not res.get("ok"):
            return _text(f"placement failed: {_json(res.get('results', res))}", error=True)
        b = await run(rt.board)
        moved = {m["ref"] for m in moves}
        notes = []
        from tw.checks.placement import _polys_overlap
        for f in b.fp_list:
            if f.ref in moved and b.outline and not b.on_board(f):
                notes.append(f"{f.ref} is outside the board outline")
        cys = [(f, l) for f in b.fp_list for l in f.courtyard()]
        for f, l in cys:
            if f.ref not in moved:
                continue
            bf = geom.bbox(l)
            for g, m in cys:
                if g is f or g.side != f.side or not geom.bbox_overlap(bf, geom.bbox(m)):
                    continue
                if _polys_overlap(l, m):
                    notes.append(f"{f.ref} courtyard overlaps {g.ref}")
        whys = {m["ref"]: m.get("why") for m in moves if m.get("ref") and m.get("why")}
        if whys:
            await run(placeplan.set_why, tw_, whys, args.get("stage"), "claude")
        plan = await run(placeplan.load, tw_)
        broken = [e["text"] for e in placeplan.evaluate(b, plan) if e["ok"] is False and
                  (moved & set([e["constraint"].get("ref")] + list(e["constraint"].get("refs") or []) + list(e["constraint"].get("from") or [])))]
        unexplained = sorted(r for r in moved if r not in plan["parts"])
        tail = ""
        if broken:
            tail += "\nBreaks a constraint (fix it, or tell the user why it cannot be kept): " + "; ".join(broken)
        if unexplained:
            tail += f"\nNo reason given for {', '.join(unexplained[:10])}: add why to each move (the user reads it)."
        if args.get("done") and args.get("stage"):
            await run(placeplan.mark_stage, tw_, args["stage"])
            hub.emit("placement.changed")
            if app.settings.get("placement_stop_stages"):
                tail += (f"\nThe {args['stage']} stage is done. The user asked to check each placement stage: stop here, say in "
                         "two or three lines what you placed and why, and wait for their OK before the next stage.")
        hub.emit("placement.changed")
        return _text(f"placed {len(ops)} parts ({res.get('via')})" + ("; " + "; ".join(sorted(set(notes))) if notes else
                                                                      "; no courtyard overlaps among them") + tail)

    @reg("copper", "Board operations (see .claude/tracewright.md): ops [{op: track|tracks|via|vias|delete|zone|rule_area|"
         "outline|cutout|text|fill|lock|value|ref_text, ...}]. cutout: an internal cut-out or milled slot on the outline "
         "layer ({op: cutout, polygon: [[x, y], ...]} or {op: cutout, slot: {a: [x, y], b: [x, y], w: mm}}; drawn again "
         "over the same place it replaces the old one; remove: true takes it away). Tracks, vias, deletes go live into KiCad when it has the board "
         "open; the others save KiCad's copy, edit the file and reload it.",
         {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}}}, "required": ["ops"]})
    async def copper(args):
        ops = args.get("ops") or []
        res = await _apply(ops, f"{len(ops)} board operations")
        bad = [r for r in res.get("results", []) if not r.get("ok")]
        took = [r.get("result") for r in res.get("results", []) if r.get("op") == "delete" and r.get("ok")]
        return _text(f"{'done' if res.get('ok') else 'FAILED'} via {res.get('via')}: " +
                     ("; ".join(f"{r['op']}: {r.get('error')}" for r in bad) if bad else f"{len(ops)} ops applied") +
                     ("; deleted " + ", ".join(f"{n} item{'s' if n != 1 else ''}" for n in took if isinstance(n, int)) +
                      " (check that is all you meant)" if any(isinstance(n, int) and n for n in took) else ""),
                     error=not res.get("ok"))

    @reg("sync_board", "Update the board from the schematic (KiCad's 'Update PCB from Schematic', headless): new parts "
         "are added parked right of the board, changed footprints swapped in place, nets updated; placement and "
         "routing kept. Creates the board if there is none. remove_extra deletes footprints no longer in the schematic.",
         {"type": "object", "properties": {"remove_extra": {"type": "boolean"}}})
    async def sync_board(args):
        tw = proj()
        rt.mark_self(20)
        res = await run(pcb.sync, tw, bool(args.get("remove_extra")))
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        return _text(_json(res), error=not res.get("ok"))

    @reg("stackup", "The board's copper layers, 2 to 12: which are signal layers (and the direction each is routed in) "
         "and which are planes (and their nets), on the fab's standard build. Decide it from the design: 2 layers for "
         "simple, slow boards; 4 (signal, GND, supply, signal) once there is anything fast (USB, Ethernet, fast SPI, "
         "MIPI), a fine-pitch QFN/BGA to fan out, or EMC to meet; 6 or more for several fast interfaces, a BGA with "
         "many rows or many supplies (board what=escape says how many signal layers each BGA needs). With HDI on "
         "(the user's choice), microvias land on the layer under each outer one: make it a signal layer. Every signal layer next to a plane (its return path and impedance); two signal "
         "layers side by side routed crosswise (x and y); a ground plane under every fast layer. The router routes on "
         "the signal layers only, each in its direction; planes are poured over the whole board. action: show (what the "
         "board has, the plan, the agreed limit, the builds) | plan (layers, preset?, roles?: per layer F.Cu first, "
         "signal | plane, planes?: {layer: net}, directions?: {layer: x | y | any}, why: one or two plain sentences "
         "for the user) | apply (the saved plan onto the board: layer count, plane layers, the build, the plane pours; "
         "again once the board has its outline). The agreed limit (the user's layer count) is not changed without "
         "asking the user.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["show", "plan", "apply"]},
                                           "layers": {"type": "integer"}, "preset": {"type": "string"},
                                           "roles": {"type": "array", "items": {"type": "string"}},
                                           "planes": {"type": "object"}, "directions": {"type": "object"},
                                           "why": {"type": "string"}},
          "required": ["action"]})
    async def stackup_t(args):
        from tw import stackup, constraints
        from tw.board import Board
        tw = proj()
        a = args["action"]
        limit = (constraints.get(p.cfg) or {}).get("layers")
        b = await run(Board.load, tw.pcb) if tw.has_pcb() else None
        nets = {}
        if b is not None:
            for pd in b.pads():
                if pd.net and not pd.net.startswith("unconnected-"):
                    nets[pd.net] = nets.get(pd.net, 0) + 1
        plan = stackup.get(p.cfg)
        if a == "show":
            lines = []
            if b is not None:
                kinds = {n: t for _, n, t, _ in b.layers}
                lines.append("board: " + ", ".join(f"{l} ({kinds.get(l, '?')})" for l in b.copper) + f", {b.thickness:g} mm")
            else:
                lines.append("no board yet")
            lines.append("agreed limit: " + (f"{limit} copper layers" if limit else "none set"))
            lines.append("plan: " + (stackup.describe(plan) if plan else "none (2 signal layers; inner layers, if any, are planes)"))
            from tw import hdi as hdimod
            lines.append(hdimod.describe(p.cfg) + ("" if hdimod.on(p.cfg) else " (the user turns it on; offer it when a part cannot escape without it: board what=escape)"))
            if plan and plan.get("why"):
                lines.append("why: " + plan["why"])
            if plan and b is not None and len(b.copper) != plan["layers"]:
                lines.append(f"the board has {len(b.copper)} copper layers: apply the plan")
            lines.append("builds: " + "; ".join(f"{k} ({v['title']})" for k, v in stackup.PRESETS.items()))
            n = plan["layers"] if plan else (limit or (len(b.copper) if b is not None else 4))
            if n in stackup.COUNTS:
                h = hdimod.get(p.cfg)
                lines.append(f"a starting point for {n}:\n" + stackup.describe(stackup.default_plan(n, nets, hdi=h["on"] and h["microvias"])))
            return _text("\n".join(lines))
        if a == "plan":
            why = (args.get("why") or "").strip()
            if len(why) < 12:
                return _text("why: one or two plain sentences for the user (what needs these layers)", error=True)
            want = {k: args[k] for k in ("layers", "preset", "roles", "planes", "directions") if args.get(k) is not None}
            want["why"] = why
            from tw import hdi as hdimod
            new, probs = stackup.validate(want, nets or None, limit, hdimod.get(p.cfg))
            errs = [t for s_, t in probs if s_ == "error"]
            if errs:
                return _text("not saved: " + "; ".join(errs), error=True)
            p.reload()
            p.cfg["stackup"] = new
            await run(p.save)
            hub.emit("project.changed", summary=p.summary())
            warns = [t for s_, t in probs if s_ == "warning"]
            return _text(stackup.describe(new) + ("\nwarnings: " + "; ".join(warns) if warns else "") +
                         "\nsaved; action apply puts it on the board")
        if not plan:
            return _text("no plan yet: action plan first", error=True)
        if b is None:
            return _text("no board yet: sync_board first (it is made with the plan's layer count)", error=True)
        from tw import hdi as hdimod
        plan, probs = stackup.validate(plan, nets or None, limit, hdimod.get(p.cfg))
        errs = [t for s_, t in probs if s_ == "error"]
        if errs:
            return _text("the plan does not hold: " + "; ".join(errs), error=True)
        rt.mark_self(60)
        res = await run(stackup.apply, tw, plan)
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        if not res.get("ok"):
            return _text(res.get("error") or "the stack-up did not apply", error=True)
        return _text("applied: " + "; ".join(res.get("did", [])))

    @reg("fanout", "Fan out a ball-grid part before routing: a via for every ball that needs one -- signal balls past the "
         "rings the top layer takes out, every power and ground ball (to its plane). method: dog-bone (a short stub to a "
         "via between four balls, the default when one fits), via-in-pad or microvia (HDI: only when the user has turned "
         "it on). Adds the neck-down area and plane patches under the part so DRC and the pours hold. Read board "
         "what=escape first; route afterwards (the router starts from the vias).",
         {"type": "object", "properties": {"ref": {"type": "string"}, "method": {"type": "string", "enum": ["dog-bone", "via-in-pad", "microvia"]}},
          "required": ["ref"]})
    async def fanout_tool(args):
        from tw import escape as esc
        tw = proj()
        if not tw.has_pcb():
            return _text("no board yet", error=True)
        try:
            ops, summ = await run(esc.fanout, tw, args["ref"], args.get("method"))
        except ValueError as e:
            return _text(str(e), error=True)
        if not ops:
            return _text(f"{args['ref']}: {summ.get('note', 'nothing to fan out')}")
        rt.mark_self(60)
        res = await run(pcb.apply, tw, ops)
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        if not res.get("ok"):
            return _text("the fan-out did not apply: " + "; ".join(r.get("error", "") for r in res.get("results", []) if not r.get("ok")), error=True)
        return _text(f"{summ['ref']}: {summ['vias']} vias ({summ['method']})" + (f", {summ['stubs']} stubs" if summ.get("stubs") else "")
                     + ". Next: route; run_checks drc and hdi.vias.")

    @reg("routability", "Can the board be routed in its room and layers, before routing it: each BGA's escape against the "
         "signal layers (with a spare layer, or tight), the crowded lines across the board (nets that must cross against "
         "the tracks that fit), the tracks' demand against the free area. Says fine, tight or impossible, with fixes "
         "(more layers, a larger board, moved parts). Read it after placement, before breakout and route.",
         {"type": "object", "properties": {}})
    async def routability_tool(args):
        from tw import routability
        tw = proj()
        if not tw.has_pcb():
            return _text("no board yet", error=True)
        r = await run(routability.estimate, tw)
        return _text(f"{r['verdict']}: " + "; ".join(r["lines"]) + ("\nfixes: " + "; ".join(r["fixes"]) if r["fixes"] else ""))

    @reg("breakout", "Bring the pins of every BGA and fine-pitch part out before routing (the way a person starts a dense "
         "board): a dog-bone via for each ball that needs one, then layer by layer, outer rings first, each signal ball's "
         "track out to the edge of the ball field -- its port, where route takes the net up; beside fine-pitch parts whose "
         "nets change layer (a connector on the bottom), a via for each pad in two staggered rows. make_room: first move "
         "the parts under a BGA (its bottom capacitors) a fraction of a mm so every ball has a via spot. refs: only these "
         "parts. Then route; escapes the route did not use are taken off again.",
         {"type": "object", "properties": {"refs": {"type": "array", "items": {"type": "string"}}, "make_room": {"type": "boolean"}}})
    async def breakout_tool(args):
        from tw import breakout
        tw = proj()
        if not tw.has_pcb():
            return _text("no board yet", error=True)
        rt.mark_self(120)
        lines = []
        if args.get("make_room"):
            mr = await run(breakout.make_room, tw, args.get("refs") or None, True, 0.8, lambda m: None)
            lines += [f"{x['ref']}: {x['with_a_spot_after']} of {x['balls_needing_vias']} balls with a via spot (was "
                      f"{x['with_a_spot_before']}), {len(mr['moves'])} parts under it moved" for x in mr["parts"]]
        rep = await run(breakout.run, proj(), args.get("refs") or None, True, lambda m: None)
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        for r in rep["parts"]:
            if r["kind"] == "array":
                lines.append(f"{r['ref']}: {r['escaped']} of {r['signals'] - r['under']} signal balls out ("
                             + ", ".join(f"{k} {v}" for k, v in r["by_layer"].items()) + f"), {r['vias']} vias"
                             + (f"; not out: {', '.join(x['pad'] + ' ' + x['net'].rsplit('/', 1)[-1] for x in r['left'][:12])}" if r["left"] else ""))
            else:
                lines.append(f"{r['ref']}: {r['vias']} vias beside {r['pads']} pads" + (f", {len(r['left'])} without room" if r["left"] else ""))
        if rep.get("error"):
            return _text("the breakout did not apply: " + rep["error"], error=True)
        return _text("\n".join(lines) + "\nNext: route (it takes each net up at its port); run_checks drc.")

    @reg("pins", "The pin plan for breakout-style boards: free GPIO (nets named after the chip pin they bring out) moved to "
         "the connector pins that lie the way their escape leaves the chip, within their own connector and row, so they "
         "run side by side instead of crossing. action propose (default): the moves and the crossings before and after; "
         "apply: only once the user agrees -- it changes the schematic (design/pin-plan.json, the schematic generated "
         "again, the board updated, docs/pin-plan.md). tracewright.json pinplan.fixed keeps nets or connectors as they are.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["propose", "apply"]}}})
    async def pins_tool(args):
        from tw import pinplan
        tw = proj()
        if not tw.has_pcb():
            return _text("no board yet", error=True)
        plan = await run(pinplan.propose, tw, None, lambda m: None)
        if args.get("action") != "apply" or not plan["moves"]:
            ex = "; ".join(f"{m['net'].rsplit('/', 1)[-1]} {m['ref']}.{m['from']}->{m['to']}" for m in plan["moves"][:20])
            return _text(plan["lines"][0] + (f". First moves: {ex}" if ex else "") +
                         (". Ask the user before apply: it changes the schematic and the pin map." if plan["moves"] else ""))
        rt.mark_self(300)
        r = await run(pinplan.apply, tw, plan)
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        if r.get("error"):
            return _text(r["error"], error=True)
        return _text(f"{plan['lines'][0]}. Applied: design/pin-plan.json, docs/pin-plan.md, {r.get('schematic', '')}; board updated. "
                     "Update any pin table the design keeps (docs) from the plan; then breakout and route.")

    @reg("region", "Rules for one area of the board, kept by KiCad's DRC and the router: kind keepout (no tracks or "
         "vias: an antenna's clearance, under a crystal), novias (tracks pass, no vias), neck (finer tracks and spacing: "
         "under a dense connector; track and clearance in mm), spacing (more clearance between nets, no pour: high "
         "voltage; clearance in mm). polygon: [[x, y], ...] board mm; layers: copper layers (default all). action: list | "
         "add | remove (name). The user's regions are theirs: do not remove or redraw them unasked.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "add", "remove"]}, "name": {"type": "string"},
                                           "kind": {"type": "string", "enum": ["keepout", "novias", "neck", "spacing"]},
                                           "polygon": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                                           "layers": {"type": "array", "items": {"type": "string"}},
                                           "track": {"type": "number"}, "clearance": {"type": "number"}},
          "required": ["action"]})
    async def region_tool(args):
        from tw import regions
        tw = proj()
        if not tw.has_pcb():
            return _text("no board yet", error=True)
        a = args.get("action")
        b = await run(rt.board)
        if a == "list":
            rs = regions.listing(tw, b)
            return _text("\n".join(f"{r['text']} (by {r.get('by', 'user')})" + ("" if r.get("on_board") else " -- not on the board")
                                   for r in rs) or "no regions")
        try:
            if a == "remove":
                mine = next((r for r in tw.cfg.get("regions") or [] if r["name"] == args.get("name")), None)
                if mine and mine.get("by") == "user":
                    return _text(f"{mine['name']} is the user's region: ask them before removing it", error=True)
                ops = regions.remove(tw, args.get("name"))
            else:
                ops = regions.add(tw, args.get("name"), args.get("polygon"), args.get("kind"), args.get("layers"),
                                  args.get("track"), args.get("clearance"), by="claude", copper=b.copper)
        except KeyError:
            return _text(f"no region {args.get('name')}", error=True)
        except ValueError as e:
            return _text(str(e), error=True)
        rt.mark_self(30)
        res = await run(pcb.apply, tw, ops + [{"op": "fill"}])
        rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        hub.emit("routing.changed")
        if not res.get("ok"):
            return _text("the region did not apply: " + "; ".join(r.get("error", "") for r in res.get("results", []) if not r.get("ok")), error=True)
        return _text(("removed " + args["name"]) if a == "remove" else "set: " + regions.describe(
            next(r for r in tw.cfg.get("regions") or [] if r["name"] == regions._name_ok(args.get("name")))))

    @reg("routing_plan", "Change the routing plan the user sees in Board > Routing (board what=routing reads it). action "
         "mode: a net's mode -- auto (the router), guided (routed first with its rules), hand (left for the copper tool) "
         "or null (back to its rule); action layers: the layers the net is routed on (its pads keep a short escape on "
         "their own layer), [] for any. Respect the user's choices (marked set by you): change them only when asked.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["mode", "layers"]}, "net": {"type": "string"},
                                           "mode": {"type": ["string", "null"]}, "layers": {"type": "array", "items": {"type": "string"}}},
          "required": ["action", "net"]})
    async def routing_plan_tool(args):
        from tw import routeplan
        p.reload()
        try:
            if args["action"] == "mode":
                routeplan.set_mode(p, args["net"], args.get("mode"))
            else:
                b = await run(rt.board) if p.tw.has_pcb() else None
                routeplan.set_layers(p, args["net"], args.get("layers") or None, b.copper if b else None)
        except ValueError as e:
            return _text(str(e), error=True)
        hub.emit("routing.changed")
        return _text(f"{args['net']}: " + (f"mode {args.get('mode') or 'by its rule'}" if args["action"] == "mode"
                                          else "on " + (", ".join(args.get("layers") or []) or "any layer")))

    @reg("route", "Route nets with the grid router (human style: 0/45/90, few vias, supplies first; streamed live to the "
         "app and into KiCad when the board is open). nets: names (default: every unrouted net); clear: tear up those "
         "nets first; engine: grid | freerouting (the whole board only; no nets); finish (with freerouting): keep every "
         "track and via already there and route only what is still open -- the way to complete a dense board the grid "
         "router left nets open on; along: a review flag's id whose route sketch the nets should follow (the router keeps "
         "to the user's line where it can).",
         {"type": "object", "properties": {"nets": {"type": "array", "items": {"type": "string"}}, "clear": {"type": "boolean"},
                                           "engine": {"type": "string", "enum": ["grid", "freerouting"]},
                                           "finish": {"type": "boolean"}, "along": {"type": "string"}}})
    async def route(args):
        from tw.route import driver
        tw = proj()
        nets = args.get("nets") or None
        engine = args.get("engine", "grid")
        if engine == "freerouting" and nets:
            return _text("Freerouting routes the whole board and replaces what is already routed; it cannot route "
                         "chosen nets. Route these with the grid router (engine grid), or call freerouting without nets "
                         "for a whole-board run.", error=True)
        sketch = None
        if args.get("along"):
            from .review import Review
            try:
                fl = Review(p).get(args["along"])
            except KeyError:
                return _text(f"no flag {args['along']}", error=True)
            sketch = [m for m in fl.get("marks") or [] if m["t"] in ("route", "pen", "arrow")]
            if not sketch:
                return _text(f"{fl['id']} has no route sketch", error=True)
            if not nets:
                return _text("along: name the nets to route along the sketch", error=True)
        link = await run(twlive.link_for, tw.pcb) if rt.live.get("board_open") else None
        mirror = LiveMirror(link) if link and engine == "grid" else None
        if link:
            await run(link.save)

        def progress(ev):
            hub.emit("route.progress", **{k: v for k, v in ev.items() if k in ("status", "net", "tracks", "vias", "done",
                                                                                "total", "where", "line", "pass", "unrouted")})
            if mirror:
                mirror.on(ev)
        rt.mark_self(600)
        try:
            if engine == "freerouting" and args.get("finish"):
                from tw.route import freerouting
                res = await run(lambda: freerouting.route(tw, keep_routed=True, on_progress=progress,
                                                          log=lambda m: hub.emit("route.log", text=m)))
            elif engine == "freerouting":
                res = await run(driver.route, tw, nets=None, engine="freerouting", on_progress=progress, live=False)
            elif mirror:
                g = await run(lambda: driver.GridRoute(tw, nets=nets, clear=bool(args.get("clear")), on_progress=progress,
                                                       log=lambda m: hub.emit("route.log", text=m), sketch=sketch).setup())
                if args.get("clear"):
                    await run(link.apply, [{"op": "delete", "nets": nets or [], "all": not nets, "kinds": ["track", "via"]}])
                summary, segs, vias = await run(g.run)
                await run(mirror.finish)
                # what the file path does after routing: neck-down areas + their DRC rule, pour islands
                if g.necks:
                    from tw.pcb import rules
                    rules.ensure_rules(tw, dict([rules.neck_rule(g.neck_cl)]), replace=True)
                    driver.neck_board_rules(tw, g.neck_w, g.neck_cl)
                    areas = [o for o in g.ops([], []) if o.get("op") == "rule_area"]
                    if areas:
                        await run(pcb.apply, tw, areas)
                from tw.pcb import stitch
                for net in g.planes:
                    if not g._net_ok(net):
                        continue
                    pr = g.B.profiles[g.net_class.get(net, "Default")]
                    for _ in range(2):
                        extra = await run(lambda: stitch.plan(Board.load(tw.pcb), net, pr.via_d, pr.via_drill))
                        if not extra:
                            break
                        await run(link.apply, [{"op": "vias", "items": extra}, {"op": "fill"}])
                        await run(link.save)
                        summary["island_vias"] = summary.get("island_vias", 0) + len(extra)
                left = await run(driver.islands_left, tw, [n for n in g.planes if g._net_ok(n)])
                if left:
                    summary["islands"] = left
                await run(link.save)
                if g.ports:                               # the breakout's escapes the route did not use
                    from tw import breakout
                    summary["unused_escapes"] = await run(breakout.prune, tw, list(summary.get("failed") or []))
                from tw.route import tune as tunemod      # pairs and length groups within their budgets
                try:
                    tr = await run(tunemod.tune, tw, True, lambda m: None)
                    summary["tuned"] = [x["net"] for x in tr["tuned"]]
                except Exception:
                    pass
                await run(driver._report, tw, g, summary)
                res = {"summary": summary, "apply": {"via": "live", "ok": True}}
            else:
                res = await run(driver.route, tw, nets=nets, clear=bool(args.get("clear")), on_progress=progress, live=False,
                                log=lambda m: hub.emit("route.log", text=m), sketch=sketch)
        finally:
            rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        s = res.get("summary", {})
        failed = s.get("failed") or {}
        txt = (f"{engine}: routed {s.get('routed', '?')} of {s.get('nets', '?')} nets, {s.get('tracks', '?')} segments, "
               f"{s.get('vias', '?')} vias, {s.get('length_mm', '?')} mm in {s.get('seconds', '?')} s"
               + (f"; stitching vias {s.get('stitching_vias')}" if s.get("stitching_vias") else "")
               + (f"\nFAILED: " + "; ".join(f"{n.rsplit('/', 1)[-1]} at {w}" for n, w in failed.items()) if failed else "")
               + (f"\nPOUR ISLANDS NOT JOINED (pads cut off from the rest of their net): "
                  + "; ".join(f"{n.rsplit('/', 1)[-1]} {', '.join(w)}" for n, w in s["islands"].items()) if s.get("islands") else "")
               + (f"\nLeft for hand routing (the routing plan, Design > Routing): {', '.join(n.rsplit('/', 1)[-1] for n in s['left_for_hand'])} "
                  "-- route each in the editor's way (copper tool), keeping its rules, or name it in nets to route it here"
                  if s.get("left_for_hand") else "")
               + (f" (preset {s['preset']})" if s.get("preset") and s["preset"] != "balanced" else "")
               + (f"\nFinished by Freerouting (everything routed before held fixed); still open: "
                  + (", ".join(n.rsplit('/', 1)[-1] for n in s["still_open"]) or "none") if s.get("finished_by") else "")
               + (f"\nTuned: {', '.join(n.rsplit('/', 1)[-1] for n in s['tuned'])}" if s.get("tuned") else "")
               + ("\nNets are still open on a dense board: route with engine freerouting and finish true completes them "
                  "with what is routed held fixed." if failed and not s.get("finished_by") and engine == "grid" else ""))
        if engine == "freerouting":
            txt = f"freerouting: {_json(s)}"
        return _text(txt + "\nNext: run_checks (drc, route.style, power.width, hs.pairs) and render the board.")

    @reg("silk", "Tidy the silkscreen: move reference designators that sit on pads, other silk or the board edge to "
         "the nearest clear spot around their part. refs: only these.", {"type": "object", "properties": {
             "refs": {"type": "array", "items": {"type": "string"}}}})
    async def silk_t(args):
        from tw import silk
        tw = proj()
        b = await run(rt.board)
        ops, rep = silk.tidy(b, refs=set(args["refs"]) if args.get("refs") else None)
        if ops:
            await _apply(ops, "silk tidy", pace=0)
        return _text(f"{len(rep['kept'])} references clear, {len(rep['moved'])} moved, {len(rep['stuck'])} with no clear "
                     f"spot {rep['stuck']}")

    # ------------------------------------------------------------------ checks, images
    @reg("run_checks", "Run the design checks (ERC, DRC, readability, pinouts and packages against the real parts, voltage "
         "domains, BOM, placement, ESD placement, antenna keep-outs, power: widths, voltage drop, regulator capacitors and "
         "heat, switcher hot loops, pours and stitching, flyback, capacitor ratings, floating gates; signal integrity: "
         "return paths and return vias, crosstalk, stubs, pairs and impedance, length groups, noise near analog lines; "
         "routing quality, DFM, JLC CPL, boot straps, lessons). only: check ids or groups (default all); refresh: re-export everything. "
         "Results show in the Checks tab; returns the verdict and the findings. Each check has a time limit; one that "
         "could not verify reports it (never a pass).",
         {"type": "object", "properties": {"only": {"type": "array", "items": {"type": "string"}}, "refresh": {"type": "boolean"}}})
    async def run_checks(args):
        import threading
        from tw.checks.runner import run_all, summary_text
        tw = proj()
        async with rt.checks_lock:                 # waits for a run started from the Checks tab
            stop = threading.Event()
            rt.checks_stop = stop
            hub.emit("checks.start", only=args.get("only"))

            def prog(c, r):
                rt.mark_self(30)
                hub.emit("checks.progress", id=c.id, title=c.title, status=r["status"], n=len(r["findings"]))
            try:
                res = await run(run_all, tw, only=args.get("only") or None, refresh=bool(args.get("refresh")),
                                progress=prog, stop=stop)
            except asyncio.CancelledError:          # the turn was interrupted: end the run too
                stop.set()
                hub.emit("checks.done", stopped=True)
                raise
            except Exception as e:
                hub.emit("checks.done", error=f"{type(e).__name__}: {e}")
                raise
            finally:
                rt.mark_self(4)
                if rt.checks_stop is stop:
                    rt.checks_stop = None
        hub.emit("checks.done", counts=res["counts"], stopped=bool(res.get("stopped")))
        return _text(summary_text(res, limit=40))

    @reg("render", "Look at your work: an image of the board (what: board; layers e.g. ['F.Cu']; region [x0,y0,x1,y1] mm; "
         "highlight refs) or of a schematic sheet (what: sheet; sheet '/Power/' or '/' for the root; region in page mm).",
         {"type": "object", "properties": {"what": {"type": "string", "enum": ["board", "sheet"]}, "sheet": {"type": "string"},
                                           "region": {"type": "array", "items": {"type": "number"}},
                                           "layers": {"type": "array", "items": {"type": "string"}},
                                           "highlight": {"type": "array", "items": {"type": "string"}}},
          "required": ["what"]})
    async def render(args):
        from tw import render as rnd
        tw = proj()
        region = tuple(args["region"]) if args.get("region") else None

        def make():
            if args.get("what") == "sheet":
                rt.ensure_svgs()
                f = os.path.join(tw.build, "render", "agent-sheet.png")
                rnd.sheet_png(tw, args.get("sheet") or "/", f, region=region)
                from PIL import Image
                im = Image.open(f)
            else:
                b = rt.board()
                im = rnd.board_image(b, layers=args.get("layers"), region=region, highlight=args.get("highlight") or (),
                                     max_px=1600)
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "PNG", optimize=True)
            return buf.getvalue(), im.size
        data, size = await run(make)
        return _img(data, f"{args.get('what')} {args.get('sheet') or ''} {size[0]}x{size[1]} px")

    # ------------------------------------------------------------------ parts, stages, lessons, history
    @reg("parts", "JLC / LCSC lookups (cached with the query date) and the project's data sheet library "
         "(docs/datasheets). action: search (query text: MPN or description; returns LCSC code, package, JLC stock, "
         "Basic/Extended, price) | detail (query: an LCSC code; LCSC stock, parameters, data sheet link) | datasheet "
         "(query: an LCSC code; mpn: its part number) -- save its data sheet into the project, then read the pages you "
         "need | pins (mpn, query: its LCSC code if it has one, pins: {number: name} from the data sheet's pin table, "
         "source: the table and page) -- record the part's pinout as its data sheet gives it; the pinout check trusts "
         "it over the parts library's.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["search", "detail", "datasheet", "pins"]},
                                           "query": {"type": "string"}, "n": {"type": "integer"}, "mpn": {"type": "string"},
                                           "pins": {"type": "object"}, "source": {"type": "string"}}, "required": ["action"]})
    async def parts(args):
        from tw.jlc import Parts, LookupFailed
        from tw import datasheets
        P = Parts(p.root)
        if args["action"] == "pins":
            try:
                f = await run(datasheets.save_pins, p.tw, args.get("pins") or {}, args.get("source") or "", args.get("mpn") or "", args.get("query") or "")
            except ValueError as e:
                return _text(str(e), error=True)
            hub.emit("datasheets")
            return _text(f"saved the pin table: {f}. sch.pinout compares the symbol with it from the next checks run.")
        if args["action"] == "datasheet":
            code = (args.get("query") or "").strip().upper()
            try:
                d = await run(P.detail, code) if code else {}
            except LookupFailed as e:
                return _text(f"could not look {code} up: {e}", error=True)
            url = (d or {}).get("datasheet") or ""
            try:
                f = await run(datasheets.fetch, p.tw, url, args.get("mpn") or "", code)
            except (ValueError, OSError) as e:
                return _text(f"no data sheet saved for {code or args.get('mpn')}: {e}", error=True)
            hub.emit("datasheets")
            return _text(f"saved {f} (read it with the Read tool, pages as needed)")
        if not args.get("query"):
            return _text("query: what to look up", error=True)
        if args["action"] == "search":
            r = await run(P.search, args["query"], 25)
            rows = [f"{it.get('lcsc')}  {it.get('mpn')}  {it.get('brand')}  {it.get('package')}  JLC stock {it.get('jlc_stock')}  "
                    f"{it.get('lib')}  ${it.get('price_1')}  {(it.get('describe') or '')[:90]}" for it in r["items"][:args.get("n", 12)]]
            return _text(f"JLC search '{args['query']}' (queried {r['_utc']}):\n" + ("\n".join(rows) or "no results"))
        r = await run(P.detail, args["query"])
        return _text(_json(r))

    @reg("stage", "Update the stage tracker the user sees. stage: brief | architecture | parts | schematic | board_setup | "
         "placement | routing | verification | release; status: todo | active | done | blocked | skipped; note: one line.",
         {"type": "object", "properties": {"stage": {"type": "string"}, "status": {"type": "string"}, "note": {"type": "string"}},
          "required": ["stage", "status"]})
    async def stage(args):
        from . import gates
        sid, status = args["stage"], args["status"]
        ids = [s["id"] for s in p.stages()]
        finishing = [sid] if status == "done" else []
        if status == "active" and sid in ids:              # moving on finishes the stage that was active before it
            cur = next((s["id"] for s in p.stages() if s["status"] == "active"), None)
            if cur and cur != sid and ids.index(cur) < ids.index(sid):
                finishing.append(cur)
        for f in finishing:
            ok, miss = await run(gates.gate, p, f)
            if not ok:
                title = next(s["title"] for s in p.stages() if s["id"] == f)
                return _text(f"{title} is not done yet: " + "; ".join(miss) + ". Finish it first (or, if it cannot be "
                             "done, say why and mark it blocked); the user can mark it done themselves.", error=True)
        st = p.set_stage(sid, status, args.get("note", ""))
        hub.emit("stages", stages=st)
        return _text("stages: " + ", ".join(f"{s['title']} {s['status']}" for s in st))

    @reg("waive", "Waive a check finding you have reviewed and cannot or should not fix, with the reason (required). "
         "action: propose (key: the finding's key from the checks, reason, title, source) | withdraw (key) | list. title: "
         "the decision in a few plain words, as the user reads it on the Sign-off page (\"J201 and J202 share a land "
         "pattern on purpose\"); source: where it is shown, if anywhere (\"SAM-M10Q integration manual s4.4 p.61\"). A "
         "warning or note waived no longer counts; an error you waive is only a proposal: it keeps counting until the user "
         "approves it on the Sign-off page, so say in the chat why you propose it.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["propose", "withdraw", "list"]},
                                           "key": {"type": "string"}, "reason": {"type": "string"}, "title": {"type": "string"},
                                           "source": {"type": "string"}}, "required": ["action"]})
    async def waive(args):
        from . import signoff
        a = args["action"]
        if a == "list":
            items = await run(signoff.waivers, p)
            return _text("\n".join(f"{w['key']}: {w.get('reason') or '(no reason)'} [{w.get('by') or 'claude'}"
                                    f"{', approved' if w.get('approved') else ''}]" for w in items) or "no waivers")
        key = (args.get("key") or "").strip()
        if not key:
            return _text("key: the finding's key (from run_checks or build/checks.json)", error=True)
        if a == "withdraw":
            w = next((x for x in signoff.waivers(p) if x["key"] == key), None)
            if not w:
                return _text(f"no waiver for {key}", error=True)
            if w.get("by") == "user" or w.get("approved"):
                return _text("the user approved or wrote that waiver: ask them before taking it back", error=True)
            await run(signoff.remove_waiver, p, key)
            hub.emit("waivers")
            return _text(f"withdrawn: {key}")
        reason = (args.get("reason") or "").strip()
        if len(reason) < 8:
            return _text("a waiver needs its reason: why this finding is acceptable on this board", error=True)
        found = None
        try:
            with open(os.path.join(p.tw.build, "checks.json")) as fh:
                res = json.load(fh)
            found = next((f for c in res.get("checks", []) for f in c.get("findings", []) if f.get("key") == key), None)
        except (OSError, ValueError):
            pass
        if not found:
            return _text(f"no finding {key} in the last checks run (run_checks, then use the key it reports)", error=True)
        await run(signoff.set_waiver, p, key, reason, "claude", found.get("severity", ""), found.get("message", ""),
                  args.get("title") or "", args.get("source") or "")
        hub.emit("waivers")
        if found.get("severity") == "error":
            return _text(f"proposed: {key}. It is an error, so it keeps counting until the user approves your waiver on "
                         "the Sign-off page; tell them in the chat why.")
        return _text(f"waived: {key} ({found.get('severity')}); it no longer counts from the next checks run. The user sees it on the Sign-off page.")

    @reg("evidence", "Record what shows a requirement is met, for the Sign-off page (Checks > Sign-off lists every "
         "requirement with its evidence). action: add (requirement: its id from the list, e.g. r3 or lim:max_size_mm, or "
         "its text; kind: check | calc | datasheet | sim | hardware; label: one line the user can read, e.g. \"LDL1117 at "
         "300 mA: 0.6 W, 41 °C rise in SOT-223, under its 125 °C limit\"; ref: a check id, a data sheet and page, a "
         "simulation file, or the bring-up step; status: ok | warn | fail | open) | remove (requirement, label: one entry, "
         "or all of the requirement's without it) | list (the requirements and what they have). hardware means only the "
         "built board can show it (status open; put the step in docs/bring-up.md). Record evidence as you verify, before "
         "the user signs off. The limits' evidence comes from the req.limits check by itself.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "remove", "list"]},
                                           "requirement": {"type": "string"}, "kind": {"type": "string"},
                                           "label": {"type": "string"}, "ref": {"type": "string"}, "status": {"type": "string"}},
          "required": ["action"]})
    async def evidence_t(args):
        from . import signoff
        a = args["action"]
        if a == "list":
            st = await run(signoff.status, p)
            lines = []
            for r in st["requirements"]:
                ev = "; ".join(f"{e.get('kind')}: {e.get('label')} [{e.get('status')}]" for e in r["evidence"]) or "no evidence yet"
                lines.append(f"{r['id']}  {r['text']}{': ' + r['value'] if r['value'] else ''}  -> {ev}")
            return _text("\n".join(lines) or "no requirements found (the guided start's list, docs/requirements.md bullets, or limits)")
        req = (args.get("requirement") or "").strip()
        if not req:
            return _text("requirement: its id (r3, lim:max_size_mm) or its text, from action list", error=True)
        if a == "remove":
            n = await run(signoff.remove_evidence, p, req, args.get("label") or None)
            hub.emit("signoff")
            return _text(f"removed {n} entr{'y' if n == 1 else 'ies'}")
        label = (args.get("label") or "").strip()
        if len(label) < 4:
            return _text("label: one line saying what shows it (a number, a check, a page)", error=True)
        st = await run(signoff.status, p)
        if not any(r["id"] == req or r["text"].lower() == req.lower() or r["text"].lower().startswith(req.lower()) for r in st["requirements"]):
            return _text(f"no requirement {req}; action list shows them", error=True)
        e = await run(signoff.add_evidence, p, req, args.get("kind") or "note", label, args.get("ref") or "", args.get("status") or "ok")
        hub.emit("signoff")
        return _text(f"recorded for {req}: {e['kind']} [{e['status']}] {e['label']}")

    @reg("board_sim", "Simulate the board as laid out (the user sees the same in the Simulate tab). kind: drop (a "
         "supply net's voltage drop and current density from its source to its loads through the real copper; amps, "
         "loads {ref: amps}, source REF.PIN) | heat (the board's temperature from the parts that get warm; sources "
         "[{ref, watts}], ambient, air still|fan, refs: parts whose temperature you want) | return (where a net's return current runs in the plane under it, "
         "the gaps it must detour round and the loop that opens, its layer changes) | signal (a net's impedance along "
         "its route, its edge at the far end in ngspice and the series resistor that tames it, its neighbours' "
         "crosstalk; rise_ns, rs, series) | pdn (a rail's impedance against its target from its capacitors; ripple, "
         "step) | circuit (a block of the schematic in ngspice: refs, sources [{net, kind dc|step|pulse|sine, v, freq}], "
         "loads [{net, r|i}], extra [{kind C|R|L, net, to, value}], analysis {kind tran|ac|op, ...}, probes [nets], name).",
         {"type": "object", "properties": {"kind": {"type": "string", "enum": ["drop", "heat", "return", "signal", "pdn", "circuit"]},
                                           "net": {"type": "string"}, "amps": {"type": "number"}, "loads": {"type": "object"},
                                           "source": {"type": "string"}, "sources": {"type": "array", "items": {"type": "object"}},
                                           "ambient": {"type": "number"}, "air": {"type": "string"},
                                           "rise_ns": {"type": "number"}, "rs": {"type": "number"}, "series": {"type": "number"},
                                           "ripple": {"type": "number"}, "step": {"type": "number"},
                                           "refs": {"type": "array", "items": {"type": "string"}},
                                           "extra": {"type": "array", "items": {"type": "object"}},
                                           "analysis": {"type": "object"}, "probes": {"type": "array", "items": {"type": "string"}},
                                           "name": {"type": "string"}},
          "required": ["kind"]})
    async def board_sim(args):
        from tw.checks.context import Context
        from tw import fields, sigint, blocksim
        tw = proj()
        k = args["kind"]
        if k != "circuit" and not tw.has_pcb():
            return _text("no board yet", error=True)

        def work():
            if k == "circuit":
                info = blocksim.run(tw, args.get("name") or "block", args.get("refs") or [], args.get("sources") or [],
                                    args.get("loads") or [], args.get("extra") or [], args.get("analysis"), args.get("probes") or [])
                return (info.get("lines") or []) + (info.get("notes") or []) + ([info["error"]] if info.get("error") else [])
            ctx = Context(tw, offline=True)
            if k == "drop":
                r = fields.ir_drop(ctx, args.get("net") or "", args.get("amps"), args.get("loads"), args.get("source"))
                return r["lines"] + [f"  {x['ref']}: {x['drop_mv']} mV at {x['amps']} A" for x in r["loads"]]
            if k == "heat":
                src = args.get("sources")
                r = fields.heat(ctx, src if src else None, args.get("ambient"), args.get("air") or "still")
                want = set(args.get("refs") or [])
                parts = [x for x in r["parts"] if x["ref"] in want] + [x for x in r["parts"][:8] if x["ref"] not in want]
                return r["lines"] + (["the board under each part (hottest first):"] +
                                     [f"  {x['ref']}: {x['max_c']} °C" for x in parts] if parts else [])
            if k == "return":
                return sigint.return_paths(ctx, args.get("net") or "")["lines"]
            if k == "signal":
                r = sigint.reflections(ctx, args.get("net") or "", args.get("rise_ns"), args.get("rs") or 25.0, args.get("series"))
                imp = [f"  {p_['layer']} {p_['length']:.1f} mm: {p_['z0']:.0f} Ω ({p_['how']})" for p_ in r["route"]["pieces"]
                       if p_["kind"] == "track" and p_.get("z0")][:8]
                return r["lines"] + ["impedance along the route:"] + imp + sigint.crosstalk(ctx, args.get("net") or "", r["rise_ns"])["lines"][:4]
            return sigint.pdn(ctx, args.get("net") or "", args.get("ripple") or 0.05, args.get("step"))["lines"]
        try:
            lines = await run(work)
        except ValueError as e:
            return _text(str(e), error=True)
        if k == "circuit":
            hub.emit("sims.changed")
        return _text("\n".join(lines))

    @reg("blocks", "The user's block library: circuits built once and wanted again (a USB-C input, a buck, a crystal). "
         "action list | read (id: the parts with values and part numbers, the connections inside, the ports to join to "
         "this design's nets, the design notes, where the parts sat) | save (name, refs, description: these parts of this "
         "design as a block) | remove (id). To use a block: read it, draw its parts and connections into this design's "
         "schematic script with its ports on this design's nets, keep its notes, and place its parts as it says.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "read", "save", "remove"]},
                                           "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                                           "refs": {"type": "array", "items": {"type": "string"}}},
          "required": ["action"]})
    async def blocks_tool(args):
        from . import blocks
        a = args["action"]
        try:
            if a == "list":
                its = blocks.items()
                return _text("\n".join(f"{b['id']}: {b['name']} ({b['parts']} parts; ports {', '.join(b['ports'])})"
                                       + (f" -- {b['description']}" if b.get("description") else "") for b in its) or "the library is empty")
            if a == "read":
                return _text(blocks.text(blocks.get(args.get("id"))))
            if a == "save":
                b = await run(blocks.save, p.reload(), args.get("refs") or [], args.get("name"), args.get("description") or "")
                return _text(f"saved {b['id']}: {len(b['parts'])} parts, ports {', '.join(x['name'] for x in b['ports'])}")
            blocks.remove(args.get("id"))
            return _text("removed")
        except KeyError:
            return _text(f"no block {args.get('id')}", error=True)
        except ValueError as e:
            return _text(str(e), error=True)

    @reg("make", "Get the board built: kind testpoints (the nets a person or fixture needs to touch and where: side F|B) | "
         "drawings (which fab: the fab drawing with outline, stack-up, drills and notes; which assembly: top and bottom "
         "assembly drawings) | panel (nx, ny, rail mm, rails tb|lr|all|none: a V-scored panel with rails, tooling holes, "
         "fiducials, its Gerbers) | enclosure (w, l, h: the box's inside in mm; standoff, gap, lid: will the board and its "
         "parts fit, the wall openings its connectors need, an OpenSCAD enclosure) | bom (the BOM's health).",
         {"type": "object", "properties": {"kind": {"type": "string", "enum": ["testpoints", "drawings", "panel", "enclosure", "bom"]},
                                           "side": {"type": "string"}, "which": {"type": "string"}, "nx": {"type": "integer"},
                                           "ny": {"type": "integer"}, "rail": {"type": "number"}, "rails": {"type": "string"},
                                           "w": {"type": "number"}, "l": {"type": "number"}, "h": {"type": "number"},
                                           "standoff": {"type": "number"}, "gap": {"type": "number"}, "lid": {"type": "number"}},
          "required": ["kind"]})
    async def make_tool(args):
        from tw.checks.context import Context
        tw = proj()
        k = args["kind"]
        if not tw.has_pcb() and k != "bom":
            return _text("no board yet", error=True)

        def work():
            if k == "bom":
                from . import bomhealth
                r = bomhealth.health(tw, rt.board() if tw.has_pcb() else None)
                bad = [x for x in r["rows"] if any(f["sev"] in ("error", "warning") for f in x["flags"])]
                return [f"BOM health {r['score']}"] + r["lines"] + [f"  {', '.join(x['refs'][:4])} {x['value']}: " + "; ".join(f["text"] for f in x["flags"]) for x in bad[:12]]
            if k == "testpoints":
                from tw import testpoints
                return testpoints.plan(Context(tw, offline=True), "F" if args.get("side") == "F" else "B")["lines"]
            if k == "drawings":
                from tw import drawings
                if args.get("which") == "assembly":
                    return [f"wrote {os.path.relpath(f, tw.root)}" for f in drawings.assembly(tw)]
                r = drawings.fab_drawing(tw)
                return [f"wrote {os.path.relpath(r['pdf'], tw.root)}"] + r["notes"]
            if k == "panel":
                from tw import panel
                r = panel.make(tw, args.get("nx", 2), args.get("ny", 2), float(args.get("rail", 5)), args.get("rails", "tb"))
                return r["lines"] + [f"wrote {os.path.relpath(r['zip'], tw.root)}"]
            from tw import enclosure
            box = {key: args[key] for key in ("w", "l", "h") if args.get(key)}
            if len(box) < 3:
                raise ValueError("the box's inside: w, l and h in mm")
            opts = {key: float(args[key]) for key in ("standoff", "gap", "lid") if args.get(key) is not None}
            return enclosure.fit(Context(tw, offline=True), box, **opts)["lines"]
        try:
            lines = await run(work)
        except ValueError as e:
            return _text(str(e), error=True)
        return _text("\n".join(lines))

    @reg("simulate", "Simulate a circuit with KiCad's ngspice to show a requirement holds: a filter's corner, a divider's "
         "or a sense amplifier's output, an RC delay, an LED's current, a regulator's start-up into its load. netlist: a "
         "SPICE netlist with its analysis (.op, .tran, .ac, .dc); probes: vectors to read (v(out), i(v1)); name: short, for "
         "docs/sim/<name>.cir/.json/.svg (kept, re-run with ./tw sim); check: {probe, at?: a time / frequency / sweep "
         "value, min?, max?, corner?: {min?, max?} for an AC probe's -3 dB point} -- the pass criterion; requirement and "
         "label: record the result as that requirement's evidence (kind sim). Use real part values and, for parts, "
         "their data sheet's model or a simple equivalent you say you used.",
         {"type": "object", "properties": {"netlist": {"type": "string"}, "probes": {"type": "array", "items": {"type": "string"}},
                                           "name": {"type": "string"}, "check": {"type": "object"}, "requirement": {"type": "string"},
                                           "label": {"type": "string"}}, "required": ["netlist", "probes", "name"]})
    async def simulate_t(args):
        from tw import sim
        from . import signoff
        import re as _re
        name = _re.sub(r"[^A-Za-z0-9_-]+", "-", args.get("name") or "sim").strip("-")[:40] or "sim"
        probes = [str(x) for x in args.get("probes") or []][:8]
        req = (args.get("requirement") or "").strip()
        proj()                                            # the config as it is on disk now
        if req:
            st = await run(signoff.status, p)
            if not any(r["id"] == req or r["text"].lower() == req.lower() or r["text"].lower().startswith(req.lower()) for r in st["requirements"]):
                return _text(f"no requirement {req}; evidence action list shows them", error=True)
        label = (args.get("label") or "").strip()
        info = await run(sim.simulate, os.path.join(p.root, "docs", "sim"), name, args["netlist"], probes, label or name,
                         args.get("check") or None, req)
        hub.emit("sims")
        if info.get("error"):
            return _text("the simulation failed: " + info["error"] + "\n" + "\n".join(info.get("log") or []), error=True)
        lines = list(info["lines"])
        if req and label:
            await run(signoff.add_evidence, p, req, "sim", label + (f" ({info['check']})" if info.get("check") else ""),
                      f"docs/sim/{name}.cir", info["status"])
            hub.emit("signoff")
            lines.append(f"recorded for {req} (docs/sim/{name}.cir)")
        return _text("\n".join(lines) + f"\nsaved docs/sim/{name}.cir (the netlist), .csv (the waveforms)"
                     + (", .svg (the plot)" if info.get("scale") else "") + "; the user sees it under Docs > Simulations")

    @reg("library", "My parts: parts the user saved from earlier projects because they were checked and right -- symbol, "
         "footprint, 3D model, pin table, the JLC pad map, the user's notes. Look here first when choosing a part. "
         "action: search (q: words of its part number, value, maker or package) | use (id: copies it into this project's own "
         "libraries; returns the symbol and footprint lib ids and the fields to put on the schematic symbol). Saving is the "
         "user's: they do it from a part's card.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["search", "use"]}, "q": {"type": "string"},
                                           "id": {"type": "string"}}, "required": ["action"]})
    async def library_t(args):
        from . import library
        if args["action"] == "search":
            found = await run(library.search, args.get("q") or "")
            if not found:
                return _text("nothing in the user's library matches" + (f" '{args.get('q')}'" if args.get("q") else ""))
            return _text("\n".join(f"{x['id']}: {x['name']} ({x.get('value') or ''}) {x.get('footprint_id') or ''}"
                                   f"{' LCSC ' + x['lcsc'] if x.get('lcsc') else ''}{' -- ' + x['notes'] if x.get('notes') else ''}"
                                   f" [from {x['from']['project']} {x['from']['ref']}, {x['saved']}]" for x in found[:30]))
        try:
            r = await run(library.use, p, args.get("id") or "")
        except KeyError:
            return _text(f"no {args.get('id')} in the library: search first", error=True)
        return _text(_json(r))

    @reg("nets", "The net model: what each net is. The checks, the router's net classes and the user's net list read it, "
         "so declare what names can't say: every supply's voltage and the current it carries, heavy-current lines (an "
         "e-match or motor output: kind signal with its current), each pair's partner and impedance, RF lines' "
         "impedance. action: list (kind?: power | ground | pair | clock | fast | rf | analog | signal) | get (net) | "
         "declare (items: [{net, kind?, voltage? V, current? A, pair?, iface?, impedance? ohm, class?, note?}]; a field "
         "set to null is removed) | classes (apply?: true writes the net classes the declarations call for into the "
         "KiCad project; without it, a preview). Use this rather than editing net classes by hand.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "get", "declare", "classes"]},
                                           "kind": {"type": "string"}, "net": {"type": "string"}, "apply": {"type": "boolean"},
                                           "items": {"type": "array", "items": {"type": "object"}}},
          "required": ["action"]})
    async def nets(args):
        from tw import netmodel
        tw = proj()
        act = args.get("action", "list")
        if act == "declare":
            done, errs = [], []
            for it in args.get("items") or []:
                it = dict(it or {})
                net = str(it.pop("net", "") or "").strip()
                if not net:
                    errs.append("an item without a net")
                    continue
                try:
                    rec = await run(netmodel.declare, tw, net, **{k: v for k, v in it.items() if k in netmodel.FIELDS})
                    done.append(f"{net}: {json.dumps(rec)}")
                except ValueError as e:
                    errs.append(f"{net}: {e}")
            p.reload()
            hub.emit("nets.changed")
            m = await run(netmodel.for_project, proj())
            unused = [k for k, _ in m.unused_keys()]
            txt = "declared:\n" + "\n".join(done) if done else "nothing declared"
            if errs:
                txt += "\nnot declared: " + "; ".join(errs)
            if unused:
                txt += "\nno net has these names (check the spelling): " + ", ".join(unused)
            return _text(txt, error=bool(errs) and not done)
        m = await run(netmodel.for_project, tw)
        if act == "get":
            r = m.get(args.get("net") or "")
            return _text(_json(r) if r else f"no net named {args.get('net')!r}", error=not r)
        if act == "classes":
            from tw.board import Board
            from tw.pro import ProjectSettings
            board = await run(Board.load, tw.pcb) if tw.has_pcb() else None
            classes, assign = await run(netmodel.suggest_classes, m, board, pro=ProjectSettings.load(tw.pro) if tw.pro else None)
            if not classes and not assign:
                return _text("no net needs its own class yet (declare currents, or impedances for pairs and RF lines)")
            lines = [f"{name}: {_json(spec)} <- {', '.join(sorted(n for n, c in assign.items() if c == name))}"
                     for name, spec in classes.items()]
            if args.get("apply"):
                if not tw.pro:
                    return _text("no KiCad project file yet", error=True)
                rt.mark_self(10)
                changed = await run(netmodel.apply_classes, tw.pro, classes, assign)
                hub.emit("rules.changed")
                lines.append("written: " + (", ".join(changed) if changed else "nothing changed"))
            else:
                lines.append("(preview; apply: true writes them)")
            return _text("\n".join(lines))
        kind = args.get("kind")
        rows = []
        for n, r in sorted(m.records.items()):
            if kind and r["kind"] != kind:
                continue
            decl = sorted({v for k, v in r["source"].items() if v != "inferred"})
            rows.append({**{k: v for k, v in r.items() if k != "source"}, "net": netmodel.short(n),
                         "declared": decl or None})
        return _text(m.summary() + "\n" + _json(rows[:400]))

    @reg("agenda", "Your agenda for the user's current request, shown to them as a live checklist above the chat. "
         "Call it before you start any request that takes more than two steps, with 3-8 concrete steps in order; "
         "then call it again with the whole list whenever you start a step (status active), finish one (done, and "
         "note: the result in a few words), or the plan changes (add steps; a dropped one is skipped). One step "
         "active at a time. Write steps in the user's terms ('Route the 5 V supply', not 'call route').",
         {"type": "object", "properties": {
             "title": {"type": "string", "description": "the goal, in a few words"},
             "items": {"type": "array", "items": {"type": "object", "properties": {
                 "text": {"type": "string"}, "status": {"type": "string", "enum": list(AGENDA_STATUS)},
                 "note": {"type": "string"}}, "required": ["text", "status"]}}},
          "required": ["items"]})
    async def agenda(args):
        items = []
        for it in args.get("items") or []:
            text = str((it or {}).get("text") or "").strip()
            if text:
                st = it.get("status") if it.get("status") in AGENDA_STATUS else "todo"
                items.append({"text": text[:160], "status": st, **({"note": str(it["note"])[:240]} if it.get("note") else {})})
        if not items:
            return _text("the agenda needs at least one step", error=True)
        ag = {"title": str(args.get("title") or "").strip()[:90], "items": items, "t": time.time()}
        rt.agenda = ag
        mgr = app.agents.get(p.id)
        sess = mgr.session if mgr else None
        if sess:
            turn = mgr.turn["tid"] if mgr.turn else None
            sess.append({"kind": "agenda", **ag, "turn": turn})
            sess.meta["agenda"] = ag
            mgr._save_meta(sess.meta)
        hub.emit("agent.agenda", sid=sess.sid if sess else None, agenda=ag)
        done = sum(i["status"] in ("done", "skipped") for i in items)
        now = next((i["text"] for i in items if i["status"] == "active"), None)
        return _text(f"agenda: {done}/{len(items)} done" + (f"; now: {now}" if now else ""))

    # ------------------------------------------------------------------ the guided start's canvas
    def _canvas_changed(cv):
        from . import canvas as cvs
        hub.emit("canvas.update", canvas=cvs.enrich(p.root, cv))
        codes = [c for c in cvs.missing_codes(p.root, cv) if c not in (cv.get("pending") or [])]
        if codes:                                 # look the new parts up, then draw them again with stock and price
            cvs.set_pending(p.root, codes, True)

            def look():
                try:
                    from . import bom as bomlib
                    bomlib.lookup(p.tw, codes, budget=60.0)
                finally:
                    cvs.set_pending(p.root, codes, False)
                    hub.emit("canvas.update", canvas=cvs.enrich(p.root, cvs.load(p.root)))
            threading.Thread(target=look, daemon=True).start()

    @reg("canvas", "The live canvas beside the chat during a guided start: what you have worked out so far, drawn for "
         "the user as you go. Replace one section at a time. requirements: {items: [{label, value}]} -- the key "
         "specs (power in, rails and currents, interfaces, size, quantity, how it is built). diagram: {blocks: [{id, "
         "label, kind: power|mcu|sensor|connector|io|rf|memory|display|motor|audio|other, note}], links: [{from, to, "
         "label, kind: power|signal|bus}]} -- the block diagram. connectors: {items: [{name, type, ref, edge: "
         "left|right|top|bottom, pins: [{n, signal}]}], board: {w, h}} -- every connector with its pinout and the "
         "board edge it sits on. floorplan: {board: {w, h, radius}, holes: [{id, ref, x, y, d}], items: [{id, label, "
         "ref, kind, w, h, note, side?: top|bottom, and either edge: left|right|top|bottom with at (mm along that edge: from "
         "the top for left/right, from the left for top/bottom) and w along the edge, h into the board (rot 90 swaps them) "
         "for a connector, or x, y (its centre) for a block}], keepouts: [{label, "
         "x, y, w, h}], note} -- the board to scale before the schematic exists: mm from the top-left corner, y down, "
         "sizes the real footprints' (a block: the area its parts will need). The user can drag anything on it; what "
         "they moved keeps their place (moved) -- ask before changing it. parts: {items: [{role, mpn, lcsc, package, "
         "qty, why}]} -- the key parts (with LCSC codes; the app shows their stock and price). proposal: when the user "
         "asks you to suggest a layout -- {moves: [{id, x, y (a block, hole or keep-out) | edge, at (a connector), rot?, "
         "why: a few words}], replies: [{note: its number, text, outcome: followed | declined}], summary} -- drawn as "
         "ghosts for them to accept all, some or none; reply to every note they gave; locked items cannot move.",
         {"type": "object", "properties": {"section": {"type": "string", "enum": ["requirements", "diagram", "connectors", "floorplan", "parts", "proposal"]},
                                           "data": {"type": "object"}}, "required": ["section", "data"]})
    async def canvas_tool(args):
        from . import canvas as cvs
        if args["section"] == "proposal":
            try:
                cv = await run(cvs.propose, p.root, args.get("data") or {})
            except ValueError as e:
                return _text(str(e), error=True)
            _canvas_changed(cv)
            pr = cv.get("proposal") or {}
            missing = [i + 1 for i in range(len(pr.get("notes") or [])) if not any(r["note"] == i + 1 for r in pr.get("replies") or [])]
            return _text(f"suggested {len(pr.get('moves') or [])} moves for the user to accept" +
                         (f"; reply to note{'s' if len(missing) > 1 else ''} {', '.join(map(str, missing))} too" if missing else ""))
        cv = await run(cvs.update, p.root, args["section"], args.get("data") or {})
        _canvas_changed(cv)
        sec = cv.get(args["section"]) or {}
        n = len(sec.get("items") or sec.get("blocks") or [])
        kept = [o.get("ref") or o.get("label") or o["id"] for o in (sec.get("items") or []) + (sec.get("holes") or [])
                if args["section"] == "floorplan" and o.get("moved")]
        solved = (sec.get("solved") or {}).get("lines") or [] if args["section"] == "floorplan" else []
        from . import feasible
        new = await run(cvs.untold, p.root, await run(feasible.check, p.root, cv))
        said = ("\nChecked against the user's picks -- tell the user now, plainly, with the fix (or change your plan):\n- " +
                "\n- ".join(feasible.lines(new)) if new else "")
        return _text(f"canvas {args['section']} updated ({n} item{'s' if n != 1 else ''})" + said +
                     (f"; kept where the user put them: {', '.join(kept)}" if kept else "") +
                     ("\nThe floorplan solver (not the user) checked it -- blocks inside the board and apart, connectors on "
                      "their edges:\n- " + "\n- ".join(solved) +
                      "\nTell the user about anything that does not fit, and what would." if solved else ""))

    @reg("ready_to_start", "Guided start: the intake is done. Call it once the requirements are settled and written to "
         "docs/requirements.md and the canvas is filled in: summary (two sentences: what you will build) and steps (the "
         "plan, 4-8 steps in the user's terms). The user then sees a Start button; stop and wait -- the design begins "
         "when they press it.",
         {"type": "object", "properties": {"summary": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}},
          "required": ["summary", "steps"]})
    async def ready_to_start(args):
        from . import canvas as cvs
        if not cvs.start_of(p.cfg):
            return _text("this project has no guided start: carry on in the chat", error=True)
        cv = await run(cvs.update, p.root, "plan", {"summary": args.get("summary"), "steps": args.get("steps")})
        await run(p.set_start_phase, "ready")
        _canvas_changed(cv)
        hub.emit("project.start", phase="ready")
        return _text("The Start card is showing. Stop here and wait: the user starts the design with the Start button "
                     "(or keeps talking to change the plan -- then call ready_to_start again).")

    @reg("lessons", "The knowledge base of lessons from real boards. action: search (query) | get (id) | add (title, body, "
         "tags: record a trap you just hit so every future project knows) | list.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["search", "get", "add", "list"]},
                                           "query": {"type": "string"}, "id": {"type": "string"}, "title": {"type": "string"},
                                           "body": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
          "required": ["action"]})
    async def lessons(args):
        a = args["action"]
        if a == "list":
            return _text(knowledge.index_text())
        if a == "get":
            l = knowledge.get(args.get("id", ""))
            return _text(f"# {l['title']}\n{l['body']}")
        if a == "search":
            ls = knowledge.search(args.get("query", ""))
            return _text("\n\n".join(f"## {l['id']}: {l['title']}\n{l['body'][:1500]}" for l in ls) or "nothing found")
        l = knowledge.add(args.get("title", "untitled"), args.get("body", ""), args.get("tags") or [],
                          source=f"{p.name}, {time.strftime('%Y-%m-%d')}")
        app.hubs_emit_all("lessons.changed", id=l["id"])
        return _text(f"recorded lesson {l['id']}")

    @reg("snapshot", "Checkpoint the project in its history (git), e.g. before a large change. message: what state this is.",
         {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]})
    async def snapshot(args):
        h = await run(history.snapshot, p.root, args["message"])
        hub.emit("history.snapshot", hash=h, message=args["message"])
        return _text(f"checkpoint {h}" if h else "nothing changed since the last checkpoint")

    @reg("kicad", "The KiCad live link. action: status | open (open the project in KiCad) | save (save KiCad's copy of the "
         "board and schematic) | reload (make KiCad re-read the files after you edited them) | selection.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "open", "save", "reload", "selection"]}},
          "required": ["action"]})
    async def kicad_t(args):
        a = args["action"]
        tw = proj()
        if a == "status":
            return _text(_json(await run(twlive.status)))
        if a == "open":
            await run(app.open_in_kicad, p, "project")
            return _text("asked KiCad to open the project (the user may need to enable the API: Preferences > Plugins)")
        if a == "selection":
            return _text(_json(rt.selection))
        link = await run(twlive.link_for, tw.pcb)
        sch = await run(twlive.sch_link_for, tw.hw)
        done = []
        for l, name in ((link, "board"), (sch, "schematic")):
            if l is None:
                continue
            try:
                await run(getattr(l, a))
                done.append(name)
            except Exception as e:
                done.append(f"{name} failed: {e}")
        return _text(f"{a}: " + (", ".join(done) if done else "nothing open in KiCad (or the API is off)"))

    @reg("outputs", "Generate the fab and documentation outputs and the release zip (Gerbers, drill, BOM, CPL fitted to "
         "JLC's footprints, PDFs, STEP, renders). renders: include the 3D renders (slower).",
         {"type": "object", "properties": {"renders": {"type": "boolean"}}})
    async def outputs(args):
        from tw.outputs import Outputs
        tw = proj()
        rt.mark_self(600)
        try:
            o = Outputs(tw)
            z = await run(o.all, bool(args.get("renders", True)))
        finally:
            rt.mark_self(4)
        hub.emit("outputs.done", release=os.path.relpath(z, p.root))
        return _text("\n".join(o.log) + f"\nrelease: {os.path.relpath(z, p.root)}")

    return tools


class LiveMirror:
    """Mirrors the grid router's progress into the open KiCad board: each routed net appears as it is
    found (one undo step per net), ripped nets disappear, the pours are refilled at the end."""

    def __init__(self, link):
        self.link = link
        self.items = {}          # net -> created items

    def on(self, ev):
        st, net = ev.get("status"), ev.get("net")
        try:
            if st in ("routed", "escape", "stitch") and (ev.get("tracks") or ev.get("vias")):
                objs = [self.link._track(t) for t in ev.get("tracks", [])] + [self.link._via(v) for v in ev.get("vias", [])]
                c = self.link.b.begin_commit()
                created = self.link.b.create_items(objs)
                self.link.b.push_commit(c, f"Tracewright: route {net.rsplit('/', 1)[-1]}")
                self.items.setdefault(net, []).extend(created)
            elif st == "ripped":
                self._remove(net)
        except Exception:
            traceback.print_exc()

    def _remove(self, net):
        its = self.items.pop(net, [])
        if its:
            c = self.link.b.begin_commit()
            self.link.b.remove_items(its)
            self.link.b.push_commit(c, f"Tracewright: rip up {net.rsplit('/', 1)[-1]}")

    def finish(self):
        try:
            self.link.refill()
        except Exception:
            pass
        self.link.save()
