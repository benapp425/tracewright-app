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
    @reg("board", "Query the board. what: summary | footprints (ref, value, footprint, x, y, rot, side) | footprint (one "
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

    @reg("review", "The user's review flags: issues they marked on the board, schematic or 3D view, or picked from the "
         "checks. action: list (status: active (default: open or sent to you) | open | sent | fixed | wontfix | all) | "
         "resolve (id, status: fixed | wontfix, note: one line on what you changed, or why it should not be done) | add "
         "(text, view: board | schematic, x, y, sheet, refs: flag something for the user to look at). Resolve every flag "
         "you were sent once you have dealt with it.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "resolve", "add"]},
                                           "id": {"type": "string"}, "status": {"type": "string"}, "note": {"type": "string"},
                                           "text": {"type": "string"}, "view": {"type": "string"}, "x": {"type": "number"},
                                           "y": {"type": "number"}, "sheet": {"type": "string"},
                                           "refs": {"type": "array", "items": {"type": "string"}}},
          "required": ["action"]})
    async def review_t(args):
        from .review import Review
        r = Review(p)
        a = args["action"]
        if a == "list":
            fl = r.flags(args.get("status") or "active")
            return _text("\n\n".join(Review.describe(f) + f"\n  status: {f['status']}" + (f"; {f['resolution']}" if f.get("resolution") else "")
                                     for f in fl) or "no flags with that status")
        if a == "resolve":
            status = args.get("status") or "fixed"
            try:
                f = r.resolve(args.get("id", ""), status, args.get("note", ""))
            except KeyError:
                return _text(f"no flag {args.get('id')}: list them first", error=True)
            except ValueError as e:
                return _text(str(e), error=True)
            hub.emit("review.changed", flags=r.flags())
            left = len(r.flags("active"))
            return _text(f"{f['id']} marked {status}" + (f"; {left} flag(s) still open or sent" if left else "; no flags left"))
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

    @reg("place", "Move footprints: moves [{ref, x, y, rot?, side?: F|B, locked?}] (mm, KiCad axes, rot CCW degrees). "
         "Shown animated in the app and, with the board open in KiCad, applied live as one undo step. Place a "
         "functional group at a time. Returns overlaps and off-board parts among the moved ones.",
         {"type": "object", "properties": {"moves": {"type": "array", "items": {"type": "object"}}}, "required": ["moves"]})
    async def place(args):
        moves = args.get("moves") or []
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
        return _text(f"placed {len(ops)} parts ({res.get('via')})" + ("; " + "; ".join(sorted(set(notes))) if notes else
                                                                      "; no courtyard overlaps among them"))

    @reg("copper", "Board operations (see .claude/tracewright.md): ops [{op: track|tracks|via|vias|delete|zone|rule_area|"
         "outline|text|fill|lock|value|ref_text, ...}]. Tracks, vias, deletes go live into KiCad when it has the board "
         "open; the others save KiCad's copy, edit the file and reload it.",
         {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}}}, "required": ["ops"]})
    async def copper(args):
        ops = args.get("ops") or []
        res = await _apply(ops, f"{len(ops)} board operations")
        bad = [r for r in res.get("results", []) if not r.get("ok")]
        return _text(f"{'done' if res.get('ok') else 'FAILED'} via {res.get('via')}: " +
                     ("; ".join(f"{r['op']}: {r.get('error')}" for r in bad) if bad else f"{len(ops)} ops applied"),
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

    @reg("route", "Route nets with the grid router (human style: 0/45/90, few vias, supplies first; streamed live to the "
         "app and into KiCad when the board is open). nets: names (default: every unrouted net); clear: tear up those "
         "nets first; engine: grid | freerouting (the whole board only, replacing what is routed; no nets).",
         {"type": "object", "properties": {"nets": {"type": "array", "items": {"type": "string"}}, "clear": {"type": "boolean"},
                                           "engine": {"type": "string", "enum": ["grid", "freerouting"]}}})
    async def route(args):
        from tw.route import driver
        tw = proj()
        nets = args.get("nets") or None
        engine = args.get("engine", "grid")
        if engine == "freerouting" and nets:
            return _text("Freerouting routes the whole board and replaces what is already routed; it cannot route "
                         "chosen nets. Route these with the grid router (engine grid), or call freerouting without nets "
                         "for a whole-board run.", error=True)
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
            if engine == "freerouting":
                res = await run(driver.route, tw, nets=None, engine="freerouting", on_progress=progress, live=False)
            elif mirror:
                g = await run(lambda: driver.GridRoute(tw, nets=nets, clear=bool(args.get("clear")), on_progress=progress,
                                                       log=lambda m: hub.emit("route.log", text=m)).setup())
                if args.get("clear"):
                    await run(link.apply, [{"op": "delete", "nets": nets or [], "all": not nets, "kinds": ["track", "via"]}])
                summary, segs, vias = await run(g.run)
                await run(mirror.finish)
                # what the file path does after routing: neck-down areas + their DRC rule, pour islands
                if g.necks:
                    from tw.pcb import rules
                    rules.ensure_rules(tw, dict([rules.neck_rule(driver.NECK_CL)]))
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
                res = {"summary": summary, "apply": {"via": "live", "ok": True}}
            else:
                res = await run(driver.route, tw, nets=nets, clear=bool(args.get("clear")), on_progress=progress, live=False,
                                log=lambda m: hub.emit("route.log", text=m))
        finally:
            rt.mark_self(4)
        hub.emit("board.changed", version=-1, source="tracewright")
        s = res.get("summary", {})
        failed = s.get("failed") or {}
        txt = (f"{engine}: routed {s.get('routed', '?')} of {s.get('nets', '?')} nets, {s.get('tracks', '?')} segments, "
               f"{s.get('vias', '?')} vias, {s.get('length_mm', '?')} mm in {s.get('seconds', '?')} s"
               + (f"; stitching vias {s.get('stitching_vias')}" if s.get("stitching_vias") else "")
               + (f"\nFAILED: " + "; ".join(f"{n.rsplit('/', 1)[-1]} at {w}" for n, w in failed.items()) if failed else ""))
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
    @reg("parts", "JLC / LCSC lookups (cached with the query date). action: search (query text: MPN or description; "
         "returns LCSC code, package, JLC stock, Basic/Extended, price) | detail (query: an LCSC code; LCSC stock, "
         "parameters, data sheet link).",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ["search", "detail"]}, "query": {"type": "string"},
                                           "n": {"type": "integer"}}, "required": ["action", "query"]})
    async def parts(args):
        from tw.jlc import Parts
        P = Parts(p.root)
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
        st = p.set_stage(args["stage"], args["status"], args.get("note", ""))
        hub.emit("stages", stages=st)
        return _text("stages: " + ", ".join(f"{s['title']} {s['status']}" for s in st))

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
         "board edge it sits on. parts: {items: [{role, mpn, lcsc, package, qty, why}]} -- the key parts (with LCSC "
         "codes; the app shows their stock and price).",
         {"type": "object", "properties": {"section": {"type": "string", "enum": ["requirements", "diagram", "connectors", "parts"]},
                                           "data": {"type": "object"}}, "required": ["section", "data"]})
    async def canvas_tool(args):
        from . import canvas as cvs
        cv = await run(cvs.update, p.root, args["section"], args.get("data") or {})
        _canvas_changed(cv)
        sec = cv.get(args["section"]) or {}
        n = len(sec.get("items") or sec.get("blocks") or [])
        return _text(f"canvas {args['section']} updated ({n} item{'s' if n != 1 else ''})")

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
