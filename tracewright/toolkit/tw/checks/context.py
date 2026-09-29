"""Everything a check may read, produced once per run and cached by file time.

ctx.board      tw.board.Board of the .kicad_pcb          (needs 'pcb')
ctx.netlist    tw.netlist.Netlist (kicad-cli export)      (needs 'netlist')
ctx.hier       tw.schematic.Hierarchy                     (needs 'sch')
ctx.pro        tw.pro.ProjectSettings of the .kicad_pro
ctx.erc        kicad-cli ERC JSON                         (needs 'erc')
ctx.drc        kicad-cli DRC JSON with schematic parity  (needs 'drc')
ctx.svgs       {sheet name path: (SvgDoc, svg path)}      (needs 'svg')
ctx.fab        fab capabilities in force (tw.dfm preset + tracewright.json fab.rules)
"""
import os, glob, json, time, hashlib, threading
from .. import env, kicad
from ..board import Board
from ..netlist import Netlist
from ..schematic import Hierarchy
from ..pro import ProjectSettings


def _mtime(paths):
    ts = [os.path.getmtime(p) for p in paths if p and os.path.exists(p)]
    return max(ts) if ts else 0.0


class Context:
    def __init__(self, project=None, refresh=False, offline=None):
        self.p = project or env.project()
        self.refresh = refresh
        self.offline = bool(os.environ.get("TW_OFFLINE")) if offline is None else offline
        self._cache = {}
        self.cfg = self.p.cfg
        self.notes = []
        self.stop = threading.Event()        # set to stop the run (the app's Stop button, an interrupted turn)
        self.abandoned = []                  # checks whose thread timed out or was left running at a stop
        self.scopes = {}                     # check id -> what it examined ("3 regulators"), shown with its result

    def stopped(self):
        return self.stop.is_set()

    # ------------------------------------------------------------------ availability
    def available(self, need):
        p = self.p
        k = env.kicad()
        return {
            "sch": p.has_sch(),
            "pcb": p.has_pcb(),
            "netlist": p.has_sch() and bool(k["cli"]),
            "erc": p.has_sch() and bool(k["cli"]),
            "drc": p.has_pcb() and bool(k["cli"]),
            "svg": p.has_sch() and bool(k["cli"]),
            "network": not self.offline,
            "assembly": bool(self.setting("fab.assembly", True)),
        }.get(need, True)

    def setting(self, dotted, default=None):
        return self.p.setting(dotted, default)

    def _get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _stale(self, out, inputs):
        return self.refresh or not os.path.exists(out) or os.path.getmtime(out) < _mtime(inputs)

    # ------------------------------------------------------------------ inputs
    @property
    def board(self):
        return self._get("board", lambda: Board.load(self.p.pcb))

    @property
    def hier(self):
        return self._get("hier", lambda: Hierarchy.load(self.p.sch))

    @property
    def pro(self):
        return self._get("pro", lambda: ProjectSettings.load(self.p.pro) if self.p.pro else ProjectSettings({}))

    @property
    def netlist_path(self):
        return os.path.join(self.p.build, f"{self.p.stem}.net")

    @property
    def netlist(self):
        def make():
            out = self.netlist_path
            if self._stale(out, self.p.sheets() + [self.p.pro]):
                kicad.netlist(self.p.sch, out)
            return Netlist.load(out)
        return self._get("netlist", make)

    @property
    def erc(self):
        def make():
            out = os.path.join(self.p.build, "erc.json")
            if self._stale(out, self.p.sheets() + [self.p.pro]):
                return kicad.erc(self.p.sch, out)
            with open(out) as f:
                return json.load(f)
        return self._get("erc", make)

    @property
    def drc(self):
        def make():
            out = os.path.join(self.p.build, "drc.json")
            ins = [self.p.pcb, self.p.pro] + self.p.sheets()
            dru = os.path.splitext(self.p.pcb)[0] + ".kicad_dru"
            ins.append(dru)
            if self._stale(out, ins):
                return kicad.drc(self.p.pcb, out, parity=self.p.has_sch())
            with open(out) as f:
                return json.load(f)
        return self._get("drc", make)

    @property
    def svg_dir(self):
        return os.path.join(self.p.build, "sch_svg")

    @property
    def svgs(self):
        def make():
            from ..svg import SvgDoc
            d = self.svg_dir
            files = sorted(glob.glob(os.path.join(d, "*.svg")))
            if not files or self._stale(files[0], self.p.sheets() + [self.p.pro]) or \
                    min(os.path.getmtime(f) for f in files) < _mtime(self.p.sheets()):
                files = kicad.sch_svg(self.p.sch, d, drawing_sheet=False, theme="_builtin_default")
            return map_svgs(self.hier, files, SvgDoc.load)
        return self._get("svgs", make)

    @property
    def fab(self):
        from .. import dfm
        return self._get("fab", lambda: dfm.capabilities(self))

    def waivers(self):
        def make():
            w = self.setting("checks.waive", []) or []
            return {x["key"] if isinstance(x, dict) else str(x) for x in w}
        return self._get("waivers", make)

    def inputs_summary(self):
        out = {}
        for label, path in (("schematic", self.p.sch), ("board", self.p.pcb), ("project", self.p.pro)):
            if path and os.path.exists(path):
                out[label] = {"file": os.path.relpath(path, self.p.root),
                              "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(path)))}
        return out

    # ------------------------------------------------------------------ helpers for checks
    def ground_nets(self):
        """Nets that are ground (by name, or declared in tracewright.json checks.ground)."""
        declared = set(self.setting("checks.ground", []) or [])
        out = set()
        names = set(self.netlist.nets_short()) if self.available("netlist") else set(self.board.nets)
        for n in names:
            u = n.upper()
            if n in declared or u in ("GND", "AGND", "DGND", "PGND", "GNDA", "GNDD", "GNDPWR", "VSS", "0V", "EARTH",
                                     "GND_ISO", "SGND", "CHASSIS", "GNDREF") or u.startswith("GND"):
                out.add(n)
        return out

    def power_nets(self):
        """Nets that are supply rails: named like one (+3V3, V3V3, P12V, VIN_12V, USB_VBUS, VCC_IO), fed by
        a power-output pin, feeding an IC's power-input pin, or reached from a rail through a fuse,
        ferrite bead or 0-ohm link (VIN_FUSED). Names marking a control or sense node (V3V3_FB,
        PWR_EN, VIN_SENSE) are not rails. tracewright.json checks.rails adds, checks.not_rails removes."""
        return self._get("power_nets", self._power_nets)

    def _power_nets(self):
        import re
        declared = set((self.setting("checks.currents", {}) or {}).keys()) | set(self.setting("checks.rails", []) or [])
        not_rails = set(self.setting("checks.not_rails", []) or [])
        grounds = self.ground_nets()
        name = re.compile(r"^[+-]?\d+V\d*|\d+V\d+|\d+V($|_)|(^|_)V(CC|DD|BUS|IN|BAT|SYS|MOT|MOTOR|CORE|IO|SUP|LED|AA|PP|S)"
                          r"[A-Z0-9]*($|_)|(^|_)(AVDD|DVDD|AVCC|DVCC|IOVDD|PVIN|AVIN|PWR|VBUS)($|_)|^\+", re.I)
        control = re.compile(r"(^|_)(FB|SNS|SENSE|ADJ|EN|ENABLE|PG|PGOOD|GOOD|SW|LX|BST|BOOT|GATE|CTRL|DET|PRESENT|"
                             r"MON|ADC|SET|ILIM|SS|TRK|COMP|RT|OK|FLT|FAULT|INT|ON|OFF|LED_A|LED_K|BUTTON|BTN|KEY|REQ|ACK|"
                             r"CTL|CMD|STBY|STANDBY|SLEEP|WAKE|RUN|RST|RESET|IRQ|ALERT|STAT|STATUS)($|_)", re.I)
        out = set(declared)
        if not self.available("netlist"):
            return out - grounds
        nl = self.netlist
        by_short = {}
        for full, nodes in nl.nets.items():
            s = nl.short(full)
            if not s or s == "NC":
                continue
            by_short.setdefault(s, []).extend(nodes)
        for s, nodes in by_short.items():
            label = plain_name(s)
            if control.search(label):
                continue
            if name.search(label) or any(nl.pin_type(r, p) == "power_out" for r, p in nodes) or \
                    any(nl.pin_type(r, p) == "power_in" and r[:1] in ("U", "I") for r, p in nodes):
                out.add(s)
        # through fuses, ferrite beads and 0-ohm links
        links = []
        for ref, part in nl.parts.items():
            pins = nl.pins_of(ref)
            if len(pins) != 2 or part.get("dnp"):
                continue
            v = part.get("value", "").lower().replace(" ", "")
            pre = ref.rstrip("0123456789").upper()
            fpl = part.get("footprint", "").lower()
            if pre in ("F", "FUSE", "PTC", "FB") or (pre == "L" and ("bead" in v or "ferrite" in fpl or "bead" in fpl)) or \
                    (pre == "R" and v in ("0", "0r", "0ohm", "0ohms", "0Ω")) or pre in ("JP", "SJ") and False:
                a, b = nl.net_of(ref, pins[0]), nl.net_of(ref, pins[1])
                if a and b and "NC" not in (a, b):
                    links.append((a, b))
        grew = True
        while grew:
            grew = False
            for a, b in links:
                for x, y in ((a, b), (b, a)):
                    if x in out and y not in out and y not in grounds and not control.search(plain_name(y)):
                        out.add(y)
                        grew = True
        return (out - grounds) - not_rails


def plain_name(net):
    """KiCad's automatic net names reduced to the pin name they carry: 'Net-(J201D-SD_PWR_ON)' -> 'SD_PWR_ON',
    'Net-(J201A-CM5_1.8V-Pad88)' -> 'CM5_1.8V', 'Net-(U101-VCC)' -> 'VCC'."""
    import re
    m = re.match(r"^(?:unconnected-)?Net-\((?:[A-Za-z#]+\d+[A-Za-z]?-)?(.*?)(?:-Pad[A-Za-z]*\d+)?\)$", net or "")
    return m.group(1) if m else net


def sheet_svg_names(h):
    """Expected kicad-cli plot file stem for each sheet instance: <project>[-<sheet>-<sheet>...]."""
    out = {}
    for sh in h.sheets:
        parts = [p for p in sh.name_path.strip("/").split("/") if p]
        out[sh.name_path] = "-".join([h.project] + parts)
    return out


def map_svgs(h, files, loader):
    """{sheet name path: (SvgDoc, file)}: by the file name KiCad uses, else by the 'Sheet: /path/'
    text of the drawing sheet."""
    names = sheet_svg_names(h)
    by_stem = {os.path.splitext(os.path.basename(f))[0]: f for f in files}
    out, used = {}, set()
    for np_, stem in names.items():
        f = by_stem.get(stem) or by_stem.get(stem.replace("/", "_"))
        if f:
            out[np_] = (loader(f), f)
            used.add(f)
    for f in files:
        if f in used:
            continue
        doc = loader(f)
        for t in doc.texts:
            if t.text.startswith("Sheet: "):
                np_ = t.text[7:].strip()
                if np_ in names and np_ not in out:
                    out[np_] = (doc, f)
                break
    return out
