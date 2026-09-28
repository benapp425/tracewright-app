"""What kind of net each one is: ground, a supply (with its voltage), one half of a differential
pair (with its interface and partner), a clock, a fast interface line, or a plain signal. The rules
are the checks' own: names (GND, +3V3, VBUS, USB_D_P / USB_D_N, SPI_SCK), the pins on the net (a
supply input names a rail), and the project's lists (tracewright.json checks.rail_voltages,
highspeed.nets).

    classify(nets, pin_types, cfg)   {net: {"kind", "tag", "voltage"?, "pair"?, "iface"?}}
"""
import re
from .checks.signal import find_pairs, pair_kind, tokens, CLOCK_TOKENS, SLOW_TOKENS
from .checks.power import rail_voltage

GROUND = re.compile(r"^(GND\w*|\w*_GND|AGND|DGND|PGND|SGND|VSS\w*|EARTH|CHASSIS|0V)$", re.I)
SUPPLY = re.compile(r"^[+-]\S+|^\d+(\.\d+)?V\d*(_\w+)?$|^V(CC|DD|BUS|BAT|IN|SYS|IO|MOT|CORE|AUX|DDA|CCA|REG|LOGIC|PP)\w*$|"
                    r"^(P|N)\d+V\d*\w*$", re.I)
SUPPLY_END = re.compile(r"_(\d+V\d*|\d+V\d+_\w+|VIN|VBUS|VSYS|VBAT)$", re.I)          # ACT_12V, COIL_12V, MOTOR_VBAT
FAST = ("HDMI", "TMDS", "MIPI", "CSI", "DSI", "LVDS", "PCIE", "SATA", "RGMII", "RMII", "SDIO", "EMMC", "QSPI")
KINDS = ("ground", "power", "pair", "clock", "fast", "signal", "unconnected")
IFACE_TAG = {"usb": "USB", "hdmi": "HDMI", "mipi": "MIPI", "eth": "ETH", "lvds": "LVDS", "pcie": "PCIe", "sata": "SATA",
             "clk": "CLK", "hs": "DIFF"}


def short(n):
    return (n or "").rsplit("/", 1)[-1]


def classify(nets, pin_types=None, cfg=None):
    """nets: {full net name: [(ref, pin), ...]} (or an iterable of names); pin_types: {(ref, pin):
    KiCad pin type}; cfg: tracewright.json. Every net gets a kind and a short tag for lists."""
    names = list(nets)
    members = nets if isinstance(nets, dict) else {}
    pin_types = pin_types or {}
    cfg = cfg or {}
    volts = ((cfg.get("checks") or {}).get("rail_voltages") or {})
    fast_pats = [re.compile(p, re.I) for p in ((cfg.get("highspeed") or {}).get("nets") or [])]
    pairs = {}
    for p, n in find_pairs(names):
        k = pair_kind(p)
        pairs[p] = (n, k)
        pairs[n] = (p, k)
    out = {}
    for full in names:
        s = short(full)
        if full.startswith("unconnected-") or not s:
            out[full] = {"kind": "unconnected", "tag": ""}
            continue
        pins = members.get(full) or []
        types = {pin_types.get((r, str(p))) for r, p in pins}
        if GROUND.match(s):
            out[full] = {"kind": "ground", "tag": "GND"}
            continue
        v = volts.get(s)
        if v is None:
            v = rail_voltage(s)
        supply_pin = "power_in" in types or "power_out" in types
        if ((SUPPLY.match(s) or SUPPLY_END.search(s)) and (v is not None or supply_pin or s.startswith("+") or
                                                          re.match(r"^V(CC|DD|BUS|BAT|IN|SYS)\d*$", s, re.I))) or \
                (supply_pin and v is not None) or \
                ("power_out" in types and not pairs.get(full)):
            out[full] = {"kind": "power", "tag": f"{v:g} V" if v is not None else "PWR", **({"voltage": v} if v is not None else {})}
            continue
        if full in pairs:
            partner, iface = pairs[full]
            out[full] = {"kind": "pair", "tag": IFACE_TAG.get(iface, "DIFF"), "pair": partner, **({"iface": iface} if iface else {})}
            continue
        toks = tokens(s)
        if any(p.search(s) for p in fast_pats):
            out[full] = {"kind": "fast", "tag": "HS"}
        elif any(t in CLOCK_TOKENS or (t.endswith("CLK") and len(t) <= 8) for t in toks) and not (set(toks) & SLOW_TOKENS):
            out[full] = {"kind": "clock", "tag": "CLK"}
        elif any(t.startswith(k) for t in toks for k in FAST) and not (set(toks) & SLOW_TOKENS):
            out[full] = {"kind": "fast", "tag": "HS"}
        else:
            out[full] = {"kind": "signal", "tag": ""}
    return out


def from_netlist(nl, cfg=None):
    """classify() over a tw.netlist.Netlist."""
    types = {k: v.get("type", "") for k, v in nl.pin_info.items()}
    return classify(nl.nets, types, cfg)


def from_board(board, cfg=None):
    """classify() over a board's nets and pads (when there is no schematic netlist)."""
    nets, types = {}, {}
    for fp in board.footprints.values():
        for p in fp.pads:
            if p.net:
                nets.setdefault(p.net, []).append((fp.ref, p.num))
                if p.pintype:
                    types[(fp.ref, p.num)] = p.pintype
    for n in getattr(board, "nets", []) or []:
        nets.setdefault(n, [])
    return classify(nets, types, cfg)
