"""Signal integrity: which nets are fast, their return paths (the plane under them, the ground via
beside each layer change), crosstalk from the tracks beside them, and differential pairs (routed
together, lengths matched, impedance from the board's stackup).

Fast nets: differential pairs with an interface name (USB, MIPI, HDMI, Ethernet, LVDS, PCIe...),
clocks (CLK, SCK, MCLK, XTAL...), and tracewright.json highspeed.nets (name patterns). A slow net
beside a fast one is a victim; a fast net over a gap in its plane is an antenna.
"""
import math, re, collections
from . import check, Finding, NotApplicable, examined, plural
from .. import geom
from ..raster import Raster

CLOCK_TOKENS = {"CLK", "SCK", "SCLK", "MCLK", "BCLK", "LRCLK", "REFCLK", "XTAL", "XIN", "XOUT", "XI", "XO", "OSC", "OSCIN",
                "OSCOUT", "CLKIN", "CLKOUT", "PCLK", "XCLK", "MCO", "XTAL1", "XTAL2", "X1", "X2", "OSC32", "SWCLK", "TCK"}
IFACE = {"USB": "usb", "HDMI": "hdmi", "TMDS": "hdmi", "MIPI": "mipi", "CSI": "mipi", "DSI": "mipi", "CAM": "mipi",
         "ETH": "eth", "MDI": "eth", "RMII": "eth", "RGMII": "eth", "LVDS": "lvds", "PCIE": "pcie", "SATA": "sata",
         "SDIO": "sdio", "EMMC": "sdio", "QSPI": "qspi"}
SLOW_TOKENS = {"EN", "RST", "RESET", "INT", "IRQ", "PWR", "DET", "PRESENT", "LED", "CEC", "HPD", "SCL", "SDA", "PG", "OE",
               "MDC", "MDIO", "VBUS", "CC", "CC1", "CC2", "ID", "SBU", "SBU1", "SBU2", "GPIO", "ADC", "SEL", "WAKE", "OC",
               "FLT", "FAULT", "STAT", "ALERT", "BOOT", "SW", "PHY_RST"}
ZDIFF = {"usb": 90.0, "hdmi": 100.0, "mipi": 100.0, "eth": 100.0, "lvds": 100.0, "pcie": 85.0, "sata": 100.0}
SKEW = {"usb": 1.25, "hdmi": 0.15, "mipi": 0.15, "lvds": 0.15, "pcie": 0.15, "eth": 0.15, "sata": 0.15}
IFACE_NAME = {"usb": "USB 2.0", "hdmi": "HDMI", "mipi": "MIPI", "lvds": "LVDS", "pcie": "PCIe", "eth": "Ethernet",
              "sata": "SATA", "clk": "a clock pair", "hs": "a high-speed pair"}


def tokens(name):
    s = name.rsplit("/", 1)[-1].upper()
    return [t for t in re.split(r"[_.\-\s/]+", s.rstrip("+-")) if t] + ([s[-1]] if s.endswith(("+", "-")) else [])


# --------------------------------------------------------------------------- pairs
_PN = re.compile(r"^(.*\d|TX|RX|CLK|REFCLK|D\d*|DATA\d*|LANE\d*|USB|T|R)P$")


def _partner(s):
    """Names the negative half of `s` could have, if `s` looks like a positive half."""
    toks = s.split("_")
    out = []
    for i, t in enumerate(toks):
        u = t.upper()
        alt = None
        if u in ("P", "DP", "D+", "TXP", "RXP", "INP", "OUTP", "POS"):
            alt = {"P": "N", "DP": "DM", "D+": "D-", "TXP": "TXN", "RXP": "RXN", "INP": "INN", "OUTP": "OUTN", "POS": "NEG"}[u]
        elif u.endswith("DP") and len(u) > 2:
            alt = u[:-2] + "DM"
        elif u.endswith("+"):
            alt = u[:-1] + "-"
        elif _PN.match(u) and len(u) > 1:
            alt = u[:-1] + "N"
        if alt:
            out.append("_".join(toks[:i] + [alt] + toks[i + 1:]))
    return out


def find_pairs(nets):
    """[(p net, n net)] matched by name: X_P/X_N, X_DP/X_DM (anywhere: USB_DP_MCU/USB_DM_MCU), X+/X-, D+/D-,
    TXP/TXN, CLK0P/CLK0N. Full net names in, full net names out."""
    short = {}
    for n in nets:
        if n and not n.startswith("unconnected-"):
            short.setdefault(n.rsplit("/", 1)[-1].upper(), n)
    pairs = set()
    for s, full in short.items():
        for cand in _partner(s):
            if cand in short and cand != s:
                pairs.add((full, short[cand]))
    return sorted(pairs)


def pair_kind(name):
    """'usb', 'mipi', 'hdmi', ... for a pair with an interface name, 'clk' / 'hs' for clock or lane
    pairs, None for other pairs (a current-sense or analogue input pair is not high speed)."""
    toks = tokens(name)
    for t in toks:
        for k, v in IFACE.items():
            if t.startswith(k):
                return v
    if any(t in ("DP", "DM", "D+", "D-") for t in toks) or name.upper().rsplit("/", 1)[-1] in ("D+", "D-", "DP", "DM"):
        return "usb"
    if any(t in CLOCK_TOKENS or t.endswith("CLK") for t in toks):
        return "clk"
    if any(re.match(r"^(TX|RX|TD|RD|D|LANE)\d*[PN]?$", t) for t in toks):
        return "hs"
    return None


def pair_tolerance(ctx, p, n, kind=None):
    """{"mm", "why"}: how far apart in length the two halves of a pair may be, the budget hs.pairs holds
    them to. The project's highspeed.skew_mm (a pattern on the positive half's short name, the last match
    wins) first; then USB full speed's 10 mm; then the interface's budget (SKEW); else 0.5 mm. None for a
    pair with no interface or clock name, which hs.pairs does not check."""
    kind = kind or pair_kind(p)
    if not kind:
        return None
    sp = p.rsplit("/", 1)[-1]
    out = None
    for pat, v in (ctx.setting("highspeed.skew_mm", {}) or {}).items():
        if re.search(pat, sp, re.I):
            out = {"mm": float(v), "why": "set for this project"}
    if out:
        return out
    if kind == "usb" and usb_speed(ctx, (p, n)) == "fs":
        return {"mm": 10.0, "why": "USB full speed"}
    return {"mm": SKEW.get(kind, 0.5), "why": IFACE_NAME.get(kind, kind)}


def ctx_grounds(ctx, board):
    try:
        return ctx.ground_nets()
    except Exception:
        return {n.rsplit("/", 1)[-1] for n in board.nets if n.rsplit("/", 1)[-1].upper().startswith("GND")}


def fast_nets(ctx, board):
    """{full net name: why} for the nets whose edges are fast (see the module note)."""
    out = {}
    pats = [re.compile(p, re.I) for p in (ctx.setting("highspeed.nets", []) or [])]
    grounds = ctx_grounds(ctx, board)
    try:
        declared = {k: set() for k in ("pair", "clock", "fast", "rf")}
        for k in declared:
            declared[k] = ctx.declared(k)
    except Exception:
        declared = {}
    for n in board.nets:
        s_ = n.rsplit("/", 1)[-1] if n else ""
        for k, names in declared.items():
            if s_ in names:
                out[n] = {"pair": "declared pair", "clock": "declared clock", "fast": "declared fast", "rf": "declared RF"}[k]
    for p, n in find_pairs(board.nets):
        k = pair_kind(p)
        if k:
            out[p] = out[n] = f"{k} pair"
    for n in board.nets:
        s = n.rsplit("/", 1)[-1] if n else ""
        if not s or s in grounds:
            continue
        toks = tokens(s)
        if any(p.search(s) for p in pats):
            out.setdefault(n, "declared fast")
        elif any(t in CLOCK_TOKENS or (t.endswith("CLK") and len(t) <= 8) for t in toks) and not (set(toks) & SLOW_TOKENS):
            out.setdefault(n, "clock")
        elif any(t.startswith(k) for t in toks for k in ("HDMI", "TMDS", "MIPI", "CSI", "DSI", "LVDS", "PCIE", "SATA", "RGMII",
                                                         "RMII", "SDIO", "EMMC", "QSPI")) and not (set(toks) & SLOW_TOKENS):
            out.setdefault(n, "interface")
    return out


def plane_nets(ctx, board):
    """Full names of the nets whose pours can carry a return current (grounds and supply rails)."""
    ref = set(ctx_grounds(ctx, board))
    try:
        ref |= set(ctx.power_nets())
    except Exception:
        pass
    return {n for n in board.nets if n.rsplit("/", 1)[-1] in ref}


class Planes:
    """Per copper layer, a raster of the reference pours (net index per cell), built on demand."""

    def __init__(self, board, nets, res=0.1):
        self.b, self.res = board, res
        self.ids = {}
        self.names = {}
        self._r = {}
        self.fills = collections.defaultdict(list)
        for z in board.zones:
            if z.is_rule_area or z.net not in nets:
                continue
            for layer, polys in z.fills.items():
                for pl in polys:
                    self.fills[layer].append((z.net, pl))
        boxes = [board.bbox()] + [geom.bbox(pl) for fl in self.fills.values() for _, pl in fl]
        x0, y0, x1, y1 = geom.bbox_union(boxes)
        self.box = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)

    def has(self, layer):
        return bool(self.fills.get(layer))

    def raster(self, layer):
        if layer not in self._r:
            r = Raster(self.box, self.res)
            for net, pl in self.fills.get(layer, []):
                if net not in self.ids:
                    self.ids[net] = len(self.ids) + 1
                    self.names[self.ids[net]] = net
                r.fill(pl, min(255, self.ids[net]))
            self._r[layer] = r
        return self._r[layer]

    def net_at(self, layer, x, y):
        v = self.raster(layer).at(x, y) if self.has(layer) else 0
        return self.names.get(v) if v else None


def neighbours(board, layer):
    cu = board.copper
    if layer not in cu:
        return []
    i = cu.index(layer)
    return [cu[j] for j in (i - 1, i + 1) if 0 <= j < len(cu)]


def _samples(t, step=0.2):
    pts = geom.arc_points(t.a, t.mid, t.b) if t.mid else [t.a, t.b]
    out = []
    for p, q in zip(pts, pts[1:]):
        L = geom.dist(p, q)
        n = max(1, int(L / step))
        for k in range(n):
            f = (k + 0.5) / n
            out.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, L / n))
    return out


# --------------------------------------------------------------------------- return path
@check("si.return_path", "Fast signals keep their return path", "High-speed", needs=("pcb",))
def return_path(ctx):
    """A fast signal's return current flows in the plane right under it. Where the track crosses a
    gap, a slot or a split between two pours (GND to +3V3), or runs off the plane's edge, the
    return current detours: the loop becomes an antenna (EMI) and the edge rings. Each fast track
    (see High-speed) is sampled every 0.2 mm against the pours of the next copper layer; crossings
    of more than 1.5 mm of void (highspeed.gap_mm), and every change of the reference net, are
    reported. Where a fast net changes layers, its return current changes planes too, so a ground
    via belongs within 2 mm of the signal via (highspeed.return_via_mm)."""
    b = ctx.board
    fast = fast_nets(ctx, b)
    if not fast:
        raise NotApplicable("no fast nets (differential pairs, clocks or declared high-speed nets) on this board")
    planes = Planes(b, plane_nets(ctx, b))
    if not any(planes.has(l) for l in b.copper):
        return [Finding("si.return_path", "info", f"no ground or supply pour on any layer, so {len(fast)} fast nets "
                        f"({', '.join(sorted(n.rsplit('/', 1)[-1] for n in fast)[:5])}) have no reference plane",
                        hint="A solid ground pour on the layer next to fast signals gives them a return path.",
                        key="si:noplane")]
    gap_mm = float(ctx.setting("highspeed.gap_mm", 1.5))
    via_mm = float(ctx.setting("highspeed.return_via_mm", 2.0))
    grounds = {n for n in b.nets if n.rsplit("/", 1)[-1] in ctx_grounds(ctx, b)}
    gvias = [(v.x, v.y) for v in b.vias if v.net in grounds] + \
            [(p.x, p.y) for p in b.pads() if p.net in grounds and p.drill and p.kind == "thru_hole"]
    two_planes = sum(1 for l in b.copper if planes.has(l)) >= 2
    out = []
    pads_by_net = b.net_pads()
    partner = {}
    for p, n in find_pairs(b.nets):
        partner[p], partner[n] = n, p
    for net in sorted(fast):
        tracks = [t for t in b.tracks if t.net == net]
        if not tracks:
            continue
        s = net.rsplit("/", 1)[-1]
        slow = fast[net] == "usb pair" and usb_speed(ctx, (net, partner.get(net, net))) == "fs"
        sev = "info" if slow else "warning"
        tail = " (USB full speed: its slow edges make this minor)" if slow else ""
        stops = [(p.x, p.y) for p in pads_by_net.get(net, [])] + [(v.x, v.y) for v in b.vias if v.net == net]
        problems = []
        for t in tracks:
            refs = [l for l in neighbours(b, t.layer) if planes.has(l)]
            if not refs:
                continue
            run, run_at, last = 0.0, None, None
            for x, y, dl in _samples(t):
                if any(abs(x - px) < 0.8 and abs(y - py) < 0.8 for px, py in stops):
                    run, run_at = 0.0, None
                    continue
                under = [planes.net_at(l, x, y) for l in refs]
                cur = next((u for u in under if u), None)
                if cur is None:
                    run += dl
                    run_at = run_at or (x, y)
                    if run >= gap_mm and run - dl < gap_mm:
                        problems.append(("gap", t.layer, run_at, "/".join(refs)))
                else:
                    if last and cur != last and last not in under:
                        problems.append(("split", t.layer, (x, y),
                                         f"{last.rsplit('/', 1)[-1]} to {cur.rsplit('/', 1)[-1]} on {'/'.join(refs)}"))
                    last = cur
                    run, run_at = 0.0, None
        if problems:
            kinds = collections.Counter(p[0] for p in problems)
            first = problems[0]
            what = []
            if kinds["gap"]:
                what.append(f"crosses {kinds['gap']} gap{'s' if kinds['gap'] > 1 else ''} in its reference plane")
            if kinds["split"]:
                sp = next(p for p in problems if p[0] == "split")
                what.append(f"changes reference plane {kinds['split']} time{'s' if kinds['split'] > 1 else ''} ({sp[3]})")
            out.append(Finding("si.return_path", sev, f"{s} ({fast[net]}) " + " and ".join(what) + tail,
                               {"net": s, "layer": first[1], "x": first[2][0], "y": first[2][1]},
                               hint="Route it over solid copper: close the gap or slot under it, move it off the split, or "
                                    "give the return a bridge (a stitching capacitor across a split, a ground via beside "
                                    "each layer change).",
                               key=f"si:ret:{s}"))
        if not two_planes:
            continue
        lonely = []
        for v in b.vias:                          # layer changes: a ground via beside each signal via
            if v.net != net:
                continue
            d = min((math.hypot(v.x - gx, v.y - gy) for gx, gy in gvias), default=99.0)
            if d > via_mm:
                lonely.append((v, d))
        if lonely:
            v, d = lonely[0]
            out.append(Finding("si.return_path", sev,
                               f"{s} ({fast[net]}) changes layers through {len(lonely)} via{'s' if len(lonely) > 1 else ''} "
                               f"with no ground via within {via_mm:g} mm (nearest {d:.1f} mm){tail}",
                               {"net": s, "x": v.x, "y": v.y},
                               hint="Put a ground via next to each signal via so the return current can follow it between "
                                    "the planes.", key=f"si:retvia:{s}"))
    examined(ctx, plural(len(fast), "fast net"))
    return out


# --------------------------------------------------------------------------- crosstalk
CELL = 4.0


def _grid(tracks):
    g = collections.defaultdict(list)
    for t in tracks:
        x0, x1 = sorted((t.a[0], t.b[0]))
        y0, y1 = sorted((t.a[1], t.b[1]))
        for i in range(int(x0 // CELL) - 1, int(x1 // CELL) + 2):
            for j in range(int(y0 // CELL) - 1, int(y1 // CELL) + 2):
                g[(t.layer, i, j)].append(t)
    return g


def coupling(t, o):
    """(parallel overlap length, center distance) of two straight segments, or None if not parallel."""
    ax, ay = t.b[0] - t.a[0], t.b[1] - t.a[1]
    L = math.hypot(ax, ay)
    bx, by = o.b[0] - o.a[0], o.b[1] - o.a[1]
    M = math.hypot(bx, by)
    if L < 0.2 or M < 0.2:
        return None
    ux, uy = ax / L, ay / L
    if abs(ux * by - uy * bx) / M > math.sin(math.radians(10)):
        return None
    t0 = (o.a[0] - t.a[0]) * ux + (o.a[1] - t.a[1]) * uy
    t1 = (o.b[0] - t.a[0]) * ux + (o.b[1] - t.a[1]) * uy
    lo, hi = max(0.0, min(t0, t1)), min(L, max(t0, t1))
    if hi - lo <= 0:
        return None
    d0 = (o.a[0] - t.a[0]) * -uy + (o.a[1] - t.a[1]) * ux
    d1 = (o.b[0] - t.a[0]) * -uy + (o.b[1] - t.a[1]) * ux
    return hi - lo, abs(d0 + d1) / 2


@check("si.crosstalk", "Fast signals are not run beside other signals", "High-speed", needs=("pcb",))
def crosstalk(ctx):
    """A fast edge couples into a track that runs beside it, more the longer and closer they run. The
    3W rule keeps center lines at least three track widths apart. Every other signal that runs
    parallel to a fast net on the same layer closer than that, for more than 10 mm in total
    (highspeed.crosstalk_mm), is reported. Grounds, rails and a pair's own partner are not victims."""
    b = ctx.board
    fast = fast_nets(ctx, b)
    if not fast:
        raise NotApplicable("no fast nets on this board")
    limit = float(ctx.setting("highspeed.crosstalk_mm", 10.0))
    quiet = plane_nets(ctx, b)
    partner = {}
    for p, n in find_pairs(b.nets):
        partner[p], partner[n] = n, p
    straight = [t for t in b.tracks if not t.mid and t.net]
    grid = _grid([t for t in straight if t.net not in quiet])
    acc = collections.defaultdict(lambda: [0.0, 99.0, None, None, 0.2])  # (fast, other) -> length, min gap, where, layer, w
    for t in straight:
        if t.net not in fast:
            continue
        seen = set()
        cx, cy = (t.a[0] + t.b[0]) / 2, (t.a[1] + t.b[1]) / 2
        xs, ys = sorted((t.a[0], t.b[0])), sorted((t.a[1], t.b[1]))
        for i in range(int(xs[0] // CELL), int(xs[1] // CELL) + 1):
            for j in range(int(ys[0] // CELL), int(ys[1] // CELL) + 1):
                for o in grid.get((t.layer, i, j), []):
                    if id(o) in seen or o.net == t.net or o.net == partner.get(t.net):
                        continue
                    seen.add(id(o))
                    c = coupling(t, o)
                    if not c:
                        continue
                    ov, d = c
                    w = max(t.w, o.w)
                    gap = d - (t.w + o.w) / 2
                    if gap < 0 or d >= 0.9 * 3 * w:            # the 3W rule, with a little slack
                        continue
                    key = tuple(sorted((t.net, o.net))) if o.net in fast else (t.net, o.net)
                    a = acc[key]
                    a[0] += ov
                    if gap < a[1]:
                        a[1], a[2], a[3], a[4] = gap, (cx, cy), t.layer, w
    out = []
    for (f, o), (length, gap, where, layer, w) in sorted(acc.items(), key=lambda kv: -kv[1][0]):
        if length < limit:
            continue
        fs, os_ = f.rsplit("/", 1)[-1], o.rsplit("/", 1)[-1]
        slow = fast.get(f) == "usb pair" and usb_speed(ctx, (f, partner.get(f, f))) == "fs"
        out.append(Finding("si.crosstalk", "info" if slow else "warning",
                           f"{fs} ({fast.get(f, 'fast')}) runs {length:.0f} mm beside {os_} on {layer}, as close as "
                           f"{gap:.2f} mm (the 3W rule wants about {3 * w - w:.2f} mm between {w:g} mm tracks)",
                           {"net": fs, "layer": layer, "x": where[0], "y": where[1]},
                           hint="Spread them apart, route them on different layers with a plane between, or put a ground "
                                "track (with vias) between them.", key=f"si:xtalk:{fs}:{os_}"))
    examined(ctx, plural(len(fast), "fast net"))
    return out


# --------------------------------------------------------------------------- differential pairs
def z_microstrip(w, h, t, er):
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def z_stripline(w, b, t, er):
    return 60.0 / math.sqrt(er) * math.log(4.0 * b / (0.67 * math.pi * (0.8 * w + t)))


def zdiff(w, s, h, t, er, strip=False, b2=None):
    """IPC-2141 edge-coupled differential impedance (ohms), about +-10%."""
    if strip:
        return 2 * z_stripline(w, b2, t, er) * (1 - 0.347 * math.exp(-2.9 * s / b2))
    return 2 * z_microstrip(w, h, t, er) * (1 - 0.48 * math.exp(-0.96 * s / h))


USB_FS = re.compile(r"ESP32-?(S2|S3|C3|C5|C6|H2)|RP2040|RP2350|STM32(F0|F1|F3|F4|L0|L1|L4|L5|G0|G4|U0|WB|C0)|ATMEGA\d*U\d|"
                    r"AT90USB|ATSAMD|SAMD\d|SAME5|NRF52|NRF53|CH34\d|CH9102|CH552|CH32V|CP210\d|FT232R|FT231X|FT230X|"
                    r"MCP2221|PIC32MX|PIC18F\d+K50|LPC11U|LPC13|EFM32|EFR32|ATTINY|MSP430F5", re.I)
USB_HS = re.compile(r"USB33[0-4]0|USB251[2-7]|USB57\d\d|FT2232H|FT232H|FT4232H|FT60[01]|CYUSB|CY7C68|GL85\d|FE1\.1|CM4|CM5|"
                    r"RK3\d|IMX|STM32H7|STM32F7|LAN95|LAN78|AX88|RTL815|VL8\d\d|TUSB8|USB46\d\d", re.I)


def usb_speed(ctx, nets):
    """'fs' when every USB device on the pair (through series resistors) is a full-speed-only part,
    'hs' when one is a high-speed part, None when unknown."""
    if not ctx.available("netlist"):
        return None
    nl = ctx.netlist
    shorts = {n.rsplit("/", 1)[-1] for n in nets}
    seen, parts = set(shorts), set()
    for _ in range(2):
        for full, nodes in nl.nets.items():
            if nl.short(full) not in seen:
                continue
            for r, _p in nodes:
                parts.add(r)
        for r in list(parts):
            if r.startswith(("R", "FB", "L")) and len(nl.pins_of(r)) == 2:
                seen |= {nl.net_of(r, x) for x in nl.pins_of(r)}
    text = [" ".join((nl.parts.get(r, {}).get(k, "") for k in ("value", "part", "footprint", "description")))
            for r in parts if r[:1] in ("U", "I", "M")]
    if any(USB_HS.search(t) for t in text):
        return "hs"
    if text and any(USB_FS.search(t) for t in text):
        return "fs"
    return None


def _stack(ctx, b, layer, ref):
    """(h mm, er, copper t mm, assumed?) between a signal layer and its reference layer."""
    d = b.dielectric_between(layer, ref)
    t = b.copper_mm(layer) or (0.035 if layer in ("F.Cu", "B.Cu") else 0.0175)
    if d:
        return d[0], d[1], t, False
    if len(b.copper) == 2:
        return max(0.2, b.thickness - 0.07), 4.5, t, True
    return 0.2104, 4.4, t, True                   # JLC04161H-7628 outer prepreg


@check("hs.pairs", "Differential pairs: together, matched, on impedance", "High-speed", needs=("pcb",))
def hs_pairs(ctx):
    """Pairs found by name (X_P/X_N, X_DP/X_DM anywhere in the name, X+/X-, D+/D-) that carry an
    interface (USB, MIPI, HDMI, Ethernet, LVDS, PCIe) or a clock: both halves routed, on the same
    layers, the same number of vias, length skew within the interface budget (USB 2.0 1.25 mm,
    MIPI/LVDS/HDMI 0.15 mm, else 0.5 mm; highspeed.skew_mm overrides), routed side by side (the
    uncoupled length), and the differential impedance estimated from the width, gap and the
    board's stackup (IPC-2141, about +-10%) against the interface's target (USB 90, PCIe 85, the
    others 100 ohms; highspeed.zdiff overrides)."""
    b = ctx.board
    pairs = [(p, n, pair_kind(p)) for p, n in find_pairs(b.nets)]
    pairs = [(p, n, k) for p, n, k in pairs if k]
    if not pairs:
        raise NotApplicable("no differential pairs with an interface or clock name")
    planes = Planes(b, plane_nets(ctx, b))
    out = []
    length = collections.defaultdict(float)
    by_layer = collections.defaultdict(lambda: collections.defaultdict(float))
    widths = collections.defaultdict(collections.Counter)
    for t in b.tracks:
        L = t.length()
        length[t.net] += L
        by_layer[t.net][t.layer] += L
        widths[t.net][round(t.w, 3)] += L
    vias = collections.Counter(v.net for v in b.vias)
    for p, n, kind in pairs:
        sp, sn = p.rsplit("/", 1)[-1], n.rsplit("/", 1)[-1]
        name = f"{sp}/{sn}"
        if length[p] == 0 and length[n] == 0:
            continue
        fs = kind == "usb" and usb_speed(ctx, (p, n)) == "fs"
        soft = "info" if fs else "warning"             # USB full speed tolerates asymmetry, loose coupling, impedance
        fsnote = " (USB full speed, which this pair carries, tolerates it)" if fs else ""
        if (length[p] == 0) != (length[n] == 0):
            out.append(Finding("hs.pairs", "warning", f"only one half of pair {name} is routed", {"net": sp},
                               key=f"hs:half:{sp}"))
            continue
        tol = pair_tolerance(ctx, p, n, kind)
        lim = tol["mm"]
        skew = abs(length[p] - length[n])
        if skew > lim:
            out.append(Finding("hs.pairs", "warning", f"pair {name}: {length[p]:.2f} vs {length[n]:.2f} mm, "
                               f"skew {skew:.2f} mm > {lim:g} mm ({tol['why']})", {"net": sp},
                               hint="Tune the shorter half near where the mismatch starts (a bend or the connector).",
                               key=f"hs:skew:{sp}"))
        if vias[p] != vias[n]:
            out.append(Finding("hs.pairs", soft, f"pair {name}: {vias[p]} vs {vias[n]} vias{fsnote}", {"net": sp},
                               hint="Give both halves the same vias, side by side (a USB-C receptacle's D+/D- crossover "
                                    "is best done with one via on each half).", key=f"hs:vias:{sp}"))
        lp = {l for l, v in by_layer[p].items() if v > 0.5}
        ln = {l for l, v in by_layer[n].items() if v > 0.5}
        if lp != ln:
            out.append(Finding("hs.pairs", soft, f"pair {name}: the halves are on different layers "
                               f"({', '.join(sorted(lp))} vs {', '.join(sorted(ln))}){fsnote}", {"net": sp},
                               key=f"hs:layers:{sp}"))
        wp, wn = widths[p].most_common(1)[0][0], widths[n].most_common(1)[0][0]
        if abs(wp - wn) > 0.011:
            out.append(Finding("hs.pairs", "info", f"pair {name}: track widths differ ({wp:g} vs {wn:g} mm)", {"net": sp},
                               key=f"hs:width:{sp}"))
        # coupling: how much of the P half runs beside the N half
        tp = [t for t in b.tracks if t.net == p and not t.mid]
        tn = [t for t in b.tracks if t.net == n and not t.mid]
        coupled, gaps = 0.0, []
        for t in tp:
            best = 0.0
            for o in tn:
                if o.layer != t.layer:
                    continue
                c = coupling(t, o)
                if not c:
                    continue
                ov, d = c
                gap = d - (t.w + o.w) / 2
                if 0 < gap <= max(3 * t.w, 0.6):
                    best += ov
                    gaps.append((gap, ov))
            coupled += min(best, t.length())
        total = sum(t.length() for t in b.tracks if t.net == p) or 1.0
        loose = max(0.0, total - coupled)
        loose_lim = float(ctx.setting("highspeed.uncoupled_mm", 5.0))
        if loose > loose_lim and loose > 0.1 * total:           # the break-out at each end is normal
            out.append(Finding("hs.pairs", "info" if fs or kind == "clk" else "warning",
                               f"pair {name}: {loose:.1f} of {total:.1f} mm are not routed side by side{fsnote}",
                               {"net": sp}, hint="Keep the two halves together, at one gap, from end to end (split only at "
                                                 "the pads).", key=f"hs:loose:{sp}"))
        # impedance
        target = None
        for pat, v in (ctx.setting("highspeed.zdiff", {}) or {}).items():
            if re.search(pat, sp, re.I):
                target = float(v)
        target = target or ZDIFF.get(kind)
        if not target or not gaps:
            continue
        gaps.sort()
        wsum, acc, gap = sum(ov for _, ov in gaps), 0.0, gaps[0][0]
        for g, ov in gaps:                             # length-weighted median gap
            acc += ov
            if acc >= wsum / 2:
                gap = g
                break
        layer = max(by_layer[p].items(), key=lambda kv: kv[1])[0]
        refs = [l for l in neighbours(b, layer) if planes.has(l)]
        if not refs:
            out.append(Finding("hs.pairs", "info", f"pair {name}: no plane next to {layer}, so its impedance is not "
                               "controlled", {"net": sp, "layer": layer}, key=f"hs:noref:{sp}"))
            continue
        h, er, tcu, assumed = _stack(ctx, b, layer, refs[0])
        if len(refs) == 2:
            h2 = _stack(ctx, b, layer, refs[1])[0]
            z = zdiff(wp, gap, h, tcu, er, strip=True, b2=h + h2 + tcu)
        else:
            z = zdiff(wp, gap, h, tcu, er)
        if abs(z - target) / target > 0.15:
            sev = "info" if fs or kind in ("clk", "hs") else "warning"
            note = fsnote or (" -- USB full speed (12 Mb/s) tolerates this; high speed (480 Mb/s) needs it"
                              if kind == "usb" else "")
            out.append(Finding("hs.pairs", sev,
                               f"pair {name}: about {z:.0f} ohm differential ({wp:g} mm wide, {gap:.2f} mm apart, "
                               f"{h:.3f} mm to {refs[0]}{', stackup assumed' if assumed else ''}); "
                               f"{kind.upper()} wants {target:.0f} ohm +-15%{note}",
                               {"net": sp, "layer": layer},
                               hint="Adjust the width and gap (JLC's impedance calculator gives exact values for its "
                                    "stackups), or order the board with impedance control.",
                               key=f"hs:z:{sp}"))
    examined(ctx, plural(len(pairs), "differential pair"))
    return out
