"""The net model: one record per net saying what it is -- a supply with its voltage and the current it
carries, ground, one half of a differential pair (with its partner and impedance), a clock, an RF line,
an analog line, or a plain signal -- and where each fact came from.

Declared facts win over guesses. They come from, in order:
  1. tracewright.json "nets"     {"VPYRO": {"kind": "power", "voltage": 8.4, "current": 5}, "USB_D_*": {...}}
                                 (keys are net names, full or short, or glob patterns)
  2. the schematic generator     hardware/<stem>/.tracewright-nets.json "attrs" (tw.sch: power(..., current=))
  3. older settings              checks.currents, checks.rail_voltages, checks.rails, checks.not_rails,
                                 checks.ground, checks.external_pullups, highspeed.nets
Anything not declared is inferred the way the checks always have: names (+3V3, VBUS, USB_D_P), pin types
(a power output names a rail), fuses and ferrites between rails, voltages carried through diodes.

    m = netmodel.for_context(ctx)          # the checks' view (ctx.nets)
    m.get("VPYRO")                        {"kind": "power", "voltage": 8.4, "current": 5, "source": {...}}
    netmodel.declare(project, "VPYRO", kind="power", voltage=8.4, current=5)
    netmodel.suggest_classes(m, board)     net classes sized from the currents and impedances
"""
import os, re, json, fnmatch, math

KINDS = ("power", "ground", "pair", "clock", "fast", "rf", "analog", "signal")
FIELDS = ("kind", "voltage", "current", "pair", "iface", "impedance", "class", "note", "external_pullup", "mhz")
NUMERIC = ("voltage", "current", "impedance", "mhz")
KIND_LABEL = {"power": "supply", "ground": "ground", "pair": "differential pair", "clock": "clock", "fast": "fast signal",
              "rf": "RF", "analog": "analog", "signal": "signal"}


def short(n):
    return (n or "").rsplit("/", 1)[-1]


def _clean(rec):
    """A record with known fields only, numbers as floats; raises ValueError on a bad kind or number."""
    out = {}
    for k, v in (rec or {}).items():
        if k not in FIELDS or v in (None, ""):
            continue
        if k == "kind":
            v = str(v).lower()
            if v not in KINDS:
                raise ValueError(f"unknown net kind {v!r}; kinds: {', '.join(KINDS)}")
        elif k in NUMERIC:
            v = float(v)
            if v < 0 or math.isnan(v):
                raise ValueError(f"{k} must be a positive number")
        elif k == "external_pullup":
            v = bool(v)
        else:
            v = str(v)
        out[k] = v
    return out


def _gen_attrs(project):
    """What the schematic generator declared (tw.sch writes it next to the schematic)."""
    try:
        pro = project.pro if hasattr(project, "pro") else None
        d = os.path.dirname(pro) if pro else None
        if not d:
            return {}
        with open(os.path.join(d, ".tracewright-nets.json")) as f:
            return (json.load(f) or {}).get("attrs") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def declared(cfg, project=None):
    """[(key, record, source)] in precedence order: tracewright.json nets, the generator's, older settings."""
    cfg = cfg or {}
    out = []
    for k, rec in ((cfg.get("nets") or {}).items()):
        try:
            out.append((k, _clean(rec), "tracewright.json"))
        except ValueError:
            continue
    if project is not None:
        for k, rec in _gen_attrs(project).items():
            try:
                out.append((k, _clean(rec), "schematic"))
            except ValueError:
                continue
    ch = cfg.get("checks") or {}
    for k, amps in (ch.get("currents") or {}).items():           # a current alone does not make a net a supply
        out.append((k, _clean({"current": amps}), "checks.currents"))
    for k, v in (ch.get("rail_voltages") or {}).items():
        out.append((k, _clean({"kind": "power", "voltage": v}), "checks.rail_voltages"))
    for k in ch.get("rails") or []:
        out.append((k, {"kind": "power"}, "checks.rails"))
    for k in ch.get("ground") or []:
        out.append((k, {"kind": "ground"}, "checks.ground"))
    for k in ch.get("not_rails") or []:
        out.append((k, {"kind": "signal"}, "checks.not_rails"))
    for k in ch.get("external_pullups") or []:
        out.append((k, {"external_pullup": True}, "checks.external_pullups"))
    for k in ((cfg.get("highspeed") or {}).get("nets") or []):
        out.append((k, {"kind": "fast"}, "highspeed.nets"))
    return out


def _matches(key, net):
    s = short(net)
    if key == net or key == s:
        return True
    if key.startswith("/") and key.endswith("/") and len(key) > 2:                 # /regex/
        try:
            return re.search(key[1:-1], net) is not None
        except re.error:
            return False
    if any(c in key for c in "*?["):
        return fnmatch.fnmatchcase(net, key) or fnmatch.fnmatchcase(s, key)
    return False


def declared_for(decl, net):
    """{field: (value, source)} declared for one net, earliest source first."""
    got = {}
    for key, rec, src in decl:
        if _matches(key, net):
            for f, v in rec.items():
                got.setdefault(f, (v, src))
    return got


def declared_kinds(cfg, project=None):
    """(nets declared as a kind, keyed by kind) -> {kind: [keys]}; the checks' rail and ground lists use it."""
    out = {}
    seen = set()
    for key, rec, src in declared(cfg, project):
        if "kind" in rec and key not in seen:
            seen.add(key)
            out.setdefault(rec["kind"], []).append(key)
    return out


class NetModel:
    """Every net's record: declared fields, else inferred ones, each with its source."""

    def __init__(self, names, decl, inferred, members=None):
        self.names = sorted(n for n in names if n and not n.startswith("unconnected-"))
        self.decl = decl
        self.members = members or {}
        self.records = {}
        for n in self.names:
            rec, src = {}, {}
            for f, (v, s) in declared_for(decl, n).items():
                rec[f], src[f] = v, s
            for f, v in (inferred.get(n) or {}).items():
                if f not in rec and v is not None:
                    rec[f], src[f] = v, "inferred"
            rec.setdefault("kind", "signal")
            src.setdefault("kind", "inferred")
            rec["source"] = src
            self.records[n] = rec
        self._by_short = {}
        for n in self.names:
            self._by_short.setdefault(short(n), n)

    def full(self, net):
        return net if net in self.records else self._by_short.get(short(net))

    def get(self, net):
        f = self.full(net)
        return dict(self.records[f]) if f else None

    def kind(self, net):
        r = self.get(net)
        return r["kind"] if r else None

    def voltage(self, net):
        r = self.get(net)
        return r.get("voltage") if r else None

    def current(self, net):
        r = self.get(net)
        return r.get("current") if r else None

    def partner(self, net):
        r = self.get(net)
        return r.get("pair") if r else None

    def of_kind(self, *kinds):
        return {n for n, r in self.records.items() if r["kind"] in kinds}

    def counts(self):
        c = {}
        for r in self.records.values():
            c[r["kind"]] = c.get(r["kind"], 0) + 1
        return c

    def summary(self):
        """'90 nets: 11 supplies (9 with a current), 1 ground, 2 in pairs, 5 clocks, 71 signals'."""
        c = self.counts()
        sup = self.of_kind("power")
        withi = sum(1 for n in sup if self.records[n].get("current") is not None)
        bits = []
        for k in KINDS:
            if not c.get(k):
                continue
            label = {"power": "supply", "ground": "ground", "pair": "in pairs", "clock": "clock", "fast": "fast signal",
                     "rf": "RF line", "analog": "analog line", "signal": "signal"}[k]
            if k in ("ground", "pair"):
                t = f"{c[k]} {label}"
            elif k == "power":
                t = f"{c[k]} {'supply' if c[k] == 1 else 'supplies'} ({withi} with a current)"
            else:
                t = f"{c[k]} {label}{'s' if c[k] != 1 else ''}"
            bits.append(t)
        return f"{len(self.records)} nets: " + ", ".join(bits)

    def unused_keys(self):
        """Declared names that match no net on the board or schematic (a typo, or a net renamed since)."""
        out = []
        for key, rec, src in self.decl:
            if src not in ("tracewright.json", "schematic"):
                continue
            if not any(_matches(key, n) for n in self.names):
                out.append((key, src))
        return out

    def to_json(self):
        return {n: {k: v for k, v in r.items()} for n, r in self.records.items()}


def _infer(names, members, pin_types, cfg, power=None, grounds=None, volts=None):
    """{net: {kind, voltage?, pair?, iface?}} from names and pins (tw.nettypes), refined by the checks'
    rail / ground / voltage readings when they are given."""
    from . import nettypes
    base = nettypes.classify({n: members.get(n, []) for n in names}, pin_types, {}) if names else {}
    out = {}
    for n in names:
        b = base.get(n) or {}
        s = short(n)
        rec = {"kind": b.get("kind", "signal")}
        if rec["kind"] == "unconnected":
            rec["kind"] = "signal"
        if rec["kind"] == "pair":
            rec["pair"] = short(b.get("pair"))
            if b.get("iface"):
                rec["iface"] = b["iface"]
        if b.get("voltage") is not None:
            rec["voltage"] = float(b["voltage"])
        if grounds is not None and s in grounds:
            rec = {"kind": "ground"}
        elif power is not None:
            if s in power and rec["kind"] not in ("ground",):
                rec["kind"] = "power"
            elif rec["kind"] == "power" and s not in power:
                rec["kind"] = "signal"
                rec.pop("voltage", None)
        if volts and s in volts and rec["kind"] == "power":
            rec["voltage"] = float(volts[s])
        if re.search(r"(^|_)(SDA|SCL)\d*($|_)|I2C\d*_?(SDA|SCL)", s, re.I):
            rec.setdefault("iface", "i2c")
        elif re.search(r"(^|_)(SPI\d*|MOSI|MISO|SCK|SCLK|CS|SS)(\d*|_N)?($|_)", s, re.I) and rec["kind"] in ("signal", "clock"):
            rec.setdefault("iface", "spi")
        elif re.search(r"(^|_)(UART\d*|TXD?|RXD?)\d*($|_)", s, re.I) and rec["kind"] == "signal":
            rec.setdefault("iface", "uart")
        out[n] = rec
    return out


IFACE_TAGS = {"usb": "USB", "hdmi": "HDMI", "mipi": "MIPI", "eth": "ETH", "lvds": "LVDS", "pcie": "PCIe", "sata": "SATA",
              "clk": "CLK", "hs": "DIFF"}


def tag(r):
    """A short tag for lists: '3.3 V 0.5 A', 'USB', 'CLK', 'RF', '5 A' (a heavy signal), 'GND'."""
    k = r.get("kind")
    if k == "power":
        bits = [f"{r['voltage']:g} V" if r.get("voltage") is not None else "PWR"]
        if r.get("current") is not None:
            bits.append(f"{r['current']:g} A")
        return " ".join(bits)
    if k == "pair":
        return IFACE_TAGS.get(r.get("iface") or "", "DIFF")
    if r.get("current") is not None and r["current"] >= 0.5:
        return f"{r['current']:g} A"
    return {"ground": "GND", "clock": "CLK", "fast": "HS", "rf": "RF", "analog": "ANA"}.get(k, "")


def for_context(ctx):
    """The model the checks see (cached on the context): netlist nets when there is a schematic, else
    the board's; the checks' own rail, ground and voltage readings as the inference."""
    def build():
        names, members, types = [], {}, {}
        if ctx.available("netlist"):
            nl = ctx.netlist
            members = dict(nl.nets)
            names = list(nl.nets)
            types = {k: v.get("type", "") for k, v in nl.pin_info.items()}
        elif ctx.available("pcb"):
            b = ctx.board
            for fp in b.footprints.values():
                for p in fp.pads:
                    if p.net:
                        members.setdefault(p.net, []).append((fp.ref, p.num))
                        if p.pintype:
                            types[(fp.ref, p.num)] = p.pintype
            names = sorted(set(members) | set(b.nets))
        try:
            grounds, power = ctx.ground_nets(), ctx.power_nets()
        except Exception:
            grounds, power = None, None
        volts = None
        if ctx.available("netlist"):
            try:
                from .checks.power import rail_voltages
                volts = rail_voltages(ctx)
            except Exception:
                volts = None
        inferred = _infer(names, members, types, ctx.cfg, power, grounds, volts)
        return NetModel(names, declared(ctx.cfg, ctx.p), inferred, members)
    return ctx._get("netmodel", build) if hasattr(ctx, "_get") else build()


def for_project(project):
    """The model for a project outside a check run (the app's net list, the inspector)."""
    from .checks.context import Context
    return for_context(Context(project, offline=True))


# ----------------------------------------------------------------------------- declaring
def declare(project, net, **fields):
    """Set (or with None, remove) declared fields of a net in tracewright.json "nets"; returns the record."""
    cfg = project.cfg
    nets = cfg.setdefault("nets", {})
    rec = dict(nets.get(net) or {})
    for k, v in fields.items():
        if k not in FIELDS:
            raise ValueError(f"unknown field {k}; fields: {', '.join(FIELDS)}")
        if v is None or v == "":
            rec.pop(k, None)
        else:
            rec[k] = v
    rec = _clean(rec)
    if rec:
        nets[net] = rec
    else:
        nets.pop(net, None)
    if not nets:
        cfg.pop("nets", None)
    _save(project)
    return rec


def _save(project):
    path = os.path.join(project.root, "tracewright.json")
    with open(path) as f:
        disk = json.load(f)
    disk["nets"] = project.cfg.get("nets") or {}
    if not disk["nets"]:
        disk.pop("nets")
    with open(path, "w") as f:
        json.dump(disk, f, indent=2)
        f.write("\n")


# ----------------------------------------------------------------------------- net classes
def _bisect(fn, target, lo, hi, n=60):
    """x in [lo, hi] with fn(x) = target for a decreasing fn."""
    for _ in range(n):
        mid = (lo + hi) / 2
        if fn(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _up(x, step=0.05):
    return round(math.ceil(x / step - 1e-9) * step, 3)


def suggest_classes(model, board, rise_c=10.0, min_w=0.2, clearance=0.2, pro=None):
    """Net classes the declarations call for: {class: {track_width, clearance, via_diameter, via_drill,
    diff_pair_width?, diff_pair_gap?}} and {net short name: class}. A supply with a current gets the
    IPC-2221 width on its outer layers (10 C rise, the board's copper weight); a pair or RF line with an
    impedance gets the width (and gap) for it over the board's stackup. A net's declared "class" wins."""
    from .checks.power import ipc2221_width
    from .checks.signal import z_microstrip, zdiff
    t_out = 0.035
    h, er = 0.2104, 4.4
    if board is not None:
        try:
            t_out = board.copper_mm("F.Cu") or 0.035
        except Exception:
            pass
        try:
            layers = list(board.copper)
            d = board.dielectric_between(layers[0], layers[1]) if len(layers) > 1 else None
            if d:
                h, er = d[0], d[1]
            elif len(layers) == 2:
                h, er = max(0.2, board.thickness - 0.07), 4.5
        except Exception:
            pass
    classes, assign = {}, {}
    have = lambda net: pro.cls(pro.class_of(net)) if pro is not None else {"track_width": min_w}
    for n, r in sorted(model.records.items()):
        s = short(n)
        want = r.get("class") if r.get("source", {}).get("class") != "inferred" else None
        if r.get("current") and r["kind"] in ("power", "signal", "ground"):
            amps = r["current"]
            w = max(min_w, _up(ipc2221_width(amps, rise_c, t_out)))
            cur = have(n)
            if not want and float(cur.get("track_width") or 0) >= w - 1e-6:
                continue                             # its class is already wide enough
            name = want or f"Power {amps:g} A"
            c = classes.setdefault(name, {"track_width": w, "clearance": max(clearance, 0.2 if amps < 3 else 0.25),
                                          "via_diameter": 0.8 if amps >= 1 else 0.6, "via_drill": 0.4 if amps >= 1 else 0.3})
            c["track_width"] = max(c["track_width"], w)
            assign[s] = name
        elif r["kind"] == "pair" and (r.get("impedance") or (model.get(r.get("pair") or "") or {}).get("impedance")):
            z = r.get("impedance") or model.get(r["pair"])["impedance"]          # one half declared: both halves
            gap = 0.15
            w = _bisect(lambda x: zdiff(x, gap, h, t_out, er), z, 0.05, 2.0)
            name = want or f"{(r.get('iface') or 'pair').upper()} {z:g} ohm"
            c = classes.setdefault(name, {"track_width": _up(w, 0.01), "clearance": clearance, "via_diameter": 0.6,
                                          "via_drill": 0.3, "diff_pair_width": _up(w, 0.01), "diff_pair_gap": gap})
            if w > 0.6:
                c["_note"] = (f"{w:.2f} mm tracks for {z:g} ohm over {h:.2f} mm of dielectric: wider than most connector pads. "
                              "A four-layer stack-up (thin dielectric to the plane) brings it near 0.2 mm; USB full speed "
                              "works without impedance control.")
            assign[s] = name
        elif r["kind"] == "rf" and r.get("impedance"):
            z = r["impedance"]
            w = _bisect(lambda x: z_microstrip(x, h, t_out, er), z, 0.05, 3.0)
            name = want or f"RF {z:g} ohm"
            c = classes.setdefault(name, {"track_width": _up(w, 0.01), "clearance": max(clearance, 0.25), "via_diameter": 0.6,
                                          "via_drill": 0.3})
            if w > 1.0:
                c["_note"] = (f"{w:.2f} mm for {z:g} ohm over {h:.2f} mm of dielectric: a coplanar waveguide (ground pour "
                              "close on both sides) narrows it.")
            assign[s] = name
        elif want:
            assign[s] = want
    return classes, assign


def apply_classes(pro_path, classes, assign):
    """Write the classes and their net patterns into the .kicad_pro (existing classes of the same name are
    updated, others kept; a pattern that already names the net is replaced). Returns what changed."""
    with open(pro_path) as f:
        data = json.load(f)
    ns = data.setdefault("net_settings", {})
    have = ns.setdefault("classes", [])
    by = {c.get("name"): c for c in have}
    base = dict(by.get("Default") or {})
    changed = []
    for name, spec in classes.items():
        c = by.get(name)
        if c is None:
            c = {k: v for k, v in base.items() if k not in ("name", "priority")}
            c["name"] = name
            have.append(c)
            by[name] = c
            changed.append(f"class {name}")
        for k, v in spec.items():
            if k.startswith("_"):                        # notes for the reader, not KiCad settings
                continue
            if c.get(k) != v:
                c[k] = v
                if f"class {name}" not in changed:
                    changed.append(f"class {name}")
    pats = ns.setdefault("netclass_patterns", []) or []
    for net, name in sorted(assign.items()):
        if any(p.get("pattern") == net and p.get("netclass") == name for p in pats):
            continue
        pats[:] = [p for p in pats if p.get("pattern") not in (net, f"*{net}", f"*/{net}")]
        pats.append({"netclass": name, "pattern": net})
        changed.append(f"{net} -> {name}")
    ns["netclass_patterns"] = pats
    with open(pro_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return changed
