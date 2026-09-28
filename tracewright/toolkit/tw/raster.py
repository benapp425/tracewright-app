"""Which net's copper covers a point of a layer, on a grid (for return-path and antenna checks).

    r = Raster(board.bbox(), 0.1)
    r.fill(poly, 3)          # cells whose centre lies inside poly (even-odd) get the value 3
    r.at(x, y)               # 0 = nothing there

KiCad writes each filled zone as simple polygons whose holes are joined to the outline by slits, so
an even-odd scanline fill of each polygon reproduces the copper exactly (to the grid).
"""
import math


class Raster:
    def __init__(self, bbox, res=0.1):
        x0, y0, x1, y1 = bbox
        self.x0, self.y0, self.res = float(x0), float(y0), float(res)
        self.nx = max(1, int(math.ceil((x1 - x0) / res)) + 1)
        self.ny = max(1, int(math.ceil((y1 - y0) / res)) + 1)
        self.cells = bytearray(self.nx * self.ny)

    def fill(self, poly, value):
        """Set the cells whose centre is inside `poly` (even-odd) to `value` (1..255)."""
        n = len(poly)
        if n < 3:
            return
        res, x0, y0 = self.res, self.x0, self.y0
        edges = []
        for i in range(n):
            (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
            if ay == by:
                continue
            if ay > by:
                ax, ay, bx, by = bx, by, ax, ay
            edges.append((ay, by, ax, (bx - ax) / (by - ay)))
        if not edges:
            return
        edges.sort()
        lo = max(0, int((edges[0][0] - y0) / res) - 1)
        hi = min(self.ny - 1, int((max(e[1] for e in edges) - y0) / res) + 1)
        one = bytes([value])
        active, k = [], 0
        for r in range(lo, hi + 1):
            yc = y0 + (r + 0.5) * res
            while k < len(edges) and edges[k][0] <= yc:
                active.append(edges[k])
                k += 1
            active = [e for e in active if e[1] > yc]
            xs = sorted(e[2] + (yc - e[0]) * e[3] for e in active)
            base = r * self.nx
            for j in range(0, len(xs) - 1, 2):
                c0 = max(0, int(math.ceil((xs[j] - x0) / res - 0.5)))
                c1 = min(self.nx - 1, int(math.floor((xs[j + 1] - x0) / res - 0.5)))
                if c1 >= c0:
                    self.cells[base + c0: base + c1 + 1] = one * (c1 - c0 + 1)

    def at(self, x, y):
        c = int((x - self.x0) / self.res)
        r = int((y - self.y0) / self.res)
        if 0 <= c < self.nx and 0 <= r < self.ny:
            return self.cells[r * self.nx + c]
        return 0

    def count(self, box, value=None):
        """(cells in box, cells set in box [to value]) for a box (x0, y0, x1, y1)."""
        c0, r0 = max(0, int((box[0] - self.x0) / self.res)), max(0, int((box[1] - self.y0) / self.res))
        c1, r1 = min(self.nx - 1, int((box[2] - self.x0) / self.res)), min(self.ny - 1, int((box[3] - self.y0) / self.res))
        tot = hit = 0
        for r in range(r0, r1 + 1):
            row = self.cells[r * self.nx + c0: r * self.nx + c1 + 1]
            tot += len(row)
            hit += (len(row) - row.count(0)) if value is None else row.count(value)
        return tot, hit
