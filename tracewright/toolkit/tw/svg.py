"""KiCad plot SVGs (kicad-cli sch/pcb export svg) as geometry, plus a small rasteriser.

KiCad writes page millimetres (viewBox = page size) and draws every text twice: an invisible
<text> for search, then <g class="stroked-text"><desc>TEXT</desc> glyph strokes...</g>. So each
text's true ink box is the extent of its own glyph strokes -- no font metrics are guessed.

    doc = SvgDoc.load("board-Power.svg")
    doc.texts[0].text, doc.texts[0].box      # (x0, y0, x1, y1) mm, glyph ink incl. stroke width
    doc.segs                                 # every stroked straight piece: (x0, y0, x1, y1, colour, width)
    doc.render(px_per_mm=6).save("x.png")    # needs Pillow
"""
import math, re
import xml.etree.ElementTree as ET

NS = "{http://www.w3.org/2000/svg}"
_num = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_cmd = re.compile(r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")


def parse_style(s):
    out = {}
    for part in (s or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _color(v):
    if not v or v == "none":
        return None
    v = v.strip()
    if v.startswith("#") and len(v) == 7:
        return v.upper()
    if v.startswith("#") and len(v) == 4:
        return ("#" + "".join(c * 2 for c in v[1:])).upper()
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", v)
    if m:
        return "#%02X%02X%02X" % tuple(int(x) for x in m.groups())
    return v.upper()


def path_polylines(d):
    """SVG path data -> list of (points, closed) with curves and arcs flattened."""
    toks = [(m.group(1), m.group(2)) for m in _cmd.finditer(d or "")]
    out, cur, start = [], [], (0.0, 0.0)
    x = y = 0.0
    i, cmd = 0, None
    last_ctrl = None

    def nums(k):
        nonlocal i
        vals = []
        while len(vals) < k and i < len(toks) and toks[i][1] is not None:
            vals.append(float(toks[i][1]))
            i += 1
        return vals if len(vals) == k else None
    while i < len(toks):
        c, n = toks[i]
        if c:
            cmd = c
            i += 1
            if c in "Zz":
                if cur:
                    out.append((cur, True))
                cur = []
                x, y = start
                continue
        elif cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        C = cmd.upper()
        if C == "M":
            v = nums(2)
            if v is None:
                break
            if cur:
                out.append((cur, False))
            x, y = (x + v[0], y + v[1]) if rel else (v[0], v[1])
            start = (x, y)
            cur = [(x, y)]
            cmd = "l" if rel else "L"                   # following pairs are line-tos
        elif C == "L":
            v = nums(2)
            if v is None:
                break
            x, y = (x + v[0], y + v[1]) if rel else (v[0], v[1])
            cur.append((x, y))
        elif C == "H":
            v = nums(1)
            if v is None:
                break
            x = x + v[0] if rel else v[0]
            cur.append((x, y))
        elif C == "V":
            v = nums(1)
            if v is None:
                break
            y = y + v[0] if rel else v[0]
            cur.append((x, y))
        elif C in "CS":
            if C == "C":
                v = nums(6)
                if v is None:
                    break
                p1 = (x + v[0], y + v[1]) if rel else (v[0], v[1])
                p2 = (x + v[2], y + v[3]) if rel else (v[2], v[3])
                p3 = (x + v[4], y + v[5]) if rel else (v[4], v[5])
            else:
                v = nums(4)
                if v is None:
                    break
                p1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
                p2 = (x + v[0], y + v[1]) if rel else (v[0], v[1])
                p3 = (x + v[2], y + v[3]) if rel else (v[2], v[3])
            p0 = (x, y)
            for k in range(1, 9):
                t = k / 8
                a, b, cc, dd = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
                cur.append((a * p0[0] + b * p1[0] + cc * p2[0] + dd * p3[0], a * p0[1] + b * p1[1] + cc * p2[1] + dd * p3[1]))
            last_ctrl = p2
            x, y = p3
            continue
        elif C in "QT":
            if C == "Q":
                v = nums(4)
                if v is None:
                    break
                p1 = (x + v[0], y + v[1]) if rel else (v[0], v[1])
                p2 = (x + v[2], y + v[3]) if rel else (v[2], v[3])
            else:
                v = nums(2)
                if v is None:
                    break
                p1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
                p2 = (x + v[0], y + v[1]) if rel else (v[0], v[1])
            p0 = (x, y)
            for k in range(1, 7):
                t = k / 6
                cur.append(((1 - t) ** 2 * p0[0] + 2 * t * (1 - t) * p1[0] + t * t * p2[0],
                            (1 - t) ** 2 * p0[1] + 2 * t * (1 - t) * p1[1] + t * t * p2[1]))
            last_ctrl = p1
            x, y = p2
            continue
        elif C == "A":
            v = nums(7)
            if v is None:
                break
            rx, ry, phi, fa, fs = v[0], v[1], v[2], int(v[3]), int(v[4])
            ex, ey = (x + v[5], y + v[6]) if rel else (v[5], v[6])
            cur += _arc(x, y, rx, ry, phi, fa, fs, ex, ey)
            x, y = ex, ey
        else:
            i += 1
        last_ctrl = None
    if cur:
        out.append((cur, False))
    return out


def _arc(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    """Endpoint-parameterised elliptical arc -> points (excluding the start)."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    n = max(2, int(abs(dt) / (math.pi / 18)) + 1)
    pts = []
    for k in range(1, n + 1):
        t = t1 + dt * k / n
        pts.append((cx + rx * math.cos(t) * cp - ry * math.sin(t) * sp, cy + rx * math.cos(t) * sp + ry * math.sin(t) * cp))
    return pts


class SvgText:
    __slots__ = ("text", "x", "y", "size", "anchor", "color", "box", "segs")

    def __repr__(self):
        return f"SvgText({self.text!r} {tuple(round(v, 2) for v in self.box) if self.box else None})"


class SvgDoc:
    def __init__(self):
        self.width = self.height = 0.0
        self.segs = []            # (x0, y0, x1, y1, colour, width) non-text strokes
        self.fills = []           # (points, colour)
        self.circles = []         # (cx, cy, r, stroke, width, fill)
        self.texts = []           # SvgText
        self.ops = []             # document-order drawing ops for render()

    @classmethod
    def load(cls, path):
        doc = cls()
        tree = ET.parse(path)
        root = tree.getroot()
        vb = root.get("viewBox")
        if vb:
            v = [float(x) for x in vb.replace(",", " ").split()]
            doc.width, doc.height = v[2], v[3]
        else:
            doc.width = float(re.sub("[a-z]+", "", root.get("width", "0")))
            doc.height = float(re.sub("[a-z]+", "", root.get("height", "0")))
        doc._walk(root, {}, None)
        return doc

    def _walk(self, el, style, text_ctx):
        for ch in list(el):
            tag = ch.tag.replace(NS, "")
            st = dict(style)
            if ch.get("style"):
                st.update(parse_style(ch.get("style")))
            for k in ("fill", "stroke", "stroke-width"):
                if ch.get(k):
                    st[k] = ch.get(k)
            if tag == "g":
                if ch.get("class") == "stroked-text":
                    desc = ch.find(NS + "desc")
                    t = self._pending if getattr(self, "_pending", None) is not None else SvgText()
                    self._pending = None
                    t.text = desc.text if desc is not None and desc.text else getattr(t, "text", "") or ""
                    t.color = _color(st.get("stroke"))
                    t.segs = []
                    self._walk(ch, st, t)
                    if t.segs:
                        xs = [c for s in t.segs for c in (s[0], s[2])]
                        ys = [c for s in t.segs for c in (s[1], s[3])]
                        hw = max(s[5] for s in t.segs) / 2
                        t.box = (min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw)
                    else:
                        t.box = None
                    if not hasattr(t, "x") or t.x is None:
                        t.x = t.y = t.size = 0.0
                        t.anchor = "start"
                    self.texts.append(t)
                else:
                    self._walk(ch, st, text_ctx)
            elif tag == "text":
                t = SvgText()
                t.text = "".join(ch.itertext())
                t.x, t.y = float(ch.get("x", 0)), float(ch.get("y", 0))
                t.size = float(ch.get("font-size", 0) or 0)
                t.anchor = ch.get("text-anchor", "start")
                t.color, t.box, t.segs = None, None, []
                self._pending = t
            elif tag == "path":
                self._shape(path_polylines(ch.get("d", "")), st, text_ctx)
            elif tag in ("polyline", "polygon"):
                nums = [float(v) for v in _num.findall(ch.get("points", ""))]
                pts = list(zip(nums[0::2], nums[1::2]))
                self._shape([(pts, tag == "polygon")], st, text_ctx)
            elif tag == "line":
                pts = [(float(ch.get("x1", 0)), float(ch.get("y1", 0))), (float(ch.get("x2", 0)), float(ch.get("y2", 0)))]
                self._shape([(pts, False)], st, text_ctx)
            elif tag == "rect":
                x, y = float(ch.get("x", 0)), float(ch.get("y", 0))
                w, h = float(ch.get("width", 0)), float(ch.get("height", 0))
                self._shape([([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)], st, text_ctx)
            elif tag == "circle":
                cx, cy, r = float(ch.get("cx", 0)), float(ch.get("cy", 0)), float(ch.get("r", 0))
                sw = float(st.get("stroke-width", 0) or 0)
                stroke, fill = _color(st.get("stroke")), _color(st.get("fill"))
                self.circles.append((cx, cy, r, stroke, sw, fill))
                self.ops.append(("circle", cx, cy, r, stroke, sw, fill))
            elif tag in ("desc", "title"):
                continue
            else:
                self._walk(ch, st, text_ctx)

    def _shape(self, polylines, st, text_ctx):
        stroke = _color(st.get("stroke"))
        fill = _color(st.get("fill"))
        sw = float(re.sub("[a-z]+", "", str(st.get("stroke-width", "0")) or "0") or 0)
        if st.get("stroke-opacity") in ("0", "0.0"):
            stroke = None
        for pts, closed in polylines:
            if len(pts) < 1:
                continue
            if fill and closed and len(pts) >= 3 and text_ctx is None:
                self.fills.append((pts, fill))
                self.ops.append(("fill", pts, fill))
            if stroke and len(pts) >= 2:
                seq = pts + ([pts[0]] if closed else [])
                pieces = [(a[0], a[1], b[0], b[1], stroke, sw) for a, b in zip(seq, seq[1:])]
                if text_ctx is not None:
                    text_ctx.segs += pieces
                else:
                    self.segs += pieces
                self.ops.append(("stroke", seq, stroke, sw))
            elif stroke and len(pts) == 1 and text_ctx is not None:
                text_ctx.segs.append((pts[0][0], pts[0][1], pts[0][0], pts[0][1], stroke, sw))

    # ------------------------------------------------------------------ raster
    def render(self, px_per_mm=6.0, region=None, background="#FFFFFF"):
        """PIL image of the page (or region (x0, y0, x1, y1) mm)."""
        from PIL import Image, ImageDraw
        x0, y0, x1, y1 = region or (0, 0, self.width, self.height)
        W, H = max(1, int((x1 - x0) * px_per_mm)), max(1, int((y1 - y0) * px_per_mm))
        if W * H > 60e6:
            s = math.sqrt(60e6 / (W * H))
            px_per_mm *= s
            W, H = int(W * s), int(H * s)
        im = Image.new("RGB", (W, H), background)
        dr = ImageDraw.Draw(im)
        T = lambda p: ((p[0] - x0) * px_per_mm, (p[1] - y0) * px_per_mm)
        cull = region is not None
        for op in self.ops:
            if cull:
                if op[0] == "circle":
                    bx = (op[1] - op[3], op[2] - op[3], op[1] + op[3], op[2] + op[3])
                else:
                    xs = [q[0] for q in op[1]]
                    ys = [q[1] for q in op[1]]
                    bx = (min(xs), min(ys), max(xs), max(ys))
                if bx[2] < x0 - 1 or bx[0] > x1 + 1 or bx[3] < y0 - 1 or bx[1] > y1 + 1:
                    continue
            if op[0] == "fill":
                pts = [T(p) for p in op[1]]
                if len(pts) >= 3:
                    dr.polygon(pts, fill=op[2])
            elif op[0] == "stroke":
                w = max(1, int(round(op[3] * px_per_mm)))
                pts = [T(p) for p in op[1]]
                dr.line(pts, fill=op[2], width=w, joint="curve")
                if w > 2:
                    r = w / 2
                    for q in (pts[0], pts[-1]):
                        dr.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=op[2])
            elif op[0] == "circle":
                _, cx, cy, r, stroke, sw, fill = op
                c = T((cx, cy))
                rr = r * px_per_mm
                box = [c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr]
                if fill:
                    dr.ellipse(box, fill=fill)
                if stroke:
                    dr.ellipse(box, outline=stroke, width=max(1, int(round(sw * px_per_mm))))
        return im


def load(path):
    return SvgDoc.load(path)
