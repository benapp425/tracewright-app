#!/usr/bin/env python3
"""A V-scored panel of a rectangular board, made under KiCad's Python (pcbnew). Reads JSON on stdin:

  {"board": "x.kicad_pcb", "out": "panel.kicad_pcb", "nx": 2, "ny": 3, "rail": 5.0, "rails": "tb" | "lr" | "all" | "none",
   "fiducials": true, "tooling": true, "label": "Board name rev A"}

Each copy is the board moved by its own size (the copies touch, to be V-scored apart); every copy's nets are renamed
B<k>-<net> so the copies stay separate circuits. The board outlines become the V-score lines (on Cmts.User, marked
V-CUT); the panel's outline (Edge.Cuts) is the frame with its rails, which carry the tooling holes (1.152 mm, JLC's) and
three fiducials. Prints TWJSON {ok, size, boards, cuts}.
"""
import sys, json, traceback
import pcbnew

MM = pcbnew.FromMM
TO = pcbnew.ToMM
GRAVE = []


def P(x, y):
    return pcbnew.VECTOR2I(MM(float(x)), MM(float(y)))


def _net(board, name, cache):
    if name in cache:
        return cache[name]
    ni = board.FindNet(name)
    if ni is None:
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
    cache[name] = ni
    return ni


def _pad(fp, kind, d, mask_margin=None):
    pad = pcbnew.PAD(fp)
    if kind == "npth":
        pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
        layers = pad.UnplatedHoleMask()
    else:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        layers = pcbnew.LSET()
        layers.AddLayer(pcbnew.F_Cu)
        layers.AddLayer(pcbnew.F_Mask)
    try:
        pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(MM(d), MM(d)))
    except TypeError:
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(MM(d), MM(d)))
    if kind == "npth":
        pad.SetDrillSize(pcbnew.VECTOR2I(MM(d), MM(d)))
    pad.SetLayerSet(layers)
    if mask_margin is not None:
        try:
            pad.SetLocalSolderMaskMargin(MM(mask_margin))
        except Exception:
            pass
    fp.Add(pad)
    return pad


def _part(board, ref, x, y, kind, d, mask_margin=None):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue("panel")
    try:
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
    except Exception:
        pass
    board.Add(fp)
    _pad(fp, kind, d, mask_margin)
    fp.SetPosition(P(x, y))
    try:
        fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    except Exception:
        pass
    return fp


def _line(board, a, b, layer, w=0.1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(P(*a))
    s.SetEnd(P(*b))
    s.SetLayer(layer)
    s.SetWidth(MM(w))
    board.Add(s)
    return s


def _text(board, x, y, txt, layer, size=1.2):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(txt)
    t.SetPosition(P(x, y))
    t.SetLayer(layer)
    t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    t.SetTextThickness(MM(size * 0.15))
    board.Add(t)


def main():
    req = json.load(sys.stdin)
    b = pcbnew.LoadBoard(req["board"])
    nx, ny = int(req.get("nx", 2)), int(req.get("ny", 2))
    rail = float(req.get("rail", 5.0))
    rails = req.get("rails", "tb")
    edges = [d for d in b.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    if not edges:
        raise ValueError("the board has no outline")
    bb = b.GetBoardEdgesBoundingBox()
    x0, y0 = TO(bb.GetX()), TO(bb.GetY())
    W, H = TO(bb.GetWidth()), TO(bb.GetHeight())
    items = list(b.GetFootprints()) + list(b.GetTracks()) + list(b.Zones()) + \
        [d for d in b.GetDrawings() if d.GetLayer() != pcbnew.Edge_Cuts]
    k = 0
    for j in range(ny):
        for i in range(nx):
            k += 1
            if i == 0 and j == 0:
                continue
            off = P(W * i, H * j)
            cache = {}
            for it in items:
                try:
                    dup = it.Duplicate(False)                # KiCad 10: (addToParentGroup)
                except TypeError:
                    dup = it.Duplicate()
                if hasattr(dup, "Cast"):
                    dup = dup.Cast()                         # SWIG hands back the base class: the real type
                b.Add(dup)
                dup.Move(off)
                if isinstance(dup, pcbnew.FOOTPRINT):
                    for pad in dup.Pads():
                        n = pad.GetNetname()
                        if n:
                            pad.SetNet(_net(b, f"B{k}-{n}", cache))
                elif hasattr(dup, "GetNetname") and dup.GetNetname():
                    dup.SetNet(_net(b, f"B{k}-{dup.GetNetname()}", cache))
    for d in edges:                                  # the boards' own outlines: gone, the V-score lines say it
        b.Remove(d)
    GRAVE.extend(edges)
    top = rail if rails in ("tb", "all") else 0.0
    side = rail if rails in ("lr", "all") else 0.0
    X0, Y0, X1, Y1 = x0 - side, y0 - top, x0 + W * nx + side, y0 + H * ny + top
    for a, c in (((X0, Y0), (X1, Y0)), ((X1, Y0), (X1, Y1)), ((X1, Y1), (X0, Y1)), ((X0, Y1), (X0, Y0))):
        _line(b, a, c, pcbnew.Edge_Cuts, 0.1)
    cuts = []
    xs = [x0 + W * i for i in range(nx + 1)]
    ys = [y0 + H * j for j in range(ny + 1)]
    for x in xs:
        if X0 < x < X1 or (side and x in (xs[0], xs[-1])):
            if X0 + 1e-6 < x < X1 - 1e-6:
                _line(b, (x, Y0), (x, Y1), pcbnew.Cmts_User, 0.15)
                cuts.append(["x", round(x, 3)])
    for y in ys:
        if Y0 + 1e-6 < y < Y1 - 1e-6:
            _line(b, (X0, y), (X1, y), pcbnew.Cmts_User, 0.15)
            cuts.append(["y", round(y, 3)])
    _text(b, X0 + 1.0, Y0 - 1.5, "V-CUT: score along the lines on this layer", pcbnew.Cmts_User, 1.2)
    n = 0
    if top or side:
        if req.get("tooling", True):
            pts = [(X0 + 3.5, Y0 + top / 2 if top else Y0 + 3.5), (X1 - 3.5, Y0 + top / 2 if top else Y0 + 3.5),
                   (X0 + 3.5, Y1 - top / 2 if top else Y1 - 3.5), (X1 - 3.5, Y1 - top / 2 if top else Y1 - 3.5)]
            if not top:
                pts = [(X0 + side / 2, Y0 + 3.5), (X1 - side / 2, Y0 + 3.5), (X0 + side / 2, Y1 - 3.5), (X1 - side / 2, Y1 - 3.5)]
            for x, y in pts:
                n += 1
                _part(b, f"TH{n}", x, y, "npth", 1.152)
        if req.get("fiducials", True):
            if top:
                fpts = [(X0 + 8, Y0 + top / 2), (X1 - 8, Y0 + top / 2), (X0 + 8, Y1 - top / 2)]
            else:
                fpts = [(X0 + side / 2, Y0 + 8), (X1 - side / 2, Y0 + 8), (X0 + side / 2, Y1 - 8)]
            for m, (x, y) in enumerate(fpts, 1):
                _part(b, f"FID{m}", x, y, "smd", 1.0, mask_margin=0.5)
        if req.get("label"):                             # in the middle of a rail, clear of the holes and fiducials
            lw = 0.95 * 1.2 * len(req["label"])
            _text(b, (X0 + X1) / 2 - lw / 2, (Y1 - top / 2 + 0.6) if top else (Y1 - 3.5), req["label"], pcbnew.F_SilkS, 1.2)
    b.BuildConnectivity()
    filler = pcbnew.ZONE_FILLER(b)
    filler.Fill([z for z in b.Zones()])
    pcbnew.SaveBoard(req["out"], b)
    print("TWJSON " + json.dumps({"ok": True, "size": [round(X1 - X0, 3), round(Y1 - Y0, 3)], "boards": nx * ny,
                                  "board": [round(W, 3), round(H, 3)], "cuts": cuts}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("TWJSON " + json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}))
