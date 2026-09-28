"""KiCad netlist (kicadsexpr, from `kicad-cli sch export netlist`) as plain data.

    nl = Netlist.load("build/x.net")
    nl.parts["U1"]            {'value', 'footprint', 'fields', 'lib', 'part', 'sheet', 'dnp', 'in_bom'}
    nl.net_of("U1", "3")      short net name ('' = not on any net, 'NC' = its own unconnected net)
    nl.nets["GND"]            [(ref, pin), ...]
    nl.pin_info["U1", "3"]    {'name', 'type'[, 'nc_flag']} from the node's pinfunction / pintype (or libparts)
    nl.net_class["GND"]       the net class KiCad assigned
"""
import copy
from .sexp import parse, find, findall, value


class Netlist:
    def __init__(self):
        self.parts = {}
        self.pin = {}          # (ref, pin) -> full net name
        self.nets = {}         # full net name -> [(ref, pin)]
        self.net_class = {}
        self.pin_info = {}     # (ref, pin) -> {'name', 'type'}
        self.libpins = {}      # (lib, part) -> {pin: (name, type)}

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls.parse(f.read())

    @classmethod
    def parse(cls, text):
        t = parse(text)
        nl = cls()
        lp = find(t, "libparts")
        for part in (findall(lp, "libpart") if lp else []):
            key = (str(value(part, "lib", "")), str(value(part, "part", "")))
            pins = {}
            pn = find(part, "pins")
            for p in (findall(pn, "pin") if pn else []):
                pins[str(value(p, "num", ""))] = (str(value(p, "name", "")), str(value(p, "type", "")))
            nl.libpins[key] = pins
        comps = find(t, "components")
        for c in (findall(comps, "comp") if comps else []):
            ref = str(value(c, "ref", ""))
            fields = {}
            fl = find(c, "fields")
            if fl:
                for f in findall(fl, "field"):
                    fields[str(value(f, "name", ""))] = str(f[2]) if len(f) > 2 and not isinstance(f[2], list) else ""
            props = {}
            for pr in findall(c, "property"):
                n = value(pr, "name")
                if n is not None:
                    props[str(n)] = str(value(pr, "value", "") or "")
            ls = find(c, "libsource")
            sp = find(c, "sheetpath")
            nl.parts[ref] = {
                "value": str(value(c, "value", "")),
                "footprint": str(value(c, "footprint", "") or ""),
                "datasheet": str(value(c, "datasheet", "") or ""),
                "description": str(value(c, "description", "") or ""),
                "fields": fields,
                "props": props,
                "lib": str(value(ls, "lib", "")) if ls else "",
                "part": str(value(ls, "part", "")) if ls else "",
                "sheet": str(value(sp, "names", "")) if sp else "",
                "dnp": "dnp" in props,
                "in_bom": "exclude_from_bom" not in props,
                "on_board": "exclude_from_board" not in props,
                "tstamp": str(value(c, "tstamps", "") or ""),
            }
        nets = find(t, "nets")
        for n in (findall(nets, "net") if nets else []):
            name = str(value(n, "name", ""))
            cls_ = value(n, "class")
            if cls_ is not None:
                nl.net_class[name] = str(cls_)
            nodes = []
            for nd in findall(n, "node"):
                ref, pin = str(value(nd, "ref", "")), str(value(nd, "pin", ""))
                nodes.append((ref, pin))
                nl.pin[(ref, pin)] = name
                pf, pt = value(nd, "pinfunction"), value(nd, "pintype")
                info = nl.pin_info.setdefault((ref, pin), {"name": "", "type": ""})
                if pf is not None:
                    pf = str(pf)
                    if pf.endswith("_" + pin) and len(pf) > len(pin) + 1:
                        pf = pf[:-len(pin) - 1]          # KiCad 10 writes "<name>_<number>" ("VCC_8")
                    info["name"] = pf
                if pt is not None:
                    pt = str(pt)
                    if pt.endswith("+no_connect"):             # a pin carrying a no-connect flag
                        pt = pt[:-len("+no_connect")]
                        info["nc_flag"] = True
                    info["type"] = pt
            nl.nets[name] = nodes
        for (ref, pin), info in nl.pin_info.items():                 # fill gaps from libparts
            p = nl.parts.get(ref)
            if p and not info["name"]:
                lpins = nl.libpins.get((p["lib"], p["part"]), {})
                if pin in lpins:
                    info["name"] = lpins[pin][0]
                    info["type"] = info["type"] or lpins[pin][1]
        return nl

    @staticmethod
    def short(name):
        if name is None:
            return None
        if name.startswith("unconnected-"):
            return "NC"
        return name.rsplit("/", 1)[-1] if "/" in name else name

    def net_of(self, ref, pin):
        return self.short(self.pin.get((ref, str(pin))))

    def pins_of(self, ref):
        return sorted([p for (r, p) in self.pin if r == ref], key=lambda s: (len(s), s))

    def pin_name(self, ref, pin):
        return self.pin_info.get((ref, str(pin)), {}).get("name", "")

    def pin_type(self, ref, pin):
        return self.pin_info.get((ref, str(pin)), {}).get("type", "")

    def nets_short(self):
        out = {}
        for full, nodes in self.nets.items():
            out.setdefault(self.short(full), []).extend(nodes)
        return out

    def mutated(self, changes):
        """A copy with {(ref, pin): net} replaced (planted-fault tests)."""
        m = copy.copy(self)
        m.pin = dict(self.pin)
        m.pin.update({(r, str(p)): v for (r, p), v in changes.items()})
        m.nets = {}
        for (r, p), n in m.pin.items():
            m.nets.setdefault(n, []).append((r, p))
        return m
