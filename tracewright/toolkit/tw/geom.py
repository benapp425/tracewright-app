"""Plain-Python 2-D geometry for board checks (millimetres, KiCad screen axes: y down)."""
import math


def rot(x, y, deg):
    """Rotate a vector by a KiCad angle (degrees, counter-clockwise as seen on screen)."""
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def xform(pts, dx, dy, deg):
    return [(dx + p[0], dy + p[1]) if not deg else _add(rot(p[0], p[1], deg), dx, dy) for p in pts]


def _add(p, dx, dy):
    return (p[0] + dx, p[1] + dy)


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def seg_point_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def seg_seg_dist(a, b, c, d):
    if segments_cross(a, b, c, d):
        return 0.0
    return min(seg_point_dist(a, c, d), seg_point_dist(b, c, d), seg_point_dist(c, a, b), seg_point_dist(d, a, b))


def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)


def segments_cross(a, b, c, d):
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True

    def on(p, q, r):
        return min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9 and \
            min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
    return (o1 == 0 and on(a, c, b)) or (o2 == 0 and on(a, d, b)) or (o3 == 0 and on(c, a, d)) or \
        (o4 == 0 and on(c, b, d))


def area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def inside(p, poly):
    """Even-odd point in polygon."""
    x, y = p
    c = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xint:
                c = not c
        j = i
    return c


def poly_dist(p, poly):
    """0 inside, else distance to the boundary."""
    if inside(p, poly):
        return 0.0
    n = len(poly)
    return min(seg_point_dist(p, poly[i], poly[(i + 1) % n]) for i in range(n))


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def bbox_overlap(a, b, margin=0.0):
    return a[0] < b[2] + margin and b[0] < a[2] + margin and a[1] < b[3] + margin and b[1] < a[3] + margin


def arc_points(start, mid, end, max_seg=0.25, max_angle=10.0):
    """Points along the circular arc through start, mid, end (inclusive)."""
    c = circle_center(start, mid, end)
    if c is None:
        return [start, end]
    cx, cy, r = c
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    am = math.atan2(mid[1] - cy, mid[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)

    def norm(a):
        while a < 0:
            a += 2 * math.pi
        while a >= 2 * math.pi:
            a -= 2 * math.pi
        return a
    # sweep from a0 to a1 passing through am
    d1 = norm(a1 - a0)
    dm = norm(am - a0)
    sweep = d1 if dm <= d1 else d1 - 2 * math.pi
    n = max(2, int(math.ceil(max(abs(sweep) * r / max_seg, abs(math.degrees(sweep)) / max_angle))))
    n = min(n, 256)
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def circle_center(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def circle_poly(cx, cy, r, n=24):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def rect_poly(cx, cy, w, h, deg=0.0):
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return xform(pts, cx, cy, deg)


def roundrect_poly(cx, cy, w, h, r, deg=0.0, n=4):
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 1e-6:
        return rect_poly(cx, cy, w, h, deg)
    hw, hh = w / 2 - r, h / 2 - r
    pts = []
    for (qx, qy, a0) in ((hw, -hh, -90), (hw, hh, 0), (-hw, hh, 90), (-hw, -hh, 180)):
        for k in range(n + 1):
            a = math.radians(a0 + 90 * k / n)
            pts.append((qx + r * math.cos(a), qy + r * math.sin(a)))
    return xform(pts, cx, cy, deg)


def oval_poly(cx, cy, w, h, deg=0.0, n=10):
    if abs(w - h) < 1e-6:
        return circle_poly(cx, cy, w / 2, 24)
    return roundrect_poly(cx, cy, w, h, min(w, h) / 2, deg, n)


def chain_loops(pieces, tol=0.02):
    """Join open polylines [[p0, ..., pn], ...] end to end into closed loops (for Edge.Cuts)."""
    pieces = [list(p) for p in pieces if len(p) >= 2]
    loops, open_ = [], []
    while pieces:
        cur = pieces.pop()
        changed = True
        while changed:
            changed = False
            if dist(cur[0], cur[-1]) <= tol and len(cur) > 2:
                break
            for i, p in enumerate(pieces):
                if dist(cur[-1], p[0]) <= tol:
                    cur += p[1:]
                elif dist(cur[-1], p[-1]) <= tol:
                    cur += p[::-1][1:]
                elif dist(cur[0], p[-1]) <= tol:
                    cur = p[:-1] + cur
                elif dist(cur[0], p[0]) <= tol:
                    cur = p[::-1][:-1] + cur
                else:
                    continue
                pieces.pop(i)
                changed = True
                break
        if dist(cur[0], cur[-1]) <= tol and len(cur) > 2:
            loops.append(cur[:-1])
        else:
            open_.append(cur)
    return loops, open_


def octilinear(a, b, tol=0.02):
    """Is segment a-b horizontal, vertical or at 45 degrees?"""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx <= tol or dy <= tol or abs(dx - dy) <= tol


def angle_between(a, b, c):
    """Interior angle at b (degrees) of the path a-b-c."""
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 180.0
    cs = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cs))
