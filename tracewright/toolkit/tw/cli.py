#!/usr/bin/env python3
"""Tracewright toolkit command line. In a project folder: ./tw <command> (or python3 tools/tw/cli.py).

  status                     what the project contains, board summary, last check verdict
  check [ids...]             run the design checks (build/checks.json, checks_report.md, readiness.md)
        --list  --refresh  --offline  --json
  selftest                   plant a fault for every check and make sure it is caught
  erc | drc                  KiCad's checks alone (JSON in build/)
  netlist                    export build/<name>.net
  svg                        plot the schematic sheets to build/sch_svg/
  render [--sheet S] [--board] [--region x0,y0,x1,y1] [-o out.png]
                             PNG of a schematic sheet or the board, for a quick look
  outputs [--no-renders]     fab + docs outputs and the release zip
  parts search TEXT... | parts code C123...     JLC / LCSC lookups (cached in sourcing/cache)
  cpl [--write]              CPL against JLC's footprints; --write stores the corrections
  sync                       update the board from the schematic (keeps placement and routing)
  place FILE.json            apply a placement [{ref, x, y, rot, side}] to the board
  route [--nets N...] [--engine grid|freerouting] [--clear]
  fill                       refill copper zones
  silk [--dry-run]           move silk reference designators off pads, other silk and the edge
  pcb OPS.json               apply a list of board operations (see tw/pcb/ops.py)
  style [flat|hierarchical] [--dry-run]
                             how the sheets are joined; redraw them the other way (sheet pins and
                             hierarchical labels, or global labels), checked against KiCad's netlist
"""
import os, sys, json, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tw import env, kicad, __version__            # noqa: E402


def cmd_status(a):
    p = env.project()
    k = env.kicad()
    print(f"project   {p.name}  ({p.root})")
    print(f"kicad     {k['version'] or 'not found'}  cli={k['cli']}")
    print(f"schematic {os.path.relpath(p.sch, p.root) if p.has_sch() else '-'}")
    print(f"board     {os.path.relpath(p.pcb, p.root) if p.has_pcb() else '-'}")
    if p.has_pcb():
        from tw.board import Board
        s = Board.load(p.pcb).summary()
        print("          " + ", ".join(f"{k}={v}" for k, v in s.items() if k != "layers"))
    f = os.path.join(p.build, "checks.json")
    if os.path.exists(f):
        from tw.checks.runner import verdict
        with open(f) as fh:
            res = json.load(fh)
        print(f"checks    {verdict(res)} ({res['counts']['error']} errors, {res['counts']['warning']} warnings) at {res['generated']}")


def cmd_check(a):
    from tw.checks import load_all, REGISTRY
    from tw.checks.runner import run_all, summary_text, load_project_checks
    p = env.project()
    if a.list:
        load_all()
        load_project_checks(p.root)
        for c in REGISTRY:
            print(f"{c.id:<24} {c.group:<14} {c.title}")
        return 0
    prog = None
    if not a.json:
        def prog(c, r):
            if r["status"] == "running":
                return
            extra = f"  ({r.get('reason')})" if r["status"] == "skipped" and r.get("reason") else \
                (f"  {r['scope']}" if r.get("scope") else "")
            print(f"  {'n/a' if r.get('na') else r['status']:<7} {c.id:<24} {len(r['findings']):>3}  "
                  f"{r.get('seconds', 0):5.1f}s{extra}", flush=True)
    res = run_all(p, only=a.ids or None, refresh=a.refresh, offline=a.offline or None, progress=prog)
    if a.json:
        print(json.dumps(res))
    else:
        print(summary_text(res))
    return 1 if res["counts"]["error"] or any(c["status"] == "error" for c in res["checks"]) else 0


def cmd_selftest(a):
    from tw.checks import selftest
    return selftest.main(verbose=a.verbose, only=a.ids or None)


def cmd_erc(a):
    p = env.project()
    d = kicad.erc(p.sch, os.path.join(p.build, "erc.json"))
    n = sum(len(s.get("violations", [])) for s in d.get("sheets", []))
    print(f"ERC: {n} violations -> build/erc.json")
    return 1 if n else 0


def cmd_drc(a):
    p = env.project()
    d = kicad.drc(p.pcb, os.path.join(p.build, "drc.json"), parity=p.has_sch())
    print(f"DRC: {len(d.get('violations', []))} violations, {len(d.get('unconnected_items', []))} unrouted, "
          f"{len(d.get('schematic_parity', []))} parity -> build/drc.json")
    return 1 if d.get("violations") or d.get("unconnected_items") else 0


def cmd_netlist(a):
    p = env.project()
    print(kicad.netlist(p.sch, os.path.join(p.build, f"{p.stem}.net")))


def cmd_svg(a):
    p = env.project()
    files = kicad.sch_svg(p.sch, os.path.join(p.build, "sch_svg"), drawing_sheet=False, theme="_builtin_default")
    for f in files:
        print(os.path.relpath(f, p.root))


def cmd_render(a):
    from tw import render
    p = env.project()
    region = tuple(float(v) for v in a.region.split(",")) if a.region else None
    if a.board or not a.sheet and p.has_pcb() and not p.has_sch():
        out = render.board_png(p, a.output or p.out("render", "board.png"), region=region, layers=a.layers,
                               px_per_mm=a.scale or None)
    else:
        out = render.sheet_png(p, a.sheet or "/", a.output or p.out("render", "sheet.png"), region=region,
                               px_per_mm=a.scale or None)
    print(out)


def cmd_outputs(a):
    from tw.outputs import Outputs
    Outputs().all(renders=not a.no_renders)


def cmd_parts(a):
    from tw.jlc import Parts
    p = env.project()
    parts = Parts(p.root)
    if a.mode == "search":
        for t in a.terms:
            r = parts.search(t, max(a.n, 10), a.refresh)
            print(f"## JLC search '{t}'  (queried {r['_utc']})")
            for it in r["items"][:a.n]:
                print(f"{it.get('lcsc') or '':>10}  {str(it.get('mpn'))[:28]:28}  {str(it.get('brand'))[:16]:16}  "
                      f"{str(it.get('package'))[:14]:14}  jlc={it.get('jlc_stock')!s:>7}  {str(it.get('lib')):8}  "
                      f"${it.get('price_1')!s:7}  {(it.get('describe') or '')[:60]}")
    else:
        for t in a.terms:
            r = parts.detail(t, a.refresh)
            print(f"{r['lcsc']}  {r['mpn']}  {r['brand']}  {r['package']}  lcsc_stock={r['lcsc_stock']}  ${r['price_1']}  "
                  f"(queried {r['_utc']})")
            for k, v in list((r.get("params") or {}).items())[:12]:
                print(f"      {k}: {v}")


def cmd_cpl(a):
    from tw.outputs import Outputs
    from tw.jlc import Parts, fit_placements, write_corrections
    from tw.board import Board
    p = env.project()
    o = Outputs(p)
    parts = o.parts(o.netlist())
    rows, _ = o.cpl_rows(parts, corrections=not a.write)
    b = Board.load(p.pcb)
    res, fitted, _ = fit_placements(Parts(p.root), b, {r["Designator"]: r for r in rows},
                                    {x["ref"]: x["lcsc"] for x in parts}, p.setting("fab.cpl_pad_map", {}), a.refresh)
    bad = [r for r in res if r[-1] != "ok"]
    for r in bad:
        print("  " + " | ".join(r))
    print(f"{len(res) - len(bad)} of {len(res)} placements agree with JLC's footprints")
    if a.write:
        out = write_corrections(fitted, b, p.path("sourcing", "jlc-placement.json"))
        print(f"corrections for {len(out)} LCSC codes -> sourcing/jlc-placement.json (re-run ./tw outputs, then ./tw cpl)")
    return 1 if bad and not a.write else 0


def cmd_pcb(a):
    from tw.pcb import client
    with open(a.file) as f:
        ops = json.load(f)
    res = client.apply(env.project(), ops if isinstance(ops, list) else ops.get("ops", []))
    print(json.dumps(res, indent=1))


def cmd_place(a):
    from tw.pcb import client
    with open(a.file) as f:
        moves = json.load(f)
    res = client.apply(env.project(), [dict(op="move", **m) for m in moves])
    print(json.dumps(res, indent=1))


def cmd_sync(a):
    from tw.pcb import client
    res = client.sync(env.project())
    print(json.dumps(res, indent=1))


def cmd_fill(a):
    from tw.pcb import client
    print(json.dumps(client.apply(env.project(), [{"op": "fill"}]), indent=1))


def cmd_silk(a):
    from tw.pcb import client
    from tw.board import Board
    from tw import silk
    p = env.project()
    ops, rep = silk.tidy(Board.load(p.pcb))
    print(f"references: {len(rep['kept'])} clear, {len(rep['moved'])} moved, {len(rep['stuck'])} with no clear spot"
          + (f" ({', '.join(rep['stuck'])})" if rep["stuck"] else ""))
    if ops and not a.dry_run:
        r = client.apply(p, ops)
        print("applied" if r.get("ok") else f"failed: {r}")


def cmd_route(a):
    from tw.route import driver
    res = driver.route(env.project(), nets=a.nets or None, engine=a.engine, clear=a.clear, apply=not a.dry_run)
    print(json.dumps(res.get("summary", res), indent=1))


def cmd_style(a):
    from tw.sch import style
    p = env.project()
    if not p.has_sch():
        print("no schematic yet")
        return 1
    want = (p.cfg.get("schematic") or {}).get("style")
    if not a.style:
        d = style.detect(p.sch)
        print(f"style     {d['style']}" + (f"  (project setting: {want})" if want else ""))
        print(f"sheets    {d['sheets']}: {d['sheet_pins']} sheet pins, {d['hierarchical_labels']} hierarchical labels, "
              f"{d['global_labels']} global labels")
        if d["crossing"]:
            print("across sheets by global label: " + ", ".join(d["crossing"][:16]) + (" ..." if len(d["crossing"]) > 16 else ""))
        if d["reused"]:
            print("sheets used more than once: " + ", ".join(d["reused"]))
        return 0
    r = style.convert(p.sch, a.style, write=not a.dry_run)
    print(r["message"])
    rep = r.get("report") or {}
    for k, v in rep.items():
        if v and k not in ("stale_notes",):
            print(f"  {k.replace('_', ' ')}: {v if not isinstance(v, (list, dict)) else json.dumps(v)[:300]}")
    for n in rep.get("stale_notes") or []:
        print(f"  note that may be out of date: {n}")
    if r["ok"] and not a.dry_run:
        p.cfg.setdefault("schematic", {})["style"] = a.style
        p.save_cfg()
        if r["changed"]:
            kicad.netlist(p.sch, os.path.join(p.build, f"{p.stem}.net"))
    return 0 if r["ok"] else 1


def cmd_firmware(a):
    """A firmware starter from the schematic: firmware/pins.h, PINS.md and a bring-up sketch."""
    from tw import firmware
    from tw.checks.context import Context
    p = env.project()
    if not p.has_sch():
        print("no schematic yet")
        return 1
    out = firmware.write(p, Context(p, offline=True).netlist, p.name)
    if not out:
        print("no microcontroller in the schematic (a part with port-named pins, or a known family)")
        return 1
    print("wrote " + ", ".join(out))
    return 0


def cmd_floorplan(a):
    """The guided start's floorplan: print it in board coordinates, or put it on the board."""
    from tw import floorplan
    from tw.board import Board
    p = env.project()
    fp = floorplan.load(p)
    if not fp:
        print("no floorplan (it comes from a guided start's canvas)")
        return 1
    b = Board.load(p.pcb) if p.has_pcb() else None
    origin = floorplan.origin_for(b)
    if a.action == "show":
        print("\n".join(floorplan.describe(fp, origin)))
        return 0
    if b is None:
        print("no board yet: sync the board from the schematic first")
        return 1
    ops = floorplan.ops(fp, b, origin, outline=True if a.outline else None)
    from tw.pcb import client
    res = client.apply(p, ops)
    moved = [o["ref"] for o in ops if o["op"] == "move"]
    print(("applied: " if res.get("ok") else "FAILED: ") + ", ".join(
        [x for x in ("outline" if any(o["op"] == "outline" for o in ops) else "", "areas on Dwgs.User" if any(o["op"] == "floorplan" for o in ops) else "",
                     f"moved {', '.join(moved)}" if moved else "") if x]) + f" ({res.get('via')})")
    if not res.get("ok"):
        print(json.dumps(res.get("results"), indent=1))
        return 1
    print("Turn each connector so its opening faces off its edge; place the rest of each block inside its area.")
    return 0


def cmd_nets(a):
    """The net model: list it, declare a net's facts, or size net classes from them."""
    from tw import netmodel
    p = env.project()
    if a.action == "set":
        if not a.args:
            print("usage: ./tw nets set NET kind=power voltage=3.3 current=0.5 [pair=... impedance=... class=... note=...]")
            return 1
        net, fields = a.args[0], {}
        for kv in a.args[1:]:
            if "=" not in kv:
                print(f"not a field=value: {kv}")
                return 1
            k, v = kv.split("=", 1)
            fields[k.strip()] = v.strip()
        try:
            rec = netmodel.declare(p, net, **fields)
        except ValueError as e:
            print(f"error: {e}")
            return 1
        print(f"{net}: {json.dumps(rec) if rec else '(no declarations)'}")
        return 0
    m = netmodel.for_project(p)
    if a.action == "classes":
        from tw.board import Board
        board = Board.load(p.pcb) if p.has_pcb() else None
        from tw.pro import ProjectSettings
        classes, assign = netmodel.suggest_classes(m, board, pro=ProjectSettings.load(p.pro) if p.pro else None)
        if not classes and not assign:
            print("no net needs its own class: declare currents (supplies) or impedances (pairs, RF) first")
            return 0
        for name, spec in classes.items():
            nets = sorted(n for n, c in assign.items() if c == name)
            dp = f", pair {spec['diff_pair_width']} / gap {spec['diff_pair_gap']} mm" if "diff_pair_width" in spec else ""
            print(f"{name}: track {spec['track_width']} mm, clearance {spec['clearance']} mm{dp}  <- {', '.join(nets)}")
            if spec.get("_note"):
                print(f"    note: {spec['_note']}")
        if a.apply:
            if not p.pro:
                print("no .kicad_pro yet")
                return 1
            changed = netmodel.apply_classes(p.pro, classes, assign)
            print(f"written to {os.path.relpath(p.pro, p.root)}: " + (", ".join(changed) if changed else "nothing changed"))
        else:
            print("(preview: add --apply to write them into the KiCad project)")
        return 0
    if a.json:
        print(json.dumps(m.to_json(), indent=1))
        return 0
    print(m.summary())
    order = {k: i for i, k in enumerate(netmodel.KINDS)}
    for n, r in sorted(m.records.items(), key=lambda t: (order.get(t[1]["kind"], 99), t[0])):
        bits = []
        if r.get("voltage") is not None:
            bits.append(f"{r['voltage']:g} V")
        if r.get("current") is not None:
            bits.append(f"{r['current']:g} A")
        if r.get("pair"):
            bits.append(f"pair {r['pair']}")
        if r.get("impedance"):
            bits.append(f"{r['impedance']:g} ohm")
        if r.get("iface"):
            bits.append(r["iface"])
        if r.get("class"):
            bits.append(f"class {r['class']}")
        decl = sorted({v for k, v in r["source"].items() if v != "inferred"})
        print(f"  {netmodel.short(n):<22} {r['kind']:<8} {', '.join(bits):<34} {'declared: ' + ', '.join(decl) if decl else 'inferred'}")
    unused = m.unused_keys()
    if unused:
        print("declared but matching no net: " + ", ".join(k for k, _ in unused))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tw", description=f"Tracewright toolkit {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    c = sub.add_parser("check")
    c.add_argument("ids", nargs="*")
    c.add_argument("--list", action="store_true")
    c.add_argument("--refresh", action="store_true")
    c.add_argument("--offline", action="store_true")
    c.add_argument("--json", action="store_true")
    s = sub.add_parser("selftest")
    s.add_argument("ids", nargs="*")
    s.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("erc")
    sub.add_parser("drc")
    sub.add_parser("netlist")
    sub.add_parser("svg")
    r = sub.add_parser("render")
    r.add_argument("--sheet")
    r.add_argument("--board", action="store_true")
    r.add_argument("--region")
    r.add_argument("--layers")
    r.add_argument("--scale", type=float)
    r.add_argument("-o", "--output")
    o = sub.add_parser("outputs")
    o.add_argument("--no-renders", action="store_true")
    pp = sub.add_parser("parts")
    pp.add_argument("mode", choices=["search", "code"])
    pp.add_argument("terms", nargs="+")
    pp.add_argument("-n", type=int, default=12)
    pp.add_argument("--refresh", action="store_true")
    cp = sub.add_parser("cpl")
    cp.add_argument("--write", action="store_true")
    cp.add_argument("--refresh", action="store_true")
    pc = sub.add_parser("pcb")
    pc.add_argument("file")
    pl = sub.add_parser("place")
    pl.add_argument("file")
    sub.add_parser("sync")
    sub.add_parser("fill")
    sk = sub.add_parser("silk")
    sk.add_argument("--dry-run", action="store_true")
    rt = sub.add_parser("route")
    rt.add_argument("--nets", nargs="*")
    rt.add_argument("--engine", default="grid", choices=["grid", "freerouting"])
    rt.add_argument("--clear", action="store_true")
    rt.add_argument("--dry-run", action="store_true")
    nt = sub.add_parser("nets", help="the net model: list, set NET field=value ..., classes [--apply]")
    nt.add_argument("action", nargs="?", default="list", choices=["list", "set", "classes"])
    nt.add_argument("args", nargs="*")
    nt.add_argument("--apply", action="store_true")
    nt.add_argument("--json", action="store_true")
    sub.add_parser("firmware", help="a firmware starter: firmware/pins.h (nets to pins), PINS.md, a bring-up sketch")
    fpp = sub.add_parser("floorplan", help="the guided start's floorplan: show (board coordinates) | apply (outline, areas, connectors, holes)")
    fpp.add_argument("action", nargs="?", default="show", choices=["show", "apply"])
    fpp.add_argument("--outline", action="store_true", help="replace the board's outline with the floorplan's")
    st = sub.add_parser("style")
    st.add_argument("style", nargs="?", choices=["flat", "hierarchical"])
    st.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    fn = globals()["cmd_" + a.cmd.replace("-", "_")]
    try:
        return fn(a) or 0
    except kicad.KiCadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
