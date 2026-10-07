"""The floorplan from the guided start: the board to scale before the schematic existed -- its size and
corners, the mounting holes, each connector on its edge, the main blocks where they go, keep-outs -- as agreed
with the user (who may have dragged things into place). Kept in .tracewright/canvas.json ("floorplan";
millimetres from the board's top-left corner, y down).

  ./tw floorplan           the plan in board coordinates: what to put where
  ./tw floorplan apply     on the board: the outline (unless the board has one; --outline replaces it), the
                           blocks' areas and keep-outs drawn on Dwgs.User (group "Floorplan"), and the
                           connectors, holes and one-part blocks whose references are on the board moved to their
                           places (a block turned on the plan turns its part; a connector's rotation is left as it
                           is: turn each so it faces off its edge); keep-outs named as a slot or cut-out are cut in
                           the outline layer (a milled slot with round ends when long and thin)

The check placement.floorplan holds the placed board to it: each connector on its planned edge, each hole
where it was agreed."""
import re
import json, os

ORIGIN = (100.0, 100.0)          # the board's top-left corner in KiCad's coordinates (as the toolkit reference)


def load(project):
    """The floorplan dict, or None when the project has none."""
    for f in (os.path.join(project.root, ".tracewright", "canvas.json"), os.path.join(project.root, "design", "floorplan.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        fp = d.get("floorplan") if "floorplan" in d else d
        if isinstance(fp, dict) and fp.get("board"):
            return fp
    return None


def origin_for(board):
    """Where the plan's top-left corner goes: the board outline's own corner when it has one."""
    if board is not None and board.outline:
        xs = [p[0] for l in board.outline for p in l]
        ys = [p[1] for l in board.outline for p in l]
        return (min(xs), min(ys))
    return ORIGIN


def placed(fp, origin=ORIGIN):
    """Everything on the plan in board coordinates:
    {"outline": [x0, y0, x1, y1], "radius", "holes": [{id, ref, x, y, d}],
     "connectors": [{id, ref, label, edge, x, y, rect, faces}], "blocks": [{id, ref, label, kind, x, y, rect}],
     "keepouts": [{label, rect}]}"""
    ox, oy = origin
    W, H = fp["board"]["w"], fp["board"]["h"]
    out = {"outline": [ox, oy, ox + W, oy + H], "radius": fp["board"].get("radius", 0) or 0, "holes": [], "connectors": [],
           "blocks": [], "keepouts": []}
    for h in fp.get("holes") or []:
        out["holes"].append({"id": h["id"], "ref": h.get("ref") or "", "x": ox + h["x"], "y": oy + h["y"], "d": h.get("d", 3.2)})
    for it in fp.get("items") or []:
        w, d = it.get("w", 8), it.get("h", 6)             # w along the edge, d into the board
        if it.get("edge") and int(it.get("rot") or 0) % 180 == 90:
            w, d = d, w                                   # turned: its other side lies along the edge
        if it.get("edge"):
            e, at = it["edge"], it.get("at", 0)
            if e == "left":
                x, y, rect = ox + d / 2, oy + at, [ox, oy + at - w / 2, ox + d, oy + at + w / 2]
            elif e == "right":
                x, y, rect = ox + W - d / 2, oy + at, [ox + W - d, oy + at - w / 2, ox + W, oy + at + w / 2]
            elif e == "top":
                x, y, rect = ox + at, oy + d / 2, [ox + at - w / 2, oy, ox + at + w / 2, oy + d]
            else:
                x, y, rect = ox + at, oy + H - d / 2, [ox + at - w / 2, oy + H - d, ox + at + w / 2, oy + H]
            out["connectors"].append({"id": it["id"], "ref": it.get("ref") or "", "label": it.get("label", ""), "edge": e,
                                      "x": round(x, 3), "y": round(y, 3), "rect": [round(v, 3) for v in rect], "faces": e})
        else:
            x, y = ox + it["x"], oy + it["y"]
            rot = int(it.get("rot") or 0) % 360
            if rot in (90, 270):                           # turned a quarter: its area turns with it
                w, d = d, w
            out["blocks"].append({"id": it["id"], "ref": it.get("ref") or "", "label": it.get("label", ""), "kind": it.get("kind", ""),
                                  "x": round(x, 3), "y": round(y, 3), "rot": rot,
                                  "rect": [round(v, 3) for v in (x - w / 2, y - d / 2, x + w / 2, y + d / 2)]})
    for k in fp.get("keepouts") or []:
        x, y = ox + k["x"], oy + k["y"]
        kw, kh = (k["h"], k["w"]) if int(k.get("rot") or 0) % 180 == 90 else (k["w"], k["h"])
        out["keepouts"].append({"label": k.get("label", ""), "rect": [x - kw / 2, y - kh / 2, x + kw / 2, y + kh / 2]})
    return out


def describe(fp, origin=ORIGIN):
    """The plan as lines for Claude."""
    pl = placed(fp, origin)
    b = fp["board"]
    x0, y0, x1, y1 = pl["outline"]
    out = [f"Board {b['w']:g} x {b['h']:g} mm" + (f", corners r{b['radius']:g}" if b.get("radius") else "") +
           f": outline rect [{x0:g}, {y0:g}, {x1:g}, {y1:g}]" + (" (the user set this size)" if b.get("moved") else "")]
    for h in pl["holes"]:
        out.append(f"Hole {h['ref'] or h['id']}: {h['d']:g} mm at ({h['x']:g}, {h['y']:g})")
    for c in pl["connectors"]:
        mv = " (the user put it here)" if next((i for i in fp.get("items") or [] if i["id"] == c["id"] and i.get("moved")), None) else ""
        out.append(f"Connector {c['ref'] or c['id']} {c['label']}: on the {c['edge']} edge, centre ({c['x']:g}, {c['y']:g}), "
                   f"inside [{', '.join(f'{v:g}' for v in c['rect'])}], opening facing {c['faces']}{mv}")
    for k in pl["blocks"]:
        it = next((i for i in fp.get("items") or [] if i["id"] == k["id"]), {})
        how = [f"turned {k['rot']}°"] if k.get("rot") else []
        how += ["placed by the user"] if it.get("moved") else []
        how += ["locked: keep it there"] if it.get("locked") else []
        out.append(f"Block {k['label'] or k['id']}{' (' + k['ref'] + ')' if k['ref'] else ''}: area [{', '.join(f'{v:g}' for v in k['rect'])}]"
                   + (f" ({', '.join(how)})" if how else ""))
    for k in pl["keepouts"]:
        out.append(f"Keep-out {k['label']}: [{', '.join(f'{v:g}' for v in k['rect'])}]")
    if fp.get("note"):
        out.append("Note: " + fp["note"])
    return out


CUT = re.compile(r"\bslots?\b|cut[- ]?outs?|\bmilled\b", re.I)


def ops(fp, board=None, origin=None, outline=None):
    """Board operations that put the plan on the board (see the module docstring). outline: None = only when the
    board has none, True = always, False = never."""
    origin = origin or origin_for(board)
    pl = placed(fp, origin)
    out = []
    has_outline = bool(board is not None and board.outline)
    if outline or (outline is None and not has_outline):
        out.append({"op": "outline", "rect": pl["outline"], "radius": pl["radius"]})
    rects = [{"rect": k["rect"], "label": k["label"] or k["id"]} for k in pl["blocks"]] + \
            [{"rect": k["rect"], "label": "keep out: " + k["label"] if k["label"] else "keep out"} for k in pl["keepouts"]]
    if rects:
        out.append({"op": "floorplan", "rects": rects})
    for k in pl["keepouts"]:                               # a slot or cut-out in the plan is cut in the board
        if CUT.search(k.get("label") or ""):
            x0, y0, x1, y1 = k["rect"]
            w, h = x1 - x0, y1 - y0
            if max(w, h) >= 3 * min(w, h):                  # long and thin: a milled slot with round ends
                r = min(w, h) / 2
                a, c = ((x0 + r, (y0 + y1) / 2), (x1 - r, (y0 + y1) / 2)) if w >= h else (((x0 + x1) / 2, y0 + r), ((x0 + x1) / 2, y1 - r))
                out.append({"op": "cutout", "slot": {"a": [round(v, 3) for v in a], "b": [round(v, 3) for v in c], "w": round(min(w, h), 3)}})
            else:
                out.append({"op": "cutout", "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]})
    refs = {f.ref for f in board.fp_list} if board is not None else set()
    for c in pl["connectors"] + pl["holes"]:
        if c.get("ref") and c["ref"] in refs:
            out.append({"op": "move", "ref": c["ref"], "x": c["x"], "y": c["y"]})
    for k in pl["blocks"]:                                  # a block that is one part: the part goes there, turned as planned
        if k.get("ref") and k["ref"] in refs:
            out.append({"op": "move", "ref": k["ref"], "x": k["x"], "y": k["y"], **({"rot": k["rot"]} if k.get("rot") else {})})
    return out
