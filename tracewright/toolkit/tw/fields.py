"""Fields on the board: voltage drop and current density in a supply's copper, and the board's temperature from the
parts that get warm. Both are solved on a grid over the copper as it is (tracks, pads, pours, vias), with numpy alone:
a conjugate-gradient solve of the conduction equation.

    ir_drop(ctx, net, amps=None, loads=None, source=None)  -> {"layers": {layer: grid of mV drop}, "density": {...}, "loads": [...], ...}
    heat(ctx, sources=None, ambient=None, air="still")      -> {"grid": temperatures, "parts": [...], "sources": [...], ...}

Grids come back as {"x0", "y0", "cell", "nx", "ny"} plus row lists (None where there is no copper / no board).
"""
import math

import numpy as np

from . import geom

RHO = 1.72e-8               # ohm m, copper at 20 C
PLATING = 20e-6             # m, via barrel plating
K_CU, K_FR4 = 385.0, 0.8    # W/m K (FR-4 in its plane)


# ------------------------------------------------------------------ rasters
class Raster:
    """A grid of square cells over a box (mm). Cell (j, i) has its centre at (x0 + (i + .5) c, y0 + (j + .5) c)."""

    def __init__(self, box, cell):
        x0, y0, x1, y1 = box
        self.c = float(cell)
        self.x0, self.y0 = x0, y0
        self.nx = max(1, int(math.ceil((x1 - x0) / self.c)))
        self.ny = max(1, int(math.ceil((y1 - y0) / self.c)))
        self.xs = x0 + (np.arange(self.nx) + 0.5) * self.c
        self.ys = y0 + (np.arange(self.ny) + 0.5) * self.c

    def empty(self, dtype=bool):
        return np.zeros((self.ny, self.nx), dtype=dtype)

    def _rows(self, lo, hi):
        j0 = max(0, int(math.floor((lo - self.y0) / self.c - 0.5)))
        j1 = min(self.ny - 1, int(math.ceil((hi - self.y0) / self.c - 0.5)))
        return j0, j1

    def _cols(self, lo, hi):
        i0 = max(0, int(math.floor((lo - self.x0) / self.c - 0.5)))
        i1 = min(self.nx - 1, int(math.ceil((hi - self.x0) / self.c - 0.5)))
        return i0, i1

    def poly(self, mask, pts):
        """Fill the polygon (even-odd) into mask: cells whose centres lie inside."""
        P = np.asarray(pts, float)
        if len(P) < 3:
            return
        xa, ya = P[:, 0], P[:, 1]
        xb, yb = np.roll(xa, -1), np.roll(ya, -1)
        j0, j1 = self._rows(ya.min(), ya.max())
        dy = np.where(yb == ya, 1e-12, yb - ya)
        for j in range(j0, j1 + 1):
            y = self.ys[j]
            hit = (ya <= y) != (yb <= y)
            if not hit.any():
                continue
            xc = np.sort(xa[hit] + (y - ya[hit]) * (xb[hit] - xa[hit]) / dy[hit])
            for a, b in zip(xc[0::2], xc[1::2]):
                i0 = int(math.ceil((a - self.x0) / self.c - 0.5))
                i1 = int(math.floor((b - self.x0) / self.c - 0.5))
                if i1 >= i0:
                    mask[j, max(0, i0):min(self.nx, i1 + 1)] = True

    def seg(self, mask, a, b, w, exact=False):
        """Cells within w/2 of the segment a-b (at least the cells the centre line crosses, unless exact)."""
        r = w / 2 if exact else max(w / 2, self.c * 0.5)
        j0, j1 = self._rows(min(a[1], b[1]) - r, max(a[1], b[1]) + r)
        i0, i1 = self._cols(min(a[0], b[0]) - r, max(a[0], b[0]) + r)
        if j1 < j0 or i1 < i0:
            return
        X, Y = np.meshgrid(self.xs[i0:i1 + 1], self.ys[j0:j1 + 1])
        ax, ay, bx, by = a[0], a[1], b[0], b[1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        t = np.clip(((X - ax) * vx + (Y - ay) * vy) / L2, 0, 1) if L2 > 0 else 0.0
        d = np.hypot(X - (ax + t * vx), Y - (ay + t * vy))
        mask[j0:j1 + 1, i0:i1 + 1] |= d <= r + 1e-9

    def disk(self, mask, c, r):
        self.seg(mask, c, c, 2 * r)

    def cell_of(self, x, y):
        i = int((x - self.x0) / self.c)
        j = int((y - self.y0) / self.c)
        return min(max(j, 0), self.ny - 1), min(max(i, 0), self.nx - 1)

    def frame(self):
        return {"x0": round(self.x0, 4), "y0": round(self.y0, 4), "cell": self.c, "nx": self.nx, "ny": self.ny}


def _finer(R, k):
    """A raster k times finer over exactly R's cells."""
    F = Raster((R.x0, R.y0, R.x0 + R.nx * R.c, R.y0 + R.ny * R.c), R.c / k)
    F.nx, F.ny = R.nx * k, R.ny * k
    F.xs = F.x0 + (np.arange(F.nx) + 0.5) * F.c
    F.ys = F.y0 + (np.arange(F.ny) + 0.5) * F.c
    return F


def _rows_out(a, mask, nd=2):
    """A grid as row lists for JSON, None off the mask."""
    return [[(round(float(v), nd) if m else None) for v, m in zip(r, mr)] for r, mr in zip(a, mask)]


# ------------------------------------------------------------------ the solver
def _components(n, ei, ej):
    """Connected component label per node (min node index of its component), by vectorised union-find."""
    parent = np.arange(n)
    if not len(ei):
        return parent
    while True:
        pi, pj = parent[ei], parent[ej]
        lo, hi = np.minimum(pi, pj), np.maximum(pi, pj)
        changed = lo != hi
        if not changed.any():
            break
        np.minimum.at(parent, hi[changed], lo[changed])
        while True:                                   # pointer jumping
            nxt = parent[parent]
            if np.array_equal(nxt, parent):
                break
            parent = nxt
    return parent


def _cg(n, ei, ej, g, diag_extra, b, fixed, tol=1e-9, maxit=20000):
    """Solve the conductance system: sum over edges g (x_i - x_j) + diag_extra x_i = b_i, with x fixed at 0 on `fixed`
    nodes. Jacobi-preconditioned conjugate gradients, matrix-free."""
    deg = np.bincount(ei, weights=g, minlength=n) + np.bincount(ej, weights=g, minlength=n) + diag_extra
    free = ~fixed
    deg_f = np.where(free & (deg > 0), deg, 1.0)

    def A(x):
        y = deg * x - np.bincount(ei, weights=g * x[ej], minlength=n) - np.bincount(ej, weights=g * x[ei], minlength=n)
        y[fixed] = 0.0
        return y
    x = np.zeros(n)
    r = np.where(free, b, 0.0)
    z = r / deg_f
    p = z.copy()
    rz = float(r @ z)
    nb = float(np.linalg.norm(r)) or 1.0
    it = 0
    for it in range(maxit):
        Ap = A(p)
        pAp = float(p @ Ap)
        if pAp <= 0:
            break
        a = rz / pAp
        x += a * p
        r -= a * Ap
        if float(np.linalg.norm(r)) <= tol * nb:
            break
        z = r / deg_f
        rz2 = float(r @ z)
        p = z + (rz2 / rz) * p
        rz = rz2
    return x, it + 1


def _grid_edges(mask_index):
    """Same-layer neighbour pairs (right and down) of the cells in a [ny, nx] index array (-1 off copper)."""
    a = mask_index
    h = (a[:, :-1] >= 0) & (a[:, 1:] >= 0)
    v = (a[:-1, :] >= 0) & (a[1:, :] >= 0)
    return np.concatenate([a[:, :-1][h], a[:-1, :][v]]), np.concatenate([a[:, 1:][h], a[1:, :][v]])


def _thickness(b, layer, default_outer=0.035, default_inner=0.0152):
    t = b.copper_mm(layer) if hasattr(b, "copper_mm") else None
    return float(t) if t else (default_outer if layer in ("F.Cu", "B.Cu") else default_inner)


def _dielectric_between(b, la, lb):
    """mm of dielectric between two copper layers, from the board's stack-up (or an even share of 1.6 mm)."""
    cu = list(b.copper)
    i, j = sorted((cu.index(la), cu.index(lb)))
    if b.stackup:
        seen, depth = -1, 0.0
        for l in b.stackup:
            if l.get("type") == "copper":
                seen += 1
                continue
            if l.get("type") in ("core", "prepreg") and i <= seen < j:
                depth += float(l.get("thickness") or 0)
        if depth > 0:
            return depth
    return 1.6 * (j - i) / max(1, len(cu) - 1)


# ------------------------------------------------------------------ voltage drop
def _net_copper(b, net):
    """What carries the net: [(layer, kind, geometry)] and the vias / through-hole pads joining layers."""
    items, joins = [], []
    for t in b.tracks:
        if t.net == net:
            pts = geom.arc_points(t.a, t.mid, t.b) if getattr(t, "mid", None) else [t.a, t.b]
            for a, c in zip(pts, pts[1:]):
                items.append((t.layer, "seg", (a, c, t.w)))
    for fp in b.fp_list:
        for p in fp.pads:
            if p.net != net:
                continue
            ls = [l for l in (b.copper if "*.Cu" in p.layers else p.layers) if l in b.copper]
            for l in ls:
                for pl in p.polys:
                    if len(pl) >= 3:
                        items.append((l, "poly", pl))
            if p.drill and len(ls) > 1:
                joins.append(((p.x, p.y), ls, max(p.drill, 0.2), (fp.ref, str(p.num))))
    for z in b.zones:
        if z.is_rule_area or z.net != net:
            continue
        for l, polys in (z.fills or {}).items():
            for pl in polys:
                if len(pl) >= 3:
                    items.append((l, "fill", pl))
    for v in b.vias:
        if v.net == net:
            ls = v.span(b.copper)
            for l in ls:
                items.append((l, "disk", ((v.x, v.y), v.d / 2)))
            joins.append(((v.x, v.y), ls, v.drill, None))
    return items, joins


def _ipc_amps(w_mm, t_mm, inner, rise=10.0):
    """IPC-2221: the current a conductor w mm wide of this copper carries at the temperature rise."""
    k = 0.024 if inner else 0.048
    area = (w_mm / 0.0254) * (t_mm / 0.0254)
    return k * rise ** 0.44 * area ** 0.725


def _smooth(a, m, r):
    """The mean of a over the copper cells within r cells (a square window), by summed-area tables."""
    def box(x):
        c = np.pad(np.cumsum(np.cumsum(x, 0), 1), ((1, 0), (1, 0)))
        ny, nx = x.shape
        j0 = np.clip(np.arange(ny) - r, 0, ny)
        j1 = np.clip(np.arange(ny) + r + 1, 0, ny)
        i0 = np.clip(np.arange(nx) - r, 0, nx)
        i1 = np.clip(np.arange(nx) + r + 1, 0, nx)
        return c[j1][:, i1] - c[j0][:, i1] - c[j1][:, i0] + c[j0][:, i0]
    s = box(np.where(m, a, 0.0))
    n = box(m.astype(float))
    return np.where(m & (n > 0), s / np.maximum(n, 1), 0.0)


def ir_drop(ctx, net, amps=None, loads=None, source=None, cell=None, max_cells=350_000):
    """Voltage drop through the net's copper from its source to its loads, and the current density on the way."""
    from .checks.power import _terminals, _source, rail_voltages
    from .checks.power_layout import _rail_current
    b, nl = ctx.board, ctx.netlist
    full = next((n for n in b.nets if n == net or n.rsplit("/", 1)[-1] == net), None)
    if full is None:
        raise ValueError(f"no net {net} on the board")
    net = full
    terms = _terminals(b, nl, net)
    if source:
        r, _, n = str(source).partition(".")
        src = (r, n) if n else next(((t[0], t[1]) for t in terms if t[0] == r), None)
    else:
        src = _source(nl, terms)
    if not src:
        raise ValueError(f"{net.rsplit('/', 1)[-1]}: no source found (a regulator output or a connector): name one")
    said = []
    total = amps if amps is not None else (_rail_current(ctx, net) if nl is not None else None)
    if total is None:
        total = 0.5
        said.append(f"no current declared for {net.rsplit('/', 1)[-1]}: assumed {total:g} A (declare it with the nets tool)")
    if loads:
        load = {str(k): float(v) for k, v in loads.items()}
    else:
        refs = sorted({t[0] for t in terms if t[0] != src[0]})
        if not refs:
            raise ValueError(f"{net.rsplit('/', 1)[-1]}: nothing draws current from it but {src[0]}")
        load = {r: total / len(refs) for r in refs}
        said.append(f"{total:g} A shared evenly by {', '.join(refs[:8])}{' ...' if len(refs) > 8 else ''} (set each load's own current to refine it)")
    items, joins = _net_copper(b, net)
    if not items:
        raise ValueError("the net has no copper on the board")
    pts = []
    for l, k, g in items:
        if k == "seg":
            pts += [g[0], g[1]]
        elif k == "disk":
            pts.append(g[0])
        else:
            pts += list(g)
    box = geom.bbox(pts)
    box = (box[0] - 0.5, box[1] - 0.5, box[2] + 0.5, box[3] + 0.5)
    widths = [g[2] for _, k, g in items if k == "seg"]
    if cell is None:
        cell = min(0.25, max(0.05, (min(widths) / 2.5) if widths else 0.2))
    layers = [l for l in b.copper if any(it[0] == l for it in items)]
    while ((box[2] - box[0]) / cell) * ((box[3] - box[1]) / cell) * len(layers) > max_cells:
        cell *= 1.25
    R = Raster(box, cell)
    F = _finer(R, 4)                                   # copper drawn four times finer: each cell's share of copper
    cover = {}
    for l in layers:
        fm = F.empty()
        for l2, k, g in items:
            if l2 != l:
                continue
            if k == "seg":
                F.seg(fm, g[0], g[1], g[2], exact=True)
            elif k == "disk":
                F.disk(fm, g[0], g[1])
            else:
                F.poly(fm, g)
        cover[l] = fm.reshape(R.ny, 4, R.nx, 4).mean(axis=(1, 3))
    masks = {l: cover[l] >= 0.12 for l in layers}
    # unknowns: copper cells of every layer
    index, n = {}, 0
    for l in layers:
        idx = np.full((R.ny, R.nx), -1, dtype=np.int64)
        m = masks[l]
        k = int(m.sum())
        idx[m] = np.arange(n, n + k)
        index[l] = idx
        n += k
    ei, ej, g = [], [], []
    sheet = {l: _thickness(b, l) * 1e-3 / RHO for l in layers}               # siemens per square
    for l in layers:
        a, c_ = _grid_edges(index[l])
        cv = np.zeros(n)
        cv[index[l][masks[l]]] = cover[l][masks[l]]
        ei.append(a)
        ej.append(c_)
        g.append(sheet[l] * (cv[a] + cv[c_]) / 2)       # a half-covered cell conducts half as much
    for (x, y), ls, drill, _ in joins:                 # each via barrel, layer to layer down its span
        j, i = R.cell_of(x, y)
        ls = [l for l in ls if l in index]
        for la, lb in zip(ls, ls[1:]):
            ua, ub = index[la][j, i], index[lb][j, i]
            if ua < 0 or ub < 0:
                continue
            h = _dielectric_between(b, la, lb) * 1e-3 + _thickness(b, lb) * 1e-3
            area = math.pi * (drill * 1e-3 + PLATING) * PLATING
            ei.append(np.array([ua]))
            ej.append(np.array([ub]))
            g.append(np.array([area / (RHO * h)]))
    ei, ej, g = np.concatenate(ei), np.concatenate(ej), np.concatenate(g)
    comp = _components(n, ei, ej)
    fixed = np.zeros(n, dtype=bool)
    src_pads = [p for p in b.footprints[src[0]].pads if str(p.num) == str(src[1]) and p.net == net] if src[0] in b.footprints else []
    if not src_pads:
        raise ValueError(f"{src[0]} pin {src[1]} is not on the board")

    def pad_cells(p):
        out = []
        for l in layers:
            if l not in (b.copper if "*.Cu" in p.layers else p.layers):
                continue
            m = R.empty()
            for pl in p.polys:
                if len(pl) >= 3:
                    R.poly(m, pl)
            if not m.any():
                j, i = R.cell_of(p.x, p.y)
                m[j, i] = True
            u = index[l][m]
            out += [x for x in u.tolist() if x >= 0]
        return out
    for p in src_pads:
        fixed[pad_cells(p)] = True
    if not fixed.any():
        raise ValueError("the source pad has no copper cells")
    live = np.isin(comp, np.unique(comp[fixed]))
    rhs = np.zeros(n)
    out_loads, cut = [], []
    load_cells = {}
    for ref, a_ in load.items():
        fp = b.footprints.get(ref)
        if fp is None:
            continue
        cells = [c_ for p in fp.pads if p.net == net for c_ in pad_cells(p)]
        cells = [c_ for c_ in cells if not fixed[c_]]
        if not cells:
            continue
        if not live[cells].any():
            cut.append(ref)
            continue
        cells = [c_ for c_ in cells if live[c_]]
        rhs[cells] -= a_ / len(cells)
        load_cells[ref] = (cells, a_)
    keep = live
    V, iters = _cg(n, ei, ej, g, np.where(keep, 0.0, 1.0), np.where(keep, rhs, 0.0), fixed | ~keep, tol=1e-6)
    drop_mv = -V * 1000.0
    try:
        rv = rail_voltages(ctx) if nl is not None else {}
    except Exception:
        rv = {}
    volts = rv.get(net.rsplit("/", 1)[-1]) or rv.get(net)
    for ref, (cells, a_) in sorted(load_cells.items()):
        worst = float(drop_mv[cells].max())
        out_loads.append({"ref": ref, "amps": round(a_, 4), "drop_mv": round(worst, 2),
                          "pct": round(worst / 10.0 / volts, 2) if volts else None})
    # current density: siemens per square x the voltage step to each neighbour, both ways, as A per mm of width
    grids, dens, hot, peak = {}, {}, [], 0.0
    for l in layers:
        idx, m = index[l], masks[l]
        Vg = np.zeros((R.ny, R.nx))
        Vg[m] = V[idx[m]]
        gx = np.zeros_like(Vg)
        gy = np.zeros_like(Vg)
        both_x = m[:, 1:] & m[:, :-1]
        dvx = np.where(both_x, Vg[:, 1:] - Vg[:, :-1], 0.0)
        gx[:, 1:] += np.abs(dvx)
        gx[:, :-1] += np.abs(dvx)
        nx_ = np.zeros_like(Vg)
        nx_[:, 1:] += both_x
        nx_[:, :-1] += both_x
        both_y = m[1:, :] & m[:-1, :]
        dvy = np.where(both_y, Vg[1:, :] - Vg[:-1, :], 0.0)
        gy[1:, :] += np.abs(dvy)
        gy[:-1, :] += np.abs(dvy)
        ny_ = np.zeros_like(Vg)
        ny_[1:, :] += both_y
        ny_[:-1, :] += both_y
        ex = np.where(nx_ > 0, gx / np.maximum(nx_, 1), 0.0)
        ey = np.where(ny_ > 0, gy / np.maximum(ny_, 1), 0.0)
        J = sheet[l] * np.hypot(ex, ey) / R.c                # A per mm of copper width (a cell edge is c mm wide)
        J = J * np.clip(cover[l], 0, 1)                     # a partly covered cell carries its share
        # heating goes by the current over a length, not a corner's crowding: J averaged over about a millimetre,
        # against IPC-2221 for the net's narrowest track (narrow tracks carry more per mm than wide ones)
        Js = _smooth(J, m, max(1, int(round(0.5 / R.c))))
        w_ref = min(widths) if widths else 1.0
        lim = _ipc_amps(w_ref, _thickness(b, l), l not in ("F.Cu", "B.Cu")) / w_ref
        grids[l] = _rows_out(np.where(m, -Vg * 1000.0, 0.0), m, 2)
        dens[l] = _rows_out(J, m, 3)
        peak = max(peak, float(J[m].max()) if m.any() else 0.0)
        over = m & (Js > lim)
        if over.any():
            jj, ii = np.nonzero(over)
            k = int(np.argmax(Js[over]))
            x, y = float(R.xs[ii[k]]), float(R.ys[jj[k]])
            near = min(b.fp_list, key=lambda f: (f.x - x) ** 2 + (f.y - y) ** 2).ref if b.fp_list else ""
            hot.append({"layer": l, "x": round(x, 2), "y": round(y, 2), "peak": round(float(Js[over].max()), 2),
                        "limit": round(lim, 2), "near": near,
                        "text": f"{l} near {near}: {float(Js[over].max()):.1f} A per mm of width, where this copper warms "
                                f"10 °C at {lim:.1f} A/mm: widen it"})
    worst = max(out_loads, key=lambda x: x["drop_mv"]) if out_loads else None
    lines = [f"{net.rsplit('/', 1)[-1]}: {total:g} A from {src[0]} pin {src[1]}"]
    if worst:
        lines.append(f"largest drop {worst['drop_mv']:.1f} mV at {worst['ref']}" + (f" ({worst['pct']:.2f} % of {volts:g} V)" if worst["pct"] is not None else ""))
    lines += said
    if cut:
        lines.append(f"{', '.join(cut)} not joined to the source by copper (unrouted?)")
    lines += [h["text"] for h in hot]
    return {"net": net, "source": f"{src[0]}.{src[1]}", "amps": total, "volts": volts, "loads": out_loads, "cut": cut,
            "layers": grids, "density": dens, "max_density": round(peak, 3), "hot": hot, "lines": lines,
            "frame": R.frame(), "iterations": iters, "assumed": said}


# ------------------------------------------------------------------ heat
AIR = {"still": 12.0, "fan": 30.0}         # W/m² K per face: natural convection + radiation; a small fan


def heat_sources(ctx):
    """Parts that get warm, with how much and why: linear regulators burn their drop, switchers their loss."""
    from .checks.power_layout import regulators, _rail_current
    from .checks.power import rail_voltages
    nl = ctx.netlist
    volts = rail_voltages(ctx)
    out = []
    for r in regulators(nl, ctx.ground_nets()):
        vin = max((volts.get(n) for _, n in r["ins"] if volts.get(n) is not None), default=None)
        outs = [(n, volts.get(n)) for _, n in r["outs"] if volts.get(n) is not None]
        if not outs and r["kind"] == "switching":
            outs = [(n, volts.get(n)) for n in {n for _, n in r["fb"]} if volts.get(n) is not None]
        if vin is None or not outs:
            continue
        net, vout = max(outs, key=lambda x: x[1])
        amps = _rail_current(ctx, net)
        if amps is None:
            continue
        if r["kind"] == "linear":
            w = max(0.0, vin - vout) * amps
            why = f"drops {vin:g} V to {vout:g} V at {amps:g} A"
        else:
            w = vout * amps * (1 / 0.88 - 1)
            why = f"{vout * amps:.2f} W out at about 88 % efficiency"
        if w > 0.005:
            out.append({"ref": r["ref"], "watts": round(w, 3), "why": why})
    return out


def heat(ctx, sources=None, ambient=None, air="still", cell=None):
    """The board's temperature with the given parts dissipating (W), cooled by the air on both faces."""
    b = ctx.board
    if not b.outline:
        raise ValueError("the board has no outline")
    if sources is None:
        sources = heat_sources(ctx)
    if ambient is None:
        from .checks.power_layout import ambient as amb
        ambient = amb(ctx)[0]
    hcoef = AIR.get(air, float(air) if isinstance(air, (int, float)) else 12.0)
    outer = max(b.outline, key=lambda o: abs(geom.area(o)))
    box = geom.bbox(outer)
    span = max(box[2] - box[0], box[3] - box[1])
    cell = cell or max(0.25, round(span / 140, 2))
    R = Raster(box, cell)
    on = R.empty()
    R.poly(on, outer)
    for hole in b.outline[1:]:
        m = R.empty()
        R.poly(m, hole)
        on &= ~m
    # copper share of each cell per layer, from a grid four times finer over the same cells
    fine = _finer(R, 4)
    kt = np.full((R.ny, R.nx), K_FR4 * (b.thickness or 1.6) * 1e-3)
    for l in b.copper:
        m = fine.empty()
        for t in b.tracks:
            if t.layer == l:
                fine.seg(m, t.a, t.b, t.w, exact=True)
        for fp in b.fp_list:
            for p in fp.pads:
                if l in p.layers or "*.Cu" in p.layers:
                    for pl in p.polys:
                        if len(pl) >= 3:
                            fine.poly(m, pl)
        for z in b.zones:
            if not z.is_rule_area and l in (z.fills or {}):
                for pl in z.fills[l]:
                    if len(pl) >= 3:
                        fine.poly(m, pl)
        share = m.reshape(R.ny, 4, R.nx, 4).mean(axis=(1, 3))
        kt += K_CU * _thickness(b, l) * 1e-3 * share
    idx = np.full((R.ny, R.nx), -1, dtype=np.int64)
    n = int(on.sum())
    idx[on] = np.arange(n)
    ei, ej = _grid_edges(idx)
    kv = kt[on]
    g = 2 * kv[ei] * kv[ej] / (kv[ei] + kv[ej])            # W/K between square cells (harmonic mean)
    conv = np.full(n, 2 * hcoef * (cell * 1e-3) ** 2)
    q = np.zeros(n)
    used = []
    for s in sources:
        fp = b.footprints.get(s["ref"])
        if fp is None or not s.get("watts"):
            continue
        m = R.empty()
        for p in fp.pads:
            for pl in p.polys:
                if len(pl) >= 3:
                    R.poly(m, pl)
        bb = fp.bbox()
        if not (m & on).any():
            R.poly(m, [[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]], [bb[0], bb[3]]])
        cells = idx[m & on]
        cells = cells[cells >= 0]
        if not len(cells):
            continue
        q[cells] += float(s["watts"]) / len(cells)
        used.append(dict(s))
    if not used:
        return {"grid": None, "parts": [], "sources": [], "ambient": ambient, "frame": R.frame(),
                "lines": ["Nothing on the board is known to get warm: give a part's power to see its heat."]}
    T, iters = _cg(n, ei, ej, g, conv, q, np.zeros(n, dtype=bool), tol=1e-10)
    grid = np.zeros((R.ny, R.nx))
    grid[on] = ambient + T
    parts = []
    for fp in b.fp_list:
        bb = fp.bbox()
        j0, j1 = R._rows(bb[1], bb[3])
        i0, i1 = R._cols(bb[0], bb[2])
        sub = grid[j0:j1 + 1, i0:i1 + 1][on[j0:j1 + 1, i0:i1 + 1]]
        if sub.size:
            parts.append({"ref": fp.ref, "max_c": round(float(sub.max()), 1)})
    parts.sort(key=lambda x: -x["max_c"])
    hottest = parts[0] if parts else None
    lines = [f"{s['ref']}: {s['watts']:g} W ({s['why']})" for s in used]
    lines.append(f"air {ambient:g} °C, {'still' if air == 'still' else 'moving'}: the hottest spot {float(grid[on].max()):.0f} °C"
                 + (f" at {hottest['ref']}" if hottest else ""))
    return {"grid": _rows_out(grid, on, 1), "parts": parts[:40], "sources": used, "ambient": ambient, "air": air,
            "max_c": round(float(grid[on].max()), 1), "frame": R.frame(), "lines": lines, "iterations": iters}
