"""Where tracks can and cannot go. The routing space of each layer as the router sees it (what is free for a track of a
net class), how crowded each part of the board is (the connections still to route against the room there), and, for any
point, what keeps a track out of it -- so the user sees why a net will not fit before the router says it failed.

    congestion(project, cls="Default", cell=None) -> {"cell", "x0", "y0", "nx", "ny", "layers", "capacity", "demand",
                                                      "free": {layer: [...]}, "hot": [{box, ratio, links, parts, text}]}
    why(project, x, y, layer=None, net=None) -> {"layer", "free", "reasons": [{"kind", "text", "gap"?, "need"?}], "text"}

Demand is the usual estimate (RUDY): each unrouted connection spreads its length evenly over the box between its ends.
Capacity is the free share of each block on every routing layer, in tracks of the class's width and clearance.
"""
import math

import numpy as np

from . import geom


def _grid(project):
    from .route.driver import GridRoute
    return GridRoute(project, log=lambda m: None).setup()


def congestion(project, cls="Default", cell=None, g=None):
    from .ratsnest import ratsnest
    from .route import router as R
    g = g or _grid(project)
    B = g.B
    prof = cls if cls in B.profiles else "Default"
    pr = B.profiles[prof]
    span = max(B.nx, B.ny) * R.RES
    size = float(cell or max(1.0, round(span / 90.0 * 2) / 2))          # about 90 blocks across, 0.5 mm steps
    k = max(1, int(round(size / R.RES)))
    ny, nx = int(math.ceil(B.ny / k)), int(math.ceil(B.nx / k))
    pad_y, pad_x = ny * k - B.ny, nx * k - B.nx
    pitch = pr.w + pr.cl
    free = {}
    cap = np.zeros((ny, nx))
    inside = None
    for li, l in enumerate(B.layers):
        t = B.T[prof][li]
        f = np.pad((t == -1).astype(np.float32) + 0.5 * (t >= 0), ((0, pad_y), (0, pad_x)))   # a net's own copper: room for its own tracks
        frac = f.reshape(ny, k, nx, k).mean(axis=(1, 3))
        free[l] = frac
        cap += frac * (size / pitch)
        board = np.pad((t != -2).astype(np.float32), ((0, pad_y), (0, pad_x))).reshape(ny, k, nx, k).mean(axis=(1, 3))
        inside = board if inside is None else np.maximum(inside, board)
    # the edge and the outside are -2 on every layer; a block is on the board if any of it is not that
    on_board = inside > 0.05
    dem = np.zeros((ny, nx))
    links = ratsnest(g.b)
    x0, y0 = B.x0, B.y0
    for x1, y1, x2, y2, net in links:
        if net in g.planes:
            continue                                    # a pour joins it, not a track
        L = abs(x2 - x1) + abs(y2 - y1)
        if L <= 0:
            continue
        bx0, bx1 = min(x1, x2), max(x1, x2)
        by0, by1 = min(y1, y2), max(y1, y2)
        w, h = max(bx1 - bx0, size), max(by1 - by0, size)
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        bx0, bx1, by0, by1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        dens = L / (w * h)                              # mm of track per mm² of the box
        i0, i1 = int((bx0 - x0) // size), int((bx1 - x0) // size)
        j0, j1 = int((by0 - y0) // size), int((by1 - y0) // size)
        for j in range(max(0, j0), min(ny - 1, j1) + 1):
            oy = min(by1, y0 + (j + 1) * size) - max(by0, y0 + j * size)
            if oy <= 0:
                continue
            for i in range(max(0, i0), min(nx - 1, i1) + 1):
                ox = min(bx1, x0 + (i + 1) * size) - max(bx0, x0 + i * size)
                if ox > 0:
                    dem[j, i] += dens * ox * oy / size      # tracks across the block
    ratio = np.where(on_board & (cap > 0.05), dem / np.maximum(cap, 1e-6), np.where(on_board & (dem > 0.5), 9.9, 0.0))
    hot = _hot(g, ratio, dem, cap, on_board, size, x0, y0, links)
    rnd = lambda a, n=2: [[round(float(v), n) for v in row] for row in a]
    return {"cell": size, "x0": round(x0, 3), "y0": round(y0, 3), "nx": nx, "ny": ny, "layers": list(B.layers),
            "class": prof, "track": round(pr.w, 3), "clearance": round(pr.cl, 3),
            "capacity": rnd(np.where(on_board, cap, -1), 1), "demand": rnd(dem, 1), "ratio": rnd(ratio),
            "free": {l: rnd(np.where(on_board, f, -1)) for l, f in free.items()}, "hot": hot, "links": len(links)}


def _hot(g, ratio, dem, cap, on_board, size, x0, y0, links, thresh=0.9):
    """Crowded regions: blocks asked for more tracks than fit, grouped, each said in words."""
    ny, nx = ratio.shape
    seen = np.zeros_like(ratio, dtype=bool)
    out = []
    for j in range(ny):
        for i in range(nx):
            if seen[j, i] or not on_board[j, i] or ratio[j, i] < thresh or dem[j, i] < 1.0:
                continue
            stack, cells = [(j, i)], []
            seen[j, i] = True
            while stack:
                a, c = stack.pop()
                cells.append((a, c))
                for da, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    b_, d_ = a + da, c + dc
                    if 0 <= b_ < ny and 0 <= d_ < nx and not seen[b_, d_] and on_board[b_, d_] and ratio[b_, d_] >= thresh and dem[b_, d_] >= 0.5:
                        seen[b_, d_] = True
                        stack.append((b_, d_))
            if len(cells) < 2 and ratio[j, i] < 1.5:
                continue
            js, is_ = [a for a, _ in cells], [c for _, c in cells]
            box = (x0 + min(is_) * size, y0 + min(js) * size, x0 + (max(is_) + 1) * size, y0 + (max(js) + 1) * size)
            peak = max(float(ratio[a, c]) for a, c in cells)
            need = max(float(dem[a, c]) for a, c in cells)
            room = min(float(cap[a, c]) for a, c in cells if ratio[a, c] == peak) if peak < 9 else 0.0
            n_links = sum(1 for x1, y1, x2, y2, _ in links
                          if geom.bbox_overlap((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)), box))
            parts = sorted({fp.ref for fp in g.b.fp_list if geom.bbox_overlap(fp.bbox(), box, 0.5)})[:5]
            text = (f"{n_links} connection{'s cross' if n_links != 1 else ' crosses'} here: about {need:.0f} track{'s' if round(need) != 1 else ''} "
                    f"wanted where {room:.0f} fit{'s' if round(room) == 1 else ''}" + (f" (near {', '.join(parts)})" if parts else ""))
            out.append({"box": [round(v, 2) for v in box], "ratio": round(peak, 2), "links": n_links, "parts": parts, "text": text})
    out.sort(key=lambda h: -h["ratio"])
    return out[:12]


def _rules(project):
    from .pro import ProjectSettings
    return ProjectSettings.load(project.pro) if project.pro else ProjectSettings({})


def why(project, x, y, layer=None, net=None, board=None):
    """What keeps a track of `net` (its class's width and clearance) off the point (x, y) on `layer`."""
    from .board import Board
    from . import stackup
    b = board or Board.load(project.pcb)
    pro = _rules(project)
    layer = layer or "F.Cu"
    cls = pro.class_of(net) if net else "Default"
    c = pro.cls(cls)
    w, cl = float(c["track_width"]), float(c["clearance"])
    reasons = []
    p = (x, y)
    edge_cl = float(pro.rule("min_copper_edge_clearance", 0.3) or 0.3)
    if b.outline:
        outer = max(b.outline, key=lambda o: abs(geom.area(o)))
        if not geom.inside(p, outer):
            return {"layer": layer, "free": False, "reasons": [{"kind": "outside", "text": "outside the board"}],
                    "text": "Outside the board."}
        d = min(geom.seg_point_dist(p, outer[i], outer[(i + 1) % len(outer)]) for i in range(len(outer)))
        if d < edge_cl + w / 2:
            reasons.append({"kind": "edge", "text": f"the board edge {d:.2f} mm away: copper keeps {edge_cl:g} mm from it", "gap": round(d, 3),
                            "need": round(edge_cl + w / 2, 3)})
        for hole in b.outline[1:] if len(b.outline) > 1 else []:
            if geom.inside(p, hole) or geom.poly_dist(p, hole) < edge_cl + w / 2:
                reasons.append({"kind": "edge", "text": "a cut-out in the board"})
    if layer not in b.copper:
        return {"layer": layer, "free": False, "reasons": [{"kind": "layer", "text": f"the board has no {layer}"}], "text": f"No {layer}."}
    roles = stackup.board_roles(b, stackup.get(getattr(project, "cfg", None)))
    r = roles.get(layer, {})
    if r.get("role") == "plane":
        reasons.append({"kind": "plane", "text": f"{layer} is the {str(r.get('net') or '').rsplit('/', 1)[-1] or 'plane'} plane: tracks go on the signal layers"})
    for z in b.zones:
        if not z.is_rule_area or not z.outline:
            continue
        on = any(l == layer or l == "*.Cu" or (l == "F&B.Cu" and layer in ("F.Cu", "B.Cu")) for l in z.layers)
        if on and any(geom.inside(p, pl) for pl in z.outline):
            ko = z.keepout or {}
            if ko.get("tracks"):
                reasons.append({"kind": "keepout", "text": f"inside the keep-out area {z.name or ''}".rstrip() + " (no tracks)"})
            elif z.name.startswith("TW neck"):
                reasons.append({"kind": "info", "text": f"in the fine-pitch area of {z.name[8:]}: tracks may neck down here"})
            elif z.name.startswith("TW region"):
                reasons.append({"kind": "info", "text": f"in the region {z.name[10:]} (its own rules)"})
    short = lambda n: (n or "").rsplit("/", 1)[-1]
    near = []
    for fp in b.fp_list:
        if not geom.bbox_overlap(fp.bbox(), (x - 3, y - 3, x + 3, y + 3)):
            continue
        for pd in fp.pads:
            if layer not in pd.layers and "*.Cu" not in pd.layers:
                continue
            if net and pd.net == net:
                continue
            d = min((geom.poly_dist(p, pl) for pl in pd.polys if len(pl) >= 3), default=geom.dist(p, (pd.x, pd.y)))
            need = cl + w / 2
            if pd.drill and pd.kind == "np_thru_hole":
                need = float(pro.rule("min_hole_clearance", 0.25) or 0.25) + w / 2
            near.append((d, need, f"{fp.ref} pin {pd.num}" + (f" ({short(pd.net)})" if pd.net else ""), "pad"))
    for t in b.tracks:
        if t.layer != layer or (net and t.net == net):
            continue
        d = geom.seg_point_dist(p, t.a, t.b) - t.w / 2
        if d < 2:
            near.append((max(d, 0.0), cl + w / 2, f"a track of {short(t.net)}", "track"))
    for v in b.vias:
        if layer not in v.span(b.copper) or (net and v.net == net):
            continue
        d = geom.dist(p, (v.x, v.y)) - v.d / 2
        if d < 2:
            near.append((max(d, 0.0), cl + w / 2, f"a via of {short(v.net)}", "via"))
    near.sort(key=lambda q: q[0] - q[1])
    for d, need, what, kind in near:
        if d < need - 1e-6:
            reasons.append({"kind": kind, "text": (f"inside {what}" if d <= 0 else f"{what} {d:.2f} mm away, a {w:g} mm track needs {need:.2f}"),
                            "gap": round(d, 3), "need": round(need, 3)})
        if len([r_ for r_ in reasons if r_["kind"] in ("pad", "track", "via")]) >= 4:
            break
    pour = next((z for z in b.zones if not z.is_rule_area and z.net and z.net != net and layer in z.fills
                 and any(geom.inside(p, pl) for pl in z.fills[layer])), None)
    blocking = [r_ for r_ in reasons if r_["kind"] != "info"]
    if pour is not None and not blocking:
        reasons.append({"kind": "info", "text": f"under the {short(pour.net)} pour: it makes room when filled again"})
    free = not blocking
    if free:
        nearest = near[0] if near else None
        txt = f"A {w:g} mm {cls} track fits here" + (f" (nearest: {nearest[2]}, {nearest[0]:.2f} mm)" if nearest else "") + "."
    else:
        txt = "No track here: " + "; ".join(r_["text"] for r_ in blocking[:3]) + "."
    return {"layer": layer, "free": free, "class": cls, "track": w, "clearance": cl, "reasons": reasons, "text": txt}
