#!/usr/bin/env python3
"""Board operations, run under KiCad's Python (pcbnew). Reads a JSON request on stdin:

  {"board": "x.kicad_pcb", "ops": [...], "save": true}

and prints "TWJSON {...}" with one result per op and a change list the app uses to animate moves.

Ops (coordinates in board mm, KiCad axes: y down, angles CCW in degrees):
  {"op": "move", "ref": "U1", "x": 10, "y": 20, "rot": 90, "side": "F"|"B", "locked": false}
  {"op": "track", "net": "GND", "layer": "F.Cu", "a": [x, y], "b": [x, y], "w": 0.25}
  {"op": "tracks", "items": [{net, layer, a, b, w}, ...]}
  {"op": "via", "net": "GND", "x": 1, "y": 2, "d": 0.6, "drill": 0.3}
  {"op": "delete", "nets": [...], "uuids": [...], "region": [x0, y0, x1, y1], "kinds": ["track", "via"], "all": false}
  {"op": "track_set", "uuid": "...", "a": [x, y], "b": [x, y], "w": 0.3, "layer": "B.Cu", "net": "GND"}
  {"op": "via_set", "uuid": "...", "x": 1, "y": 2, "d": 0.6, "drill": 0.3}
  {"op": "zone_set", "uuid": "...", "polygon": [[x, y], ...], "net": "GND", "priority": 1, "layers": [...]}
  {"op": "zone", "net": "GND", "layers": ["B.Cu"], "polygon": [[x, y], ...], "name": "", "priority": 0,
                 "clearance": 0.25, "min_width": 0.25, "connect": "thermal"|"solid"|"tht", "remove_islands": true}
  {"op": "rule_area", "name": "keepout", "layers": ["F.Cu"], "polygon": [...], "no_tracks": true, "no_vias": true,
                      "no_pour": true, "no_footprints": false, "add": false}   (add: keep others of the same name)
  {"op": "outline", "rect": [x0, y0, x1, y1], "radius": 1.0}  |  {"op": "outline", "polygon": [[x, y], ...]}
  {"op": "text", "text": "REV A", "x": 1, "y": 2, "layer": "F.SilkS", "size": 1.0, "thickness": 0.15, "rot": 0}
  {"op": "floorplan", "rects": [{"rect": [x0, y0, x1, y1], "label": "MCU"}, ...], "layer": "Dwgs.User"}
      the guided start's plan, drawn as one group named "Floorplan" (drawn again each time)
  {"op": "value", "ref": "R1", "value": "10k"}
  {"op": "lock", "refs": [...], "locked": true}
  {"op": "layers", "copper": 4}
  {"op": "fill"}
"""
import sys, os, json, math, traceback
import pcbnew

MM = pcbnew.FromMM
TO = pcbnew.ToMM


def P(x, y):
    return pcbnew.VECTOR2I(MM(float(x)), MM(float(y)))


def layer_id(b, name):
    lid = b.GetLayerID(name)
    if lid < 0:
        alias = {"F.SilkS": "F.Silkscreen", "B.SilkS": "B.Silkscreen", "F.Silkscreen": "F.SilkS", "B.Silkscreen": "B.SilkS"}
        if name in alias:
            lid = b.GetLayerID(alias[name])
    if lid < 0:
        raise ValueError(f"unknown layer {name}")
    return lid


def net(b, name):
    if not name:
        return None
    n = b.FindNet(name)
    if n is None:
        # accept short names: '/Power/VIN' <- 'VIN'
        for code, ni in b.GetNetInfo().NetsByNetcode().items():
            nm = ni.GetNetname()
            if nm.rsplit("/", 1)[-1] == name:
                return ni
        raise ValueError(f"net {name} is not on the board")
    return n


def fp_state(fp):
    x, y = TO(fp.GetPosition().x), TO(fp.GetPosition().y)
    return {"x": round(x, 4), "y": round(y, 4), "rot": round(fp.GetOrientationDegrees(), 3),
            "side": "B" if fp.IsFlipped() else "F"}


# Items removed from the board are kept referenced until the process ends: destroying the Python
# wrapper of a removed item corrupts SWIG's type table, after which getters return raw SwigPyObjects.
# main() leaves with os._exit so no destructor runs at interpreter shutdown either.
GRAVE = []
FPS = {}          # ref -> FOOTPRINT, collected before any removal (SWIG returns raw objects after Remove)


def find_fp(b, ref):
    if not FPS:
        for fp in list(b.GetFootprints()):
            FPS[fp.GetReference()] = fp
    return FPS.get(ref)


def do_move(b, op, changes):
    fp = find_fp(b, op["ref"])
    if fp is None:
        raise ValueError(f"no footprint {op['ref']}")
    before = fp_state(fp)
    side = op.get("side")
    if side and (side == "B") != fp.IsFlipped():
        try:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        except (AttributeError, TypeError):
            fp.Flip(fp.GetPosition(), False)
    if "x" in op and "y" in op:
        fp.SetPosition(P(op["x"], op["y"]))
    if op.get("rot") is not None:
        fp.SetOrientationDegrees(float(op["rot"]))
    if "locked" in op:
        fp.SetLocked(bool(op["locked"]))
    after = fp_state(fp)
    changes.append({"kind": "move", "ref": op["ref"], "from": before, "to": after})
    return after


def add_track(b, t, changes):
    tr = pcbnew.PCB_TRACK(b)
    tr.SetStart(P(*t["a"]))
    tr.SetEnd(P(*t["b"]))
    tr.SetWidth(MM(float(t.get("w", 0.25))))
    tr.SetLayer(layer_id(b, t.get("layer", "F.Cu")))
    n = net(b, t.get("net"))
    if n is not None:
        tr.SetNet(n)
    b.Add(tr)
    changes.append({"kind": "track", "net": t.get("net", ""), "layer": t.get("layer", "F.Cu"), "a": t["a"], "b": t["b"],
                    "w": t.get("w", 0.25), "uuid": tr.m_Uuid.AsString()})


def add_via(b, v, changes):
    via = pcbnew.PCB_VIA(b)
    via.SetPosition(P(v["x"], v["y"]))
    via.SetWidth(MM(float(v.get("d", 0.6))))
    via.SetDrill(MM(float(v.get("drill", 0.3))))
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    n = net(b, v.get("net"))
    if n is not None:
        via.SetNet(n)
    b.Add(via)
    changes.append({"kind": "via", "net": v.get("net", ""), "x": v["x"], "y": v["y"], "d": v.get("d", 0.6), "uuid": via.m_Uuid.AsString()})


def _xy(v):
    """Plain floats: KiCad's getters return references into the item, and a wrapper that outlives
    a removed item corrupts SWIG's type table (later calls then return raw SwigPyObjects)."""
    return (TO(v.x), TO(v.y))


def do_delete(b, op, changes):
    kinds = set(op.get("kinds") or ["track", "via"])
    nets = set(op.get("nets") or [])
    uuids = set(op.get("uuids") or [])
    region = op.get("region")
    everything = bool(op.get("all"))
    victims = []
    for t in list(b.GetTracks()):
        is_via = t.GetClass() == "PCB_VIA"
        if ("via" if is_via else "track") not in kinds:
            continue
        name = t.GetNetname()
        hit = everything
        if nets and (name in nets or name.rsplit("/", 1)[-1] in nets):
            hit = True
        if uuids and t.m_Uuid.AsString() in uuids:
            hit = True
        if region:
            x0, y0, x1, y1 = region
            pts = [_xy(t.GetPosition())] if is_via else [_xy(t.GetStart()), _xy(t.GetEnd())]
            if all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in pts):
                hit = hit or not nets
        if hit:
            victims.append(t)
    if "zone" in kinds:
        for z in list(b.Zones()):
            named = bool(uuids) and z.m_Uuid.AsString() in uuids
            if z.GetIsRuleArea() and not named:          # a keep-out goes only when it is named
                continue
            if everything or (nets and z.GetNetname() in nets) or named:
                victims.append(z)
    for v in victims:
        b.Remove(v)
    GRAVE.extend(victims)           # never let Python destroy a removed item (see GRAVE)
    changes.append({"kind": "delete", "count": len(victims)})
    return len(victims)


def _poly(z, pts):
    ol = z.Outline()
    ol.NewOutline()
    for x, y in pts:
        ol.Append(P(x, y))


def do_zone(b, op, changes):
    name = op.get("name") or op.get("net", "")
    if not op.get("add"):                        # a zone of the same name is replaced, not doubled
        old = [z for z in list(b.Zones()) if not z.GetIsRuleArea() and z.GetZoneName() == name]
        for z in old:
            b.Remove(z)
        GRAVE.extend(old)
    z = pcbnew.ZONE(b)
    ls = pcbnew.LSET()
    for l in op.get("layers") or [op.get("layer", "F.Cu")]:
        ls.AddLayer(layer_id(b, l))
    z.SetLayerSet(ls)
    n = net(b, op.get("net"))
    if n is not None:
        z.SetNet(n)
    z.SetZoneName(op.get("name") or op.get("net", ""))
    z.SetAssignedPriority(int(op.get("priority", 0)))
    z.SetMinThickness(MM(float(op.get("min_width", 0.25))))
    z.SetLocalClearance(MM(float(op.get("clearance", 0.25))))
    conn = {"solid": pcbnew.ZONE_CONNECTION_FULL, "tht": pcbnew.ZONE_CONNECTION_THT_THERMAL,
            "thermal": pcbnew.ZONE_CONNECTION_THERMAL, "none": pcbnew.ZONE_CONNECTION_NONE}
    z.SetPadConnection(conn.get(op.get("connect", "thermal"), pcbnew.ZONE_CONNECTION_THERMAL))
    z.SetThermalReliefGap(MM(float(op.get("thermal_gap", 0.25))))
    z.SetThermalReliefSpokeWidth(MM(float(op.get("spoke", 0.4))))
    if op.get("remove_islands", True):
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    _poly(z, op["polygon"])
    b.Add(z)
    changes.append({"kind": "zone", "net": op.get("net", ""), "layers": op.get("layers"), "uuid": z.m_Uuid.AsString()})


def _by_uuid(b, uid):
    """A track, via or zone by its uuid, or None."""
    for t in b.GetTracks():
        if t.m_Uuid.AsString() == uid:
            return t
    for z in b.Zones():
        if z.m_Uuid.AsString() == uid:
            return z
    return None


def do_track_set(b, op, changes):
    """{"op": "track_set", "uuid", "a"?, "b"?, "w"?, "layer"?, "net"?}: change a track segment in place."""
    t = _by_uuid(b, op["uuid"])
    if t is None or t.GetClass() not in ("PCB_TRACK", "PCB_ARC"):
        raise ValueError(f"no track {op['uuid']}")
    before = {"a": list(_xy(t.GetStart())), "b": list(_xy(t.GetEnd())), "w": TO(t.GetWidth()), "layer": b.GetLayerName(t.GetLayer())}
    if op.get("a"):
        t.SetStart(P(*op["a"]))
    if op.get("b"):
        t.SetEnd(P(*op["b"]))
    if op.get("w"):
        t.SetWidth(MM(float(op["w"])))
    if op.get("layer"):
        t.SetLayer(layer_id(b, op["layer"]))
    if op.get("net"):
        n = net(b, op["net"])
        if n is not None:
            t.SetNet(n)
    changes.append({"kind": "track_set", "uuid": op["uuid"], "from": before})
    return True


def do_via_set(b, op, changes):
    """{"op": "via_set", "uuid", "x"?, "y"?, "d"?, "drill"?}: move or resize a via."""
    v = _by_uuid(b, op["uuid"])
    if v is None or v.GetClass() != "PCB_VIA":
        raise ValueError(f"no via {op['uuid']}")
    before = {"x": TO(v.GetPosition().x), "y": TO(v.GetPosition().y), "d": TO(v.GetWidth())}
    if "x" in op or "y" in op:
        v.SetPosition(P(op.get("x", before["x"]), op.get("y", before["y"])))
    if op.get("d"):
        v.SetWidth(MM(float(op["d"])))
    if op.get("drill"):
        v.SetDrill(MM(float(op["drill"])))
    changes.append({"kind": "via_set", "uuid": op["uuid"], "from": before})
    return True


def do_zone_set(b, op, changes):
    """{"op": "zone_set", "uuid", "polygon"?, "net"?, "priority"?, "layers"?, "clearance"?, "min_width"?, "name"?}:
    change a pour or rule area in place (its fill is stale until the next fill)."""
    z = _by_uuid(b, op["uuid"])
    if z is None or z.GetClass() != "ZONE":
        raise ValueError(f"no zone {op['uuid']}")
    if op.get("polygon"):
        z.Outline().RemoveAllContours()
        _poly(z, op["polygon"])
        z.UnFill()
    if op.get("net") and not z.GetIsRuleArea():
        n = net(b, op["net"])
        if n is not None:
            z.SetNet(n)
            z.UnFill()
    if op.get("priority") is not None:
        z.SetAssignedPriority(int(op["priority"]))
    if op.get("layers"):
        ls = pcbnew.LSET()
        for l in op["layers"]:
            ls.AddLayer(layer_id(b, l))
        z.SetLayerSet(ls)
        z.UnFill()
    if op.get("clearance") is not None and not z.GetIsRuleArea():
        z.SetLocalClearance(MM(float(op["clearance"])))
    if op.get("min_width") is not None and not z.GetIsRuleArea():
        z.SetMinThickness(MM(float(op["min_width"])))
    if op.get("name") is not None:
        z.SetZoneName(str(op["name"]))
    changes.append({"kind": "zone_set", "uuid": op["uuid"]})
    return True


def do_rule_area(b, op, changes):
    name = op.get("name", "keepout")
    if not op.get("add"):                        # one of the same name is replaced, not doubled
        old = [z for z in list(b.Zones()) if z.GetIsRuleArea() and z.GetZoneName() == name]
        for z in old:
            b.Remove(z)
        GRAVE.extend(old)
    z = pcbnew.ZONE(b)
    z.SetIsRuleArea(True)
    ls = pcbnew.LSET()
    for l in op.get("layers") or ["F.Cu"]:
        ls.AddLayer(layer_id(b, l))
    z.SetLayerSet(ls)
    z.SetZoneName(op.get("name", "keepout"))
    z.SetDoNotAllowTracks(bool(op.get("no_tracks", True)))
    z.SetDoNotAllowVias(bool(op.get("no_vias", True)))
    z.SetDoNotAllowZoneFills(bool(op.get("no_pour", True)))
    z.SetDoNotAllowPads(bool(op.get("no_pads", False)))
    z.SetDoNotAllowFootprints(bool(op.get("no_footprints", False)))
    _poly(z, op["polygon"])
    b.Add(z)
    changes.append({"kind": "rule_area", "name": op.get("name", "keepout"), "uuid": z.m_Uuid.AsString()})


def do_outline(b, op, changes):
    old = [d for d in list(b.GetDrawings()) if d.GetLayer() == pcbnew.Edge_Cuts]
    for d in old:
        b.Remove(d)
    GRAVE.extend(old)
    w = MM(float(op.get("width", 0.1)))

    def seg(p, q):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(P(*p))
        s.SetEnd(P(*q))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(w)
        b.Add(s)

    def arc(s0, m, e):
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_ARC)
        s.SetArcGeometry(P(*s0), P(*m), P(*e))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(w)
        b.Add(s)
    if "rect" in op:
        x0, y0, x1, y1 = op["rect"]
        r = float(op.get("radius", 0))
        if r <= 0:
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            for i in range(4):
                seg(pts[i], pts[(i + 1) % 4])
        else:
            k = r * (1 - math.sqrt(0.5))
            seg((x0 + r, y0), (x1 - r, y0))
            seg((x1, y0 + r), (x1, y1 - r))
            seg((x1 - r, y1), (x0 + r, y1))
            seg((x0, y1 - r), (x0, y0 + r))
            arc((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r))
            arc((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1))
            arc((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r))
            arc((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))
    else:
        pts = op["polygon"]
        for i in range(len(pts)):
            seg(pts[i], pts[(i + 1) % len(pts)])
    changes.append({"kind": "outline"})


def do_text(b, op, changes):
    t = pcbnew.PCB_TEXT(b)
    t.SetText(op["text"])
    t.SetPosition(P(op["x"], op["y"]))
    t.SetLayer(layer_id(b, op.get("layer", "F.SilkS")))
    sz = MM(float(op.get("size", 1.0)))
    t.SetTextSize(pcbnew.VECTOR2I(sz, sz))
    t.SetTextThickness(MM(float(op.get("thickness", 0.15))))
    if op.get("rot"):
        t.SetTextAngleDegrees(float(op["rot"]))
    if op.get("layer", "F.SilkS").startswith("B."):
        t.SetMirrored(True)
    b.Add(t)
    changes.append({"kind": "text", "text": op["text"]})


def do_floorplan(b, op, changes):
    """The floorplan's areas (each block where it is meant to go, the keep-outs) as outlined rectangles with
    their names on a drawing layer, grouped as "Floorplan" so the next plan replaces them and nothing else."""
    groups = [g for g in list(b.Groups()) if g.GetName() == "Floorplan"]
    for g in groups:
        items = list(g.GetItems())
        for it in items:
            g.RemoveItem(it)
            b.Remove(it)
        b.Remove(g)
        GRAVE.extend(items)
        GRAVE.append(g)
    grp = pcbnew.PCB_GROUP(b)
    grp.SetName("Floorplan")
    b.Add(grp)
    lid = layer_id(b, op.get("layer", "Dwgs.User"))
    for r in op.get("rects", []):
        x0, y0, x1, y1 = [float(v) for v in r["rect"]]
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_RECT)
        s.SetStart(P(min(x0, x1), min(y0, y1)))
        s.SetEnd(P(max(x0, x1), max(y0, y1)))
        s.SetLayer(lid)
        s.SetWidth(MM(0.1))
        b.Add(s)
        grp.AddItem(s)
        if r.get("label"):
            t = pcbnew.PCB_TEXT(b)
            t.SetText(str(r["label"])[:40])
            sz = MM(min(1.2, max(0.6, (abs(y1 - y0)) / 6)))
            t.SetTextSize(pcbnew.VECTOR2I(sz, sz))
            t.SetTextThickness(MM(0.12))
            t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
            t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_TOP)
            t.SetPosition(P(min(x0, x1) + 0.4, min(y0, y1) + 0.4))
            t.SetLayer(lid)
            b.Add(t)
            grp.AddItem(t)
    changes.append({"kind": "floorplan", "rects": len(op.get("rects", []))})


def do_fill(b, changes):
    b.BuildConnectivity()
    zones = [z for z in b.Zones()]
    filler = pcbnew.ZONE_FILLER(b)
    filler.Fill(zones)
    changes.append({"kind": "fill", "zones": len(zones)})


def apply_ops(b, ops, changes, stop_on_error=True):
    """Apply ops to a loaded board; (ok, results)."""
    results = []
    ok = True
    for op in ops:
        kind = op.get("op")
        try:
            if kind == "move":
                r = do_move(b, op, changes)
            elif kind == "track":
                add_track(b, op, changes)
                r = True
            elif kind == "tracks":
                for t in op.get("items", []):
                    add_track(b, t, changes)
                r = len(op.get("items", []))
            elif kind == "via":
                add_via(b, op, changes)
                r = True
            elif kind == "vias":
                for v in op.get("items", []):
                    add_via(b, v, changes)
                r = len(op.get("items", []))
            elif kind == "delete":
                r = do_delete(b, op, changes)
            elif kind == "track_set":
                r = do_track_set(b, op, changes)
            elif kind == "via_set":
                r = do_via_set(b, op, changes)
            elif kind == "zone_set":
                r = do_zone_set(b, op, changes)
            elif kind == "zone":
                do_zone(b, op, changes)
                r = True
            elif kind == "rule_area":
                do_rule_area(b, op, changes)
                r = True
            elif kind == "outline":
                do_outline(b, op, changes)
                r = True
            elif kind == "text":
                do_text(b, op, changes)
                r = True
            elif kind == "floorplan":
                do_floorplan(b, op, changes)
                r = True
            elif kind == "ref_text":
                fp = find_fp(b, op["ref"])
                if fp is None:
                    raise ValueError(f"no footprint {op['ref']}")
                f = fp.Reference()
                f.SetPosition(P(op["x"], op["y"]))
                if op.get("rot") is not None:
                    f.SetTextAngleDegrees(float(op["rot"]))
                if op.get("size"):
                    sz = MM(float(op["size"]))
                    f.SetTextSize(pcbnew.VECTOR2I(sz, sz))
                if "visible" in op:
                    f.SetVisible(bool(op["visible"]))
                changes.append({"kind": "ref_text", "ref": op["ref"], "x": op["x"], "y": op["y"]})
                r = True
            elif kind == "value":
                fp = find_fp(b, op["ref"])
                fp.SetValue(op["value"])
                r = True
            elif kind == "lock":
                for ref in op.get("refs", []):
                    fp = find_fp(b, ref)
                    if fp:
                        fp.SetLocked(bool(op.get("locked", True)))
                r = True
            elif kind == "layers":
                b.SetCopperLayerCount(int(op["copper"]))
                r = True
            elif kind == "fill":
                do_fill(b, changes)
                r = True
            else:
                raise ValueError(f"unknown op {kind}")
            results.append({"op": kind, "ok": True, "result": r})
        except Exception as e:
            ok = False
            results.append({"op": kind, "ok": False, "error": f"{type(e).__name__}: {e}"})
            if stop_on_error:
                break
    return ok, results


def main():
    req = json.loads(sys.stdin.read())
    path = req["board"]
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    keep = {}
    for f in (pro,):
        if os.path.exists(f):
            keep[f] = open(f, "rb").read()
    b = pcbnew.LoadBoard(path)
    find_fp(b, "")                                    # index the footprints now, before anything is removed
    changes = []
    ok, results = apply_ops(b, req.get("ops", []), changes, req.get("stop_on_error", True))
    saved = False
    if req.get("save", True) and (ok or not req.get("stop_on_error", True)):
        if req.get("fill_after"):
            do_fill(b, changes)
        pcbnew.SaveBoard(path, b)
        saved = True
        for f, data in keep.items():                  # SaveBoard may rewrite project settings: put them back
            if open(f, "rb").read() != data:
                open(f, "wb").write(data)
    print("TWJSON " + json.dumps({"ok": ok, "saved": saved, "results": results, "changes": changes}))


if __name__ == "__main__":
    code = 0
    try:
        main()
    except Exception as e:
        print("TWJSON " + json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}))
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
