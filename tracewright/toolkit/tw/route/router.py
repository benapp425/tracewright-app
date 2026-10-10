#!/usr/bin/env python3
"""Grid router with a human routing style (numpy; the search core is astar.c, built on first use).

Originally written for a CM5 carrier board and generalised by tw/route/driver.py, which builds
the board dump from the .kicad_pcb and the profiles from the project's net classes.

The board (see driver.dump_for_router) is rasterised at RES mm on its routing layers: F.Cu and B.Cu (inner
layers taken to be planes), or the signal layers of the project's stack-up plan (tw/stackup.py), each routed in
its own direction, vias through every layer. For every routing profile (track width + clearance + via) each
cell holds which net may put a track centreline (or a via centre) there: -1 free, >= 0 only that
net, -2 nobody. Obstacles are stamped with the true distance to their copper, so a legal cell is
DRC-clean by construction (the KiCad DRC is still the final judge).

Neck areas (the CM5 socket fan-out): inside them every net routes with its neck profile (0.1 mm
track / 0.1 mm clearance, custom DRC rule 'J201 fanout') and vias may only sit on the listed via
columns, so the escape pattern stays regular. Segments are split at the area edge: necked inside,
full width outside.

Style rules, as a person would route:
  * 0 / 45 / 90 degree segments only, no acute angles; bends cost, 90-degree bends cost more;
  * vias are expensive, so short nets stay on one layer and long nets change layer once;
  * B.Cu prefers runs along the board (x), F.Cu has no preference (a stack-up plan sets each layer's direction);
  * multi-pin nets grow as a tree from the connector pin, joining the nearest pad each time;
  * every path is then pulled tight: runs are replaced by the fewest straight / 45-degree doglegs
    that stay legal.
Rip-up and reroute: the rasters of the fixed copper (pads, rule areas, hand geometry, pours) are
kept, so routed nets can be removed and the board re-stamped (Router.rip / Router.rebuild).
"""
import heapq, json, math, os, ctypes
import numpy as np

RES = 0.1
LAYERS = ("F.Cu", "B.Cu")
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]   # index = 45-degree steps

_LIB = None


def _build(src, so):
    """Compile astar.c to `so` for this machine (on a Mac, for the CPU this Python runs as, Rosetta included)."""
    import subprocess, shutil, platform, sys
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        return
    os.makedirs(os.path.dirname(so), exist_ok=True)
    arch = ["-arch", platform.machine()] if sys.platform == "darwin" else []
    try:
        subprocess.run([cc, "-O3", "-shared", "-fPIC", *arch, "-o", so, src], check=False, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass


def _load(so):
    try:
        return ctypes.CDLL(so) if os.path.exists(so) else None
    except OSError:          # built for another machine (another system or CPU)
        return None


def _lib():
    """The C search core (astar.c), compiled on first use (None if it cannot be built or loaded: pure Python)."""
    global _LIB
    if _LIB is None:
        import platform, sys
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "astar.c")
        cache = os.path.join(os.path.expanduser("~"), ".cache", "tracewright")
        L = None
        for so in (os.path.join(here, "libastar.so") if os.access(here, os.W_OK) else os.path.join(cache, "libastar.so"),
                   os.path.join(cache, f"libastar-{sys.platform}-{platform.machine()}.so")):
            if not os.path.exists(so) or os.path.getmtime(so) < os.path.getmtime(src):
                _build(src, so)
            L = _load(so)
            if L is not None:
                break
        if L is None:
            _LIB = False
            return None
        P8, P32 = (np.ctypeslib.ndpointer(t, flags="C_CONTIGUOUS") for t in (np.uint8, np.int32))
        L.astar.restype = ctypes.c_long
        PF = np.ctypeslib.ndpointer(np.float32, flags="C_CONTIGUOUS")
        L.astar.argtypes = ([ctypes.c_int] * 3 + [P8, P8, ctypes.c_void_p, P32, ctypes.c_long, P8] + [ctypes.c_int] * 8
                            + [ctypes.c_float] * 3 + [PF, PF, PF] + [ctypes.c_float, ctypes.c_int, ctypes.c_long, P32, ctypes.c_long])
        _LIB = L
    return _LIB


class Profile:
    """Track width, clearance and via for one kind of route. `neck`: the profile used inside neck
    areas; `fixed_cl`: the clearance to every net is `clearance` (neck profiles: the area's DRC rule)."""
    def __init__(self, name, width, clearance, via_d, via_drill, neck=None, fixed_cl=False):
        self.name, self.w, self.cl, self.via_d, self.via_drill = name, width, clearance, via_d, via_drill
        self.hw, self.vr = width / 2, via_d / 2
        self.neck, self.fixed_cl = neck, fixed_cl


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _poly_dist(px, py, pts):
    """Distance from points to a closed polygon (0 inside)."""
    pts = list(pts)
    d = np.full(px.shape, np.inf)
    inside = np.zeros(px.shape, dtype=bool)
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]; bx, by = pts[(i + 1) % n]
        d = np.minimum(d, _seg_dist(px, py, ax, ay, bx, by))
        cond = ((ay > py) != (by > py))
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (bx - ax) * (py - ay) / (by - ay + 1e-300) + ax
        inside ^= cond & (px < xint)
    d[inside] = 0.0
    return d


def _neck_margin(pieces, m=0.1):
    """Carry the narrow (neck) width `m` mm past a neck-area edge: the wide track's round end must
    not reach into the area, where its neighbours sit at the neck clearance."""
    out = [list(x) for x in pieces]
    for k in range(len(out) - 1):
        (p0, q0, in0), (p1, q1, in1) = out[k], out[k + 1]
        if in0 == in1:
            continue
        outside = out[k + 1] if in0 else out[k]
        L = math.hypot(outside[1][0] - outside[0][0], outside[1][1] - outside[0][1])
        if L < 1e-9:
            continue
        d = m if L >= 2 * m else L               # a short outside piece is narrow all the way
        ux, uy = (outside[1][0] - outside[0][0]) / L, (outside[1][1] - outside[0][1]) / L
        if in0:                                  # inside -> outside: move the split forward
            j = (q0[0] + ux * d, q0[1] + uy * d)
        else:                                    # outside -> inside: move it back
            j = (p1[0] - ux * d, p1[1] - uy * d)
        j = (round(j[0], 4), round(j[1], 4))
        out[k][1] = j; out[k + 1][0] = j
    return [tuple(x) for x in out]


def clip_rects(a, b, rects):
    """Split segment a-b into [(p, q, inside_any_rect)] pieces (rects: (x0, y0, x1, y1))."""
    ts = {0.0, 1.0}
    dx, dy = b[0] - a[0], b[1] - a[1]
    for x0, y0, x1, y1 in rects:
        for v, d, lo, hi in ((a[0], dx, x0, x1), (a[1], dy, y0, y1)):
            if abs(d) > 1e-12:
                for bound in (lo, hi):
                    t = (bound - v) / d
                    if 0 < t < 1:
                        ts.add(t)
    ts = sorted(ts)
    out = []
    for t0, t1 in zip(ts, ts[1:]):
        tm = (t0 + t1) / 2
        mx, my = a[0] + dx * tm, a[1] + dy * tm
        ins = any(x0 <= mx <= x1 and y0 <= my <= y1 for x0, y0, x1, y1 in rects)
        p = (round(a[0] + dx * t0, 4), round(a[1] + dy * t0, 4)); q = (round(a[0] + dx * t1, 4), round(a[1] + dy * t1, 4))
        if out and out[-1][2] == ins:
            out[-1] = (out[-1][0], q, ins)
        else:
            out.append((p, q, ins))
    return out


class Board:
    def __init__(self, dump, profiles, class_clearance, net_class, necks=(), layers=None):
        self.d = dump
        self.layers = tuple(layers or LAYERS)         # the routing layers, top to bottom
        ol = dump["outline"][0]["outline"]
        xs = [p[0] for p in ol]; ys = [p[1] for p in ol]
        self.x0, self.y0 = min(xs) - 1.0, min(ys) - 1.0
        self.nx = int(math.ceil((max(xs) + 1.0 - self.x0) / RES)) + 1
        self.ny = int(math.ceil((max(ys) + 1.0 - self.y0) / RES)) + 1
        self.N = self.nx * self.ny
        self.profiles = profiles
        self.class_cl = class_clearance          # net class -> clearance to other nets (mm)
        self.net_class = net_class               # net name -> class
        self.netcode, self.netname = {}, {}
        for p in dump["pads"]:
            if p["net"]:
                self.code(p["net"])
        self.T = {pr: [np.full((self.ny, self.nx), -1, dtype=np.int32) for _ in self.layers] for pr in profiles}
        self.V = {pr: [np.full((self.ny, self.nx), -1, dtype=np.int32) for _ in self.layers] for pr in profiles}
        gx = self.x0 + np.arange(self.nx) * RES
        gy = self.y0 + np.arange(self.ny) * RES
        self.GX, self.GY = np.meshgrid(gx, gy)
        self.necks = list(necks)                 # (x0, y0, x1, y1) rectangles
        self.neck_mask = np.zeros((self.ny, self.nx), dtype=bool)
        self.via_ok = np.ones((self.ny, self.nx), dtype=bool)      # via columns inside neck areas
        for x0, y0, x1, y1 in self.necks:
            self.neck_mask |= self._rect(x0, y0, x1, y1)
        self._outline(ol, dump["outline"][0]["holes"])
        self.T0 = self.V0 = None

    def _rect(self, x0, y0, x1, y1):
        return (self.GX >= x0 - 1e-6) & (self.GX <= x1 + 1e-6) & (self.GY >= y0 - 1e-6) & (self.GY <= y1 + 1e-6)

    # ---------------------------------------------------------------- coordinates
    def cell(self, x, y):
        return int(round((y - self.y0) / RES)), int(round((x - self.x0) / RES))

    def xy(self, j, i):
        return (round(self.x0 + i * RES, 4), round(self.y0 + j * RES, 4))

    def idx(self, x, y):
        j, i = self.cell(x, y)
        return j * self.nx + i

    def code(self, net):
        if net not in self.netcode:
            self.netcode[net] = len(self.netcode); self.netname[self.netcode[net]] = net
        return self.netcode[net]

    def clearance(self, net_a_class, prof):
        pr = self.profiles[prof]
        if pr.fixed_cl:
            return pr.cl
        return max(self.class_cl.get(net_a_class, 0.15), pr.cl)

    def via_columns(self, rect, xs):
        """Inside `rect`, vias only on the given x columns (mm)."""
        m = self._rect(*rect)
        col = np.zeros_like(m)
        for x in xs:
            col |= np.abs(self.GX - x) < RES / 2
        self.via_ok &= ~m | col

    # ---------------------------------------------------------------- stamping
    def _window(self, xmin, ymin, xmax, ymax):
        j0 = max(0, int((ymin - self.y0) / RES) - 1); j1 = min(self.ny, int((ymax - self.y0) / RES) + 2)
        i0 = max(0, int((xmin - self.x0) / RES) - 1); i1 = min(self.nx, int((xmax - self.x0) / RES) + 2)
        return j0, j1, i0, i1

    def _mark(self, arr, sl, mask, code):
        sub = arr[sl]
        if code < 0:
            sub[mask] = code
            return
        free = mask & (sub == -1)
        other = mask & (sub >= 0) & (sub != code)
        sub[free] = code
        sub[other] = -2

    def stamp_window(self, cls, kind, geom, extra=0.0):
        """The cells (j0, j1, i0, i1) a stamp of this copper can touch."""
        if kind == "poly":
            xs = [p[0] for p in geom]; ys = [p[1] for p in geom]
            bb = (min(xs), min(ys), max(xs), max(ys))
        elif kind == "seg":
            ax, ay, bx, by, h = geom
            bb = (min(ax, bx) - h, min(ay, by) - h, max(ax, bx) + h, max(ay, by) + h)
        else:
            cx, cy, r = geom
            bb = (cx - r, cy - r, cx + r, cy + r)
        key = (cls, extra)
        rmax = self._rmax.get(key) if hasattr(self, "_rmax") else None
        if rmax is None:
            rmax = max(max(self.clearance(cls, p) + max(pr.hw, pr.vr) for p, pr in self.profiles.items()), 0) + extra + RES
            if not hasattr(self, "_rmax"):
                self._rmax = {}
            self._rmax[key] = rmax
        return self._window(bb[0] - rmax, bb[1] - rmax, bb[2] + rmax, bb[3] + rmax)

    def stamp(self, net, cls, layers, kind, geom, extra=0.0, via_only=False, block_all_vias=False, bare=False):
        """Mark cells near copper of `net` for every profile. kind: 'poly' (list of pts), 'seg'
        (ax, ay, bx, by, halfwidth), 'circle' (cx, cy, r). bare: `extra` from the copper's edge, no clearance (a
        margin that is not a clearance to another net: no via touching an SMD pad)."""
        code = self.code(net) if net else -2
        j0, j1, i0, i1 = self.stamp_window(cls, kind, geom, extra)
        if kind == "seg":
            ax, ay, bx, by, h = geom
        elif kind == "circle":
            cx, cy, r = geom
        sl = (slice(j0, j1), slice(i0, i1))
        PX, PY = self.GX[sl], self.GY[sl]
        if kind == "poly":
            dist = _poly_dist(PX, PY, geom)
        elif kind == "seg":
            dist = np.maximum(_seg_dist(PX, PY, ax, ay, bx, by) - h, 0)
        else:
            dist = np.maximum(np.hypot(PX - cx, PY - cy) - r, 0)
        for pname, pr in self.profiles.items():
            cl = extra if bare else self.clearance(cls, pname) + extra
            for li, lname in enumerate(self.layers):
                if lname not in layers:
                    continue
                if not via_only:
                    self._mark(self.T[pname][li], sl, dist < cl + pr.hw - 1e-6, code)
                vcode = -2 if block_all_vias else code
                vthr = cl + pr.vr
                hc = getattr(self, "hole_cl", 0.0)
                if hc and not bare:                       # a via's hole keeps the board's hole clearance from it too
                    vthr = max(vthr, hc + pr.via_drill / 2 + extra)
                self._mark(self.V[pname][li], sl, dist < vthr - 1e-6, vcode)

    def _outline(self, ol, holes, edge_cl=0.3):
        inside = _poly_dist(self.GX, self.GY, ol) == 0
        dist_edge = np.full(self.GX.shape, np.inf)
        n = len(ol)
        for i in range(n):
            ax, ay = ol[i]; bx, by = ol[(i + 1) % n]
            dist_edge = np.minimum(dist_edge, _seg_dist(self.GX, self.GY, ax, ay, bx, by))
        for pname, pr in self.profiles.items():
            for li in range(len(self.layers)):
                self.T[pname][li][(~inside) | (dist_edge < edge_cl + pr.hw)] = -2
                self.V[pname][li][(~inside) | (dist_edge < edge_cl + pr.vr)] = -2

    def stamp_pads(self):
        for p in self.d["pads"]:
            cls = self.net_class.get(p["net"], "Default")
            smd = len(p["layers"]) == 1
            for lname, pl in p["shape"].items():
                for poly in pl:
                    self.stamp(p["net"] or f"__nc_{p['ref']}_{p['num']}", cls, [lname], "poly", poly["outline"])
                    # no vias in pads, whatever the net (SMD: solder wicking; plated pads: pointless and
                    # the drill would cut the pad)
                    self.stamp("", cls, [lname], "poly", poly["outline"], via_only=True, block_all_vias=True,
                               extra=0.05 if smd else 0.0, bare=True)          # another net's pad: its clearance above
            if p["drill"] > 0:  # through holes: block vias on both layers around the hole too
                self.stamp(p["net"] or f"__nc_{p['ref']}_{p['num']}", cls, list(self.layers), "circle",
                           (p["pos"][0], p["pos"][1], p["drill"] / 2 + 0.1), via_only=True)
                # hole-to-hole 0.25 mm for vias of any net, same-net ones included (no via in a plated hole)
                self.stamp("", cls, list(self.layers), "circle", (p["pos"][0], p["pos"][1], p["drill"] / 2 + 0.25),
                           via_only=True, block_all_vias=True)

    def stamp_rules(self):
        for r in self.d["rules"]:
            if not (r["no_tracks"] or r["no_vias"]):
                continue
            layers = [l for l in r["layers"] if l in self.layers]
            for poly in r["poly"]:
                pts = poly["outline"]
                xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
                j0, j1, i0, i1 = self._window(min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
                sl = (slice(j0, j1), slice(i0, i1))
                dist = _poly_dist(self.GX[sl], self.GY[sl], pts)
                for pname, pr in self.profiles.items():
                    for li, lname in enumerate(self.layers):
                        if lname not in layers:
                            continue
                        if r["no_tracks"]:
                            self.T[pname][li][sl][dist < pr.hw + 0.01] = -2
                        if r["no_vias"]:
                            self.V[pname][li][sl][dist < pr.vr + 0.01] = -2

    def stamp_track(self, net, layer, a, b, w):
        cls = self.net_class.get(net, "Default")
        self.stamp(net, cls, [layer], "seg", (a[0], a[1], b[0], b[1], w / 2))

    def stamp_via(self, net, pos, d, layers=None, drill=None):
        """A via's copper; layers: the ones a microvia or blind via spans (None: a through via, every layer). The
        router's own vias go through every layer, so one on any spanned layer is blocked there as well. With the
        board's hole-to-hole minimum (self.h2h) and the drill, no other via's hole comes nearer than that."""
        cls = self.net_class.get(net, "Default")
        hc = getattr(self, "hole_cl", 0.0)
        if hc:                                            # its hole: other copper keeps the board's hole clearance from it
            dr = drill if drill else d * 0.45
            ls_h = list(self.layers) if layers is None else [l for l in layers if l in self.layers]
            if ls_h:
                self.stamp(net, cls, ls_h, "circle", (pos[0], pos[1], dr / 2), extra=hc, bare=True)
        h2h = getattr(self, "h2h", 0.0)
        if h2h and layers is None:
            dr = drill if drill else d * 0.45
            for pname, pr in self.profiles.items():
                r = dr / 2 + h2h + pr.via_drill / 2
                j0, j1, i0, i1 = self._window(pos[0] - r, pos[1] - r, pos[0] + r, pos[1] + r)
                sl = (slice(j0, j1), slice(i0, i1))
                m = np.hypot(self.GX[sl] - pos[0], self.GY[sl] - pos[1]) < r - 1e-6
                for li in range(len(self.layers)):
                    self._mark(self.V[pname][li], sl, m, -2)
        ls = list(self.layers) if layers is None else [l for l in layers if l in self.layers]
        if ls:
            self.stamp(net, cls, ls, "circle", (pos[0], pos[1], d / 2))
        elif layers is not None:                      # spans no routing layer (planes only): it still takes the column
            self.stamp(net, cls, list(self.layers), "circle", (pos[0], pos[1], d / 2), via_only=True)

    def reserve(self, net, layers, pts):
        """Keep an area for a pour of `net` (other nets route around it)."""
        cls = self.net_class.get(net, "Default")
        self.stamp(net, cls, layers, "poly", pts)

    def snapshot(self):
        """Remember the fixed copper; Router.rebuild() returns to it and re-stamps the routes."""
        self.T0 = {k: [a.copy() for a in v] for k, v in self.T.items()}
        self.V0 = {k: [a.copy() for a in v] for k, v in self.V.items()}

    def restore(self):
        for k in self.T:
            for li in range(len(self.layers)):
                self.T[k][li][...] = self.T0[k][li]
                self.V[k][li][...] = self.V0[k][li]

    def restore_window(self, j0, j1, i0, i1):
        sl = (slice(j0, j1), slice(i0, i1))
        for k in self.T:
            for li in range(len(self.layers)):
                self.T[k][li][sl] = self.T0[k][li][sl]
                self.V[k][li][sl] = self.V0[k][li][sl]

    # ---------------------------------------------------------------- legality for one net
    def legal(self, net, prof, static=False):
        """(lt [layers, N] uint8 track-legal cells per layer, lv [N] uint8 via-legal cells: every layer) for `net`;
        neck areas use the neck profile. static: ignore routed nets (fixed copper only)."""
        c = self.code(net)
        T, V = (self.T0, self.V0) if static else (self.T, self.V)
        pr = self.profiles[prof]

        def ok(arrs):
            return [((a == -1) | (a == c)) for a in arrs]
        t = ok(T[prof]); v = ok(V[prof])
        if pr.neck:
            tn = ok(T[pr.neck]); vn = ok(V[pr.neck])
            t = [np.where(self.neck_mask, a, b) for a, b in zip(tn, t)]
            v = [np.where(self.neck_mask, a, b) for a, b in zip(vn, v)]
        lt = np.stack([a.ravel() for a in t]).astype(np.uint8)
        lv = self.via_ok.copy()
        for a in v:
            lv &= a
        lv = lv.ravel()
        nv = getattr(self, "no_vias", {}).get(net)
        if nv is not None:
            lv = lv & ~nv
        return lt, lv.astype(np.uint8)

    def forbid_vias(self, net, pts):
        """No vias of `net` inside the polygon (the GNSS lines near the antenna, say)."""
        if not hasattr(self, "no_vias"):
            self.no_vias = {}
        m = self.no_vias.setdefault(net, np.zeros(self.N, dtype=bool))
        xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
        j0, j1, i0, i1 = self._window(min(xs), min(ys), max(xs), max(ys))
        sl = (slice(j0, j1), slice(i0, i1))
        inside = _poly_dist(self.GX[sl], self.GY[sl], pts) == 0
        mm = m.reshape(self.ny, self.nx)
        mm[sl] |= inside


class Router:
    def __init__(self, board, bend45=40, bend90=140, via=500, bcu_cross=1.12, max_expand=6_000_000,
                 hweight=1.0, margin_mm=14.0, directions=None):
        self.B = board
        self.hweight, self.margin = hweight, int(margin_mm / RES)
        self.bend45, self.bend90, self.via_cost = bend45, bend90, via
        self.bcu_cross, self.max_expand = bcu_cross, max_expand
        self.fcu_cross = 1.0                     # v2 sets F.Cu to prefer runs along y (layer directions)
        self.directions = directions             # layer -> "x" | "y" | "any" (a stack-up plan), else F.Cu / B.Cu as above
        self.routes = []     # dicts: net, profile, segments [(layer, a, b, w)], vias [(pos, d, drill)], fixed
        self._on_grid = {}   # id -> (route, its stamp windows): the routed copper the grid holds now
        self.last_status = 0
        self.hist = np.zeros(len(board.layers) * board.N, dtype=np.float32)    # PathFinder history: contested cells
        self.use_hist = False

    # ------------------------------------------------------------------ A*
    def layer_costs(self, fcu_factor=1.0):
        """Per routing layer: (cost factor, factor for moves not along x, factor for moves not along y)."""
        out = []
        for li, l in enumerate(self.B.layers):
            f = fcu_factor if l == "F.Cu" else 1.0
            if self.directions is not None:
                d = self.directions.get(l, "any")
                out.append((f, self.bcu_cross if d == "x" else 1.0, self.bcu_cross if d == "y" else 1.0))
            else:
                out.append((f, self.bcu_cross if l == "B.Cu" else 1.0, self.fcu_cross if l == "F.Cu" else 1.0))
        return out

    def astar(self, net, prof, sources, targets, fcu_factor=1.0, allow_vias=True, _legal=None, cell_cost=None,
              margin=None):
        """C search core: [(layer, idx), ...] source -> target, or None."""
        if not _lib():
            return self.astar_py(net, prof, sources, targets, fcu_factor, allow_vias, _legal)
        B = self.B; nx = B.nx; N = B.N
        lt, lv = _legal if _legal else B.legal(net, prof)
        if not targets or not sources:
            return None
        nl = len(B.layers)
        tmask = np.zeros(nl * N, dtype=np.uint8)
        tmask[np.array([l * N + idx for l, idx in targets], dtype=np.int64)] = 1
        tj = [t[1] // nx for t in targets]; ti = [t[1] % nx for t in targets]
        sj = [idx // nx for _, idx in sources]; si = [idx % nx for _, idx in sources]
        m = self.margin if margin is None else int(margin / RES)
        wj0 = max(1, min(min(sj), min(tj)) - m); wj1 = min(B.ny - 2, max(max(sj), max(tj)) + m)
        wi0 = max(1, min(min(si), min(ti)) - m); wi1 = min(nx - 2, max(max(si), max(ti)) + m)
        src = np.array([l * N + idx for l, idx in sources], dtype=np.int32)
        cap = 400000
        out = np.zeros(cap, dtype=np.int32)
        cc = None
        if cell_cost is not None:
            cc = np.ascontiguousarray(cell_cost, dtype=np.float32)
        lc = np.array(self.layer_costs(fcu_factor), dtype=np.float32)
        n = _lib().astar(nx, B.ny, nl, np.ascontiguousarray(lt).ravel(), np.ascontiguousarray(lv),
                         cc.ctypes.data if cc is not None else None, src, len(src), tmask,
                         min(ti), max(ti), min(tj), max(tj), wi0, wi1, wj0, wj1,
                         self.bend45, self.bend90, self.via_cost, np.ascontiguousarray(lc[:, 0]), np.ascontiguousarray(lc[:, 1]),
                         np.ascontiguousarray(lc[:, 2]), self.hweight, 1 if allow_vias else 0, self.max_expand, out, cap)
        self.last_status = int(n)
        if n <= 0:
            return None
        return [(int(v) // N, int(v) % N) for v in out[:n]]

    def astar_py(self, net, prof, sources, targets, fcu_factor=1.0, allow_vias=True, _legal=None):
        """Pure-python reference of the C core (tests)."""
        B = self.B; nx = B.nx; N = B.N
        lt, lv = _legal if _legal else B.legal(net, prof)
        tset = set(targets)
        if not tset:
            return None
        tj = [t[1] // nx for t in tset]; ti = [t[1] % nx for t in tset]
        tj0, tj1, ti0, ti1 = min(tj), max(tj), min(ti), max(ti)
        step = [10, 19, 10, 19, 10, 19, 10, 19]      # astar.c DG
        offs = [dx + dy * nx for dx, dy in DIRS]
        lcost = self.layer_costs(fcu_factor)
        nl = len(B.layers)
        W = self.hweight

        tlayers = {l for l, _ in tset}
        lh = [0.0 if (l in tlayers or not allow_vias) else W * self.via_cost for l in range(nl)]

        def h(idx, l=None):
            j, i = divmod(idx, nx)
            dx = 0 if ti0 <= i <= ti1 else min(abs(i - ti0), abs(i - ti1))
            dy = 0 if tj0 <= j <= tj1 else min(abs(j - tj0), abs(j - tj1))
            return W * (10 * max(dx, dy) + 9 * min(dx, dy)) + (lh[l] if l is not None else 0.0)
        sj = [idx // nx for _, idx in sources]; si = [idx % nx for _, idx in sources]
        wj0 = max(1, min(min(sj), tj0) - self.margin); wj1 = min(B.ny - 2, max(max(sj), tj1) + self.margin)
        wi0 = max(1, min(min(si), ti0) - self.margin); wi1 = min(nx - 2, max(max(si), ti1) + self.margin)
        g = {}; came = {}; heap = []
        for (l, idx) in sources:
            s = (l * N + idx) * 9 + 8
            if lt[l][idx] and g.get(s, 1e18) > 0:
                g[s] = 0; came[s] = None
                heapq.heappush(heap, (h(idx, l), 0, s))
        expanded = 0
        while heap:
            f, gs, s = heapq.heappop(heap)
            if gs > g.get(s, 1e18):
                continue
            ld, d = divmod(s, 9)
            l, idx = divmod(ld, N)
            if (l, idx) in tset:
                path = []
                while s is not None:
                    ld, d = divmod(s, 9); l, idx = divmod(ld, N)
                    path.append((l, idx))
                    s = came[s]
                return path[::-1]
            expanded += 1
            if expanded > self.max_expand:
                return None
            ltl = lt[l]
            for nd in range(8):
                if d != 8:
                    turn = (nd - d) % 8
                    if turn in (3, 4, 5):
                        continue
                    bend = 0 if turn == 0 else (self.bend45 if turn in (1, 7) else self.bend90)
                else:
                    bend = 0
                ni = idx + offs[nd]
                if not ltl[ni]:
                    continue
                nj_, ni_ = divmod(ni, nx)
                if nj_ < wj0 or nj_ > wj1 or ni_ < wi0 or ni_ > wi1:
                    continue
                if nd & 1:
                    dx, dy = DIRS[nd]
                    if not (ltl[idx + dx] and ltl[idx + dy * nx]):
                        continue
                c = step[nd] * lcost[l][0]
                if nd not in (0, 4):
                    c *= lcost[l][1]
                if nd not in (2, 6):
                    c *= lcost[l][2]
                ns = (l * N + ni) * 9 + nd
                ng = gs + c + bend
                if ng < g.get(ns, 1e18):
                    g[ns] = ng; came[ns] = s
                    heapq.heappush(heap, (ng + h(ni, l), ng, ns))
            if allow_vias and lv[idx]:
                for ol in range(nl):
                    if ol == l:
                        continue
                    ns = (ol * N + idx) * 9 + 8
                    ng = gs + self.via_cost
                    if lt[ol][idx] and ng < g.get(ns, 1e18):
                        g[ns] = ng; came[ns] = s
                        heapq.heappush(heap, (ng + h(idx, ol), ng, ns))
        return None

    # ------------------------------------------------------------------ path -> geometry
    def _runs(self, path):
        """Split a cell path into per-layer runs [(layer, [(j, i), ...])] with vias between them."""
        runs = []; nx = self.B.nx
        for l, idx in path:
            j, i = divmod(idx, nx)
            if not runs or runs[-1][0] != l:
                runs.append((l, [(j, i)]))
            else:
                if runs[-1][1][-1] != (j, i):
                    runs[-1][1].append((j, i))
        return runs

    def _seg_ok(self, lt, a, b):
        """Octilinear segment between cells a, b (j, i) legal?"""
        (j0, i0), (j1, i1) = a, b
        dj, di = j1 - j0, i1 - i0
        n = max(abs(dj), abs(di))
        if n == 0:
            return True
        if not (dj == 0 or di == 0 or abs(dj) == abs(di)):
            return False
        sj, si = (dj > 0) - (dj < 0), (di > 0) - (di < 0)
        nx = self.B.nx
        for k in range(n + 1):
            j, i = j0 + sj * k, i0 + si * k
            if not lt[j * nx + i]:
                return False
            if sj and si and k < n:
                if not (lt[j * nx + i + si] and lt[(j + sj) * nx + i]):
                    return False
        return True

    def _dogleg(self, lt, a, b, chamfer=10):
        """Octilinear connection a -> b the way it would be drawn: straight if possible; else an
        orthogonal L with a short 45-degree chamfer (chamfer cells); only then a long diagonal dogleg."""
        (j0, i0), (j1, i1) = a, b
        dj, di = j1 - j0, i1 - i0
        if dj == 0 or di == 0 or abs(dj) == abs(di):
            return [a, b] if self._seg_ok(lt, a, b) else None
        sj, si = (dj > 0) - (dj < 0), (di > 0) - (di < 0)
        m = min(abs(dj), abs(di))
        c = min(chamfer, m)
        if m > chamfer:
            for first_h in (True, False):
                if first_h:
                    p1 = (j0, i1 - si * c); p2 = (j0 + sj * c, i1)
                else:
                    p1 = (j1 - sj * c, i0); p2 = (j1, i0 + si * c)
                if self._seg_ok(lt, a, p1) and self._seg_ok(lt, p1, p2) and self._seg_ok(lt, p2, b):
                    return [a, p1, p2, b]
        c1 = (j0 + sj * m, i0 + si * m)                  # diagonal first
        c2 = (j1 - sj * m, i1 - si * m)                  # straight first
        for c_ in (c2, c1):
            if self._seg_ok(lt, a, c_) and self._seg_ok(lt, c_, b):
                return [a, c_, b]
        return None

    @staticmethod
    def _angle_ok(p, q, r):
        """Interior angle at q is >= 90 degrees (no acute corners)."""
        v1 = (p[0] - q[0], p[1] - q[1]); v2 = (r[0] - q[0], r[1] - q[1])
        return v1[0] * v2[0] + v1[1] * v2[1] <= 0

    def _pull_tight(self, lt, pts):
        pts = [p for k, p in enumerate(pts) if k == 0 or p != pts[k - 1]]
        if len(pts) <= 2:
            return pts
        out = [pts[0]]; i = 0
        while i < len(pts) - 1:
            best = None
            for j in range(len(pts) - 1, i, -1):
                dg = self._dogleg(lt, pts[i], pts[j])
                if dg is None:
                    continue
                if len(out) >= 2 and not self._angle_ok(out[-2], out[-1], dg[1]):
                    continue
                best = (j, dg)
                break
            if best is None:
                best = (i + 1, [pts[i], pts[i + 1]])
            j, dg = best
            out = out[:-1] + dg
            i = j
        simp = [out[0]]
        for k in range(1, len(out) - 1):
            a, b, c = simp[-1], out[k], out[k + 1]
            if (b[0] - a[0]) * (c[1] - b[1]) == (b[1] - a[1]) * (c[0] - b[0]):
                continue
            simp.append(b)
        simp.append(out[-1])
        return simp

    def cells_of(self, segs):
        """Cells under committed octilinear segments (sources for T-junctions)."""
        B = self.B; out = []
        for lname, a, b, w in segs:
            if lname not in B.layers:
                continue
            l = B.layers.index(lname)
            (j0, i0), (j1, i1) = B.cell(*a), B.cell(*b)
            n = max(abs(j1 - j0), abs(i1 - i0), 1)
            for k in range(n + 1):
                j = j0 + round((j1 - j0) * k / n); i = i0 + round((i1 - i0) * k / n)
                out.append((l, j * B.nx + i))
        return out

    def connect(self, net, prof, sources, targets, fcu_factor=1.0, allow_vias=True, cell_cost=None, margin=None, layers=None,
                keep_to=None, escape=None):
        """Route one connection; returns the committed route record or None. layers: only these routing layers;
        escape: [(layer index, cell indices)] kept open off those layers (a pad's way to its via); keep_to: [layers, N]
        cells the path may be pulled tight over (with its own): a sketch's band."""
        lt, lv = self.B.legal(net, prof)
        if layers is not None:
            keep = [li for li, l in enumerate(self.B.layers) if l in layers]
            if keep and len(keep) < len(self.B.layers):
                full = lt
                lt = lt.copy()
                for li in range(len(self.B.layers)):
                    if li not in keep:
                        lt[li] = 0
                for li, idx in escape or ():
                    lt[li][idx] = full[li][idx]
        if cell_cost is None and self.use_hist:
            cell_cost = self.hist
        path = self.astar(net, prof, sources, targets, fcu_factor, allow_vias, _legal=(lt, lv), cell_cost=cell_cost,
                          margin=margin)
        if path is None and self.last_status == -2:        # search budget ran out: weighted retry, more budget
            hw, mx = self.hweight, self.max_expand
            self.hweight, self.max_expand = max(1.25, hw), 4 * mx
            try:
                path = self.astar(net, prof, sources, targets, fcu_factor, allow_vias, _legal=(lt, lv),
                                  cell_cost=cell_cost, margin=margin)
            finally:
                self.hweight, self.max_expand = hw, mx
        if path is None:
            return None
        if keep_to is not None:                          # pulled tight only within the band (and over its own cells)
            lt = lt & keep_to.astype(np.uint8)
            for l, idx in path:
                lt[l][idx] = 1
        return self.commit(net, prof, path, lt)

    def geometry(self, prof, path, lt):
        """Cell path -> (segments, vias), segments split at neck-area edges."""
        B = self.B; pr = B.profiles[prof]
        wn = B.profiles[pr.neck].w if pr.neck else pr.w
        runs = self._runs(path)
        segs, vias = [], []
        for k, (l, cells) in enumerate(runs):
            pts = self._pull_tight(lt[l], cells)
            xy = [B.xy(j, i) for j, i in pts]
            for a, b in zip(xy, xy[1:]):
                if B.necks and pr.neck:
                    for p, q, ins in _neck_margin(clip_rects(a, b, B.necks)):
                        if p != q:
                            segs.append((B.layers[l], p, q, wn if ins else pr.w))
                else:
                    segs.append((B.layers[l], a, b, pr.w))
            if k < len(runs) - 1:
                v = pr
                if pr.neck and B.necks:                   # inside a neck area: the neck's via (it was checked with it)
                    j, i = pts[-1]
                    if B.neck_mask[j, i]:
                        v = B.profiles[pr.neck]
                vias.append((xy[-1], v.via_d, v.via_drill))
        return segs, vias

    def commit(self, net, prof, path, lt):
        """Turn a cell path into tracks and vias, stamp them, and record the route."""
        segs, vias = self.geometry(prof, path, lt)
        return self.add(net, prof, segs, vias)

    def add(self, net, prof, segs, vias, fixed=False):
        B = self.B
        for lname, a, b, w in segs:
            B.stamp_track(net, lname, a, b, w)
        for pos, dv, dr in vias:
            B.stamp_via(net, pos, dv, drill=dr)
        rec = {"net": net, "profile": prof, "segments": segs, "vias": vias, "fixed": fixed}
        self.routes.append(rec)
        self._on_grid[id(rec)] = (rec, self._windows(rec))
        return rec

    def _windows(self, rec):
        B = self.B
        cls = B.net_class.get(rec["net"], "Default")
        out = [B.stamp_window(cls, "seg", (a[0], a[1], b[0], b[1], w / 2)) for _, a, b, w in rec["segments"]]
        out += [B.stamp_window(cls, "circle", (pos[0], pos[1], dv / 2)) for pos, dv, _ in rec["vias"]]
        return out

    def _stamp(self, rec):
        B = self.B
        for lname, a, b, w in rec["segments"]:
            B.stamp_track(rec["net"], lname, a, b, w)
        for pos, dv, dr in rec["vias"]:
            B.stamp_via(rec["net"], pos, dv, drill=dr)

    # ------------------------------------------------------------------ rip-up and reroute
    def rip(self, nets):
        """Remove every (non-fixed) route of these nets; call rebuild() afterwards."""
        nets = set(nets)
        self.routes = [r for r in self.routes if r["net"] not in nets or r.get("fixed")]

    def rebuild(self, full=False):
        """Bring the grid in line with self.routes. Where copper left, the fixed copper is restored and
        every route that reaches there is stamped again; new copper is stamped. Stamps are idempotent
        and do not depend on order, so the rest of the grid stays as it is."""
        B = self.B
        now = {id(r): r for r in self.routes}
        gone = [w for k, (r, w) in self._on_grid.items() if k not in now]
        kept = {k: v for k, v in self._on_grid.items() if k in now}
        rects = [x for w in gone for x in w]
        if full or sum((j1 - j0) * (i1 - i0) for j0, j1, i0, i1 in rects) > 0.3 * B.N:
            B.restore()
            redo = list(self.routes)
        else:
            for x in rects:
                B.restore_window(*x)
            if rects:
                R = np.array(rects)
                def near(w):
                    W = np.array(w)
                    return bool(((W[:, None, 0] < R[None, :, 1]) & (R[None, :, 0] < W[:, None, 1]) &
                                 (W[:, None, 2] < R[None, :, 3]) & (R[None, :, 2] < W[:, None, 3])).any())
                redo = [r for k, (r, w) in kept.items() if w and near(w)]
            else:
                redo = []
            redo += [r for k, r in now.items() if k not in kept]
        for r in redo:
            self._stamp(r)
        self._on_grid = {k: kept[k] if k in kept else (r, self._windows(r)) for k, r in now.items()}

    def blockers(self, net, prof, sources, targets, fcu_factor=1.0, penalty=60.0, margin=None, include_fixed=False,
                 layers=None, allow_vias=True):
        """Which routed nets stand in the way of this connection? Route on the fixed copper only, with
        a penalty for cells taken by routed nets, and return the nets owning those cells. layers: only these
        routing layers (a route kept to one layer asks who is in its way there)."""
        B = self.B
        lt_s, lv_s = B.legal(net, prof, static=True)
        lt_d, lv_d = B.legal(net, prof)
        if layers is not None:
            keep = [li for li, l in enumerate(B.layers) if l in layers]
            lt_s, lt_d = lt_s.copy(), lt_d.copy()
            for li in range(len(B.layers)):
                if li not in keep:
                    lt_s[li] = 0
                    lt_d[li] = 1                         # nothing there counts as in the way
        cc = np.where(lt_d == 0, penalty, 0.0).astype(np.float32).ravel()
        hw, mx = self.hweight, self.max_expand          # a rough answer is enough here: weighted, bigger budget
        self.hweight, self.max_expand = max(2.0, hw), 4 * mx
        try:
            path = self.astar(net, prof, sources, targets, fcu_factor, allow_vias, _legal=(lt_s, lv_s), cell_cost=cc,
                              margin=margin)
            if path is None and self.last_status == -2:     # still too dear: plain shortest path on fixed copper
                path = self.astar(net, prof, sources, targets, fcu_factor, allow_vias, _legal=(lt_s, lv_s), margin=margin)
        finally:
            self.hweight, self.max_expand = hw, mx
        if path is None:
            return None
        taken = [(l, idx) for l, idx in path if not lt_d[l][idx]]
        vias_at = [idx for (l0, idx), (l1, _) in zip(path, path[1:]) if l0 != l1 and not lv_d[idx]]
        self.last_taken = taken
        return self._route_owners(net, prof, taken, vias_at, include_fixed)

    def add_history(self, cells, amount=40.0, radius=3):
        """Contested cells (and their neighbourhood) get dearer for every later search."""
        B = self.B; N = B.N
        for l, idx in cells:
            j, i = divmod(idx, B.nx)
            for dj in range(-radius, radius + 1):
                for di in range(-radius, radius + 1):
                    jj, ii = j + dj, i + di
                    if 0 <= jj < B.ny and 0 <= ii < B.nx:
                        self.hist[l * N + jj * B.nx + ii] += amount
        self.use_hist = True

    def _route_owners(self, net, prof, cells, via_cells=(), include_fixed=False):
        """Nets of routed copper within reach of the given cells (frozen routes too if asked)."""
        B = self.B; pr = B.profiles[prof]
        pts = [(l, B.xy(idx // B.nx, idx % B.nx)) for l, idx in cells]
        vpts = [B.xy(idx // B.nx, idx % B.nx) for idx in via_cells]
        found = set()
        for r in self.routes:
            if r["net"] == net or (r.get("fixed") and not include_fixed):
                continue
            hit = False
            for lname, a, b, w in r["segments"]:
                if lname not in B.layers:
                    continue
                li = B.layers.index(lname)
                reach = w / 2 + max(pr.hw, pr.vr) + 0.25
                for l, (x, y) in pts:
                    if l != li:
                        continue
                    if float(_seg_dist(np.array(x), np.array(y), a[0], a[1], b[0], b[1])) < reach:
                        hit = True; break
                if not hit:
                    for x, y in vpts:
                        if float(_seg_dist(np.array(x), np.array(y), a[0], a[1], b[0], b[1])) < reach:
                            hit = True; break
                if hit:
                    break
            if not hit:
                for pos, dv, dr in r["vias"]:
                    for _, (x, y) in pts:
                        if math.hypot(x - pos[0], y - pos[1]) < dv / 2 + max(pr.hw, pr.vr) + 0.25:
                            hit = True; break
                    if hit:
                        break
            if hit:
                found.add(r["net"])
        return found


def pad_cells(board, pad, prof, shrink=0.04):
    """Cells a track of `prof` may start/end on inside a pad (per layer index)."""
    out = []
    for li, lname in enumerate(board.layers):
        if lname not in pad["shape"]:
            continue
        for poly in pad["shape"][lname]:
            pts = poly["outline"]
            xs = [q[0] for q in pts]; ys = [q[1] for q in pts]
            j0, j1, i0, i1 = board._window(min(xs), min(ys), max(xs), max(ys))
            sl = (slice(j0, j1), slice(i0, i1))
            inside = _inside(board.GX[sl], board.GY[sl], pts, shrink)
            jj, ii = np.nonzero(inside)
            for j, i in zip(jj + j0, ii + i0):
                out.append((li, int(j) * board.nx + int(i)))
            if not len(jj):            # tiny pad: nearest cell to the centre
                j, i = board.cell(pad["pos"][0], pad["pos"][1])
                out.append((li, j * board.nx + i))
    return out


def _inside(PX, PY, pts, shrink):
    d = _poly_dist(PX, PY, pts)
    inside = d == 0
    n = len(pts); db = np.full(PX.shape, np.inf)
    for k in range(n):
        ax, ay = pts[k]; bx, by = pts[(k + 1) % n]
        db = np.minimum(db, _seg_dist(PX, PY, ax, ay, bx, by))
    return inside & (db >= shrink)
