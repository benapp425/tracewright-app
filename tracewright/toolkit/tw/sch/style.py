"""A schematic drawn either way: strictly hierarchical, or flat.

Hierarchical: sheets joined through sheet pins and hierarchical labels, the way KiCad's hierarchy is
meant to be used -- the parent sheet shows which signals cross between which sheets. Flat: pages
joined by global labels of the same name, the way many engineers draw. Supplies are power symbols in
both. `convert(sch, "flat" | "hierarchical")` redraws one as the other and proves it: KiCad's own
netlist of the result must put every pin on the same net as before (only net names may change), or
nothing is written.

KiCad's connection rules, measured with kicad-cli (KiCad 10; tests: sheet_connectivity_rules):
  * wires join at shared end points and at junction dots; a wire that ends on the middle of another
    does not join it without a dot, and crossing wires never do;
  * a pin joins wire ends, pins and labels at its point (not the middle of a wire);
  * a label joins any wire it lies on, at an end or in the middle, and a pin at its point;
  * on one sheet, labels of one name join: local and hierarchical ones, and a global one;
  * global labels and power symbols join by name across all sheets; a local label joins a global
    label or a power symbol of its name only on the same sheet;
  * a hierarchical label joins the sheet pin of its name on its sheet's symbol in the parent.

    from tw.sch import style
    style.detect("hardware/x/x.kicad_sch")              {"style": "hierarchical", ...}
    style.convert("hardware/x/x.kicad_sch", "flat")     {"ok": True, "changed": [...], "proof": {...}}
"""
import os, re, shutil, tempfile, collections, uuid as _uuid
from .. import sexp, kicad
from ..sexp import find, findall, value
from ..schematic import Hierarchy
from ..netlist import Netlist

UNIT = 10000                   # file coordinates are mm to 4 decimals; points compare on this grid
STYLES = ("hierarchical", "flat")


class Refused(Exception):
    """The conversion cannot be done safely; the message says why, in the user's terms."""


def _k(x, y):
    return (round(float(x) * UNIT), round(float(y) * UNIT))


def _mm(v):
    s = f"{v / UNIT:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _at(node):
    a = find(node, "at")
    if not a:
        return 0.0, 0.0, 0.0
    return float(a[1]), float(a[2]), float(a[3]) if len(a) > 3 else 0.0


def _font_size(node):
    e = find(node, "effects")
    f = find(e, "font") if e else None
    s = find(f, "size") if f else None
    try:
        return float(s[1]) if s else 1.27
    except (TypeError, ValueError):
        return 1.27


def _q(s):
    return sexp.atom_text(sexp.Q(s))


def _uid():
    return str(_uuid.uuid4())


class _DSU:
    def __init__(self):
        self.parent = {}

    def find(self, a):
        p = self.parent
        if a not in p:
            p[a] = a
            return a
        root = a
        while p[root] != root:
            root = p[root]
        while p[a] != root:
            p[a], a = root, p[a]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def keys(self):
        return list(self.parent)


# ----------------------------------------------------------------------------- one file
class _Doc:
    """One .kicad_sch file, parsed with spans so edits change only what they must."""

    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.text = f.read()
        self.tree = sexp.parse(self.text, spans=True)
        self.edits = []                       # (node, replacement text)
        self.adds = []                        # new top-level items
        self.wires = []                       # (node, a, b) per segment, points on the UNIT grid
        for w in findall(self.tree, "wire"):
            pts = [_k(q[1], q[2]) for q in findall(find(w, "pts") or [], "xy")]
            for a, b in zip(pts, pts[1:]):
                self.wires.append((w, a, b))
        self.junctions = [(n, _k(*_at(n)[:2])) for n in findall(self.tree, "junction")]
        self.labels = []
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in findall(self.tree, kind):
                x, y, r = _at(n)
                self.labels.append({"node": n, "kind": kind, "text": str(n[1]), "pt": _k(x, y), "rot": r,
                                    "shape": str(value(n, "shape", "") or "passive"), "size": _font_size(n)})
        self.sheets = {}                      # child sheet uuid -> its symbol on this page
        for n in findall(self.tree, "sheet"):
            x, y, _ = _at(n)
            sz = find(n, "size")
            pins = []
            for p in findall(n, "pin"):
                px, py, pr = _at(p)
                pins.append({"node": p, "name": str(p[1]), "shape": str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else "passive",
                             "pt": _k(px, py), "rot": pr, "size": _font_size(p)})
            self.sheets[str(value(n, "uuid", ""))] = {"node": n, "at": _k(x, y), "size": _k(float(sz[1]), float(sz[2])) if sz else (0, 0),
                                                      "pins": pins}
        self.buses = bool(findall(self.tree, "bus") or findall(self.tree, "bus_entry")) or \
            any("[" in l["text"] or "{" in l["text"] for l in self.labels)
        self._h, self._v, self._d = collections.defaultdict(list), collections.defaultdict(list), []
        for i, (_, a, b) in enumerate(self.wires):
            if a[1] == b[1]:
                self._h[a[1]].append(i)
            elif a[0] == b[0]:
                self._v[a[0]].append(i)
            else:
                self._d.append(i)

    def wires_through(self, pt):
        """Segments that contain pt, at an end or in the middle."""
        x, y = pt
        out = []
        for i in self._h.get(y, ()):
            _, a, b = self.wires[i]
            if min(a[0], b[0]) <= x <= max(a[0], b[0]):
                out.append(i)
        for i in self._v.get(x, ()):
            _, a, b = self.wires[i]
            if min(a[1], b[1]) <= y <= max(a[1], b[1]):
                out.append(i)
        for i in self._d:
            _, a, b = self.wires[i]
            if min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]) and \
                    abs((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])) <= max(abs(b[0] - a[0]), abs(b[1] - a[1])):
                out.append(i)
        return out

    def result(self):
        """The file's new text, or None when nothing changed."""
        if not self.edits and not self.adds:
            return None
        edits = list(self.edits)
        if self.adds:
            end = self.tree.span[1] - 1                    # the document's closing parenthesis
            edits.append(((end, end), "".join("\t" + a.replace("\n", "\n\t") + "\n" for a in self.adds)))
        return sexp.splice(self.text, edits)


# ----------------------------------------------------------------------------- connectivity
def _pins(sh):
    """(item, symbol, number, name, point, global name) for every pin on one sheet instance. A power
    input pin joins a net by name: on a power symbol the symbol's value names it, on another part a
    hidden one is named by the pin (KiCad's implicit connection). PWR_FLAG's pin is an output: it
    names nothing."""
    out = []
    for si, s in enumerate(sh.symbols):
        lib_pins = s.lib.unit_pins(s.unit, s.style) if s.lib else []
        for (num, name, typ, x, y), lp in zip(s.pins, lib_pins):
            glob = None
            if typ == "power_in" and (s.is_power or lp.get("hidden")):
                glob = s.value if s.is_power else name
            out.append((("p", si, num), s, num, name, _k(x, y), glob))
    return out


def _local(doc, sh):
    """What touches what on one sheet instance, by geometry alone."""
    d = _DSU()
    at = collections.defaultdict(list)
    for i, (_, a, b) in enumerate(doc.wires):
        d.find(("w", i))
        at[a].append(("w", i))
        at[b].append(("w", i))
    for it, *_rest in _pins(sh):
        pt = _rest[3]
        d.find(it)
        at[pt].append(it)
    for i, lb in enumerate(doc.labels):
        d.find(("l", i))
        at[lb["pt"]].append(("l", i))
    for cu, sd in doc.sheets.items():
        for j, pn in enumerate(sd["pins"]):
            d.find(("s", cu, j))
            at[pn["pt"]].append(("s", cu, j))
    for i, (_, pt) in enumerate(doc.junctions):
        d.find(("j", i))
        at[pt].append(("j", i))
    for items in at.values():
        for it in items[1:]:
            d.union(items[0], it)
    for i, lb in enumerate(doc.labels):                    # labels and dots join the wires they lie on
        for wi in doc.wires_through(lb["pt"]):
            d.union(("l", i), ("w", wi))
    for i, (_, pt) in enumerate(doc.junctions):
        for wi in doc.wires_through(pt):
            d.union(("j", i), ("w", wi))
    return d


class Design:
    """A schematic hierarchy read for conversion: its files, instances and connectivity."""

    def __init__(self, sch):
        self.sch = os.path.abspath(sch)
        self.h = Hierarchy.load(self.sch)
        self.docs = {f: _Doc(f) for f in self.h.files}
        self.inst = {sh.path: sh for sh in self.h.sheets}
        self.count = collections.Counter(sh.file for sh in self.h.sheets)
        self.local = {sh.path: _local(self.docs[sh.file], sh) for sh in self.h.sheets}
        self.power = {g for sh in self.h.sheets for (_, s, _, _, _, g) in _pins(sh) if g and s.is_power}
        D = self.D = _DSU()
        self.pinkeys = []
        for sh in self.h.sheets:
            P, doc, loc = sh.path, self.docs[sh.file], self.local[sh.path]
            for it in loc.keys():
                D.union((P,) + loc.find(it), (P,) + it)
            for it, s, num, name, pt, glob in _pins(sh):
                if not s.is_power:
                    D.union((P,) + it, ("P", s.ref, num))
                    self.pinkeys.append((s.ref, num))
                if glob:
                    D.union((P,) + it, ("G", glob))
                    if s.is_power:                         # like a global label: same-name labels on its sheet join it
                        D.union((P,) + it, ("N", P, glob))
            for i, lb in enumerate(doc.labels):
                it = (P, "l", i)
                D.union(it, ("N", P, lb["text"]))
                if lb["kind"] == "global_label":
                    D.union(it, ("G", lb["text"]))
                elif lb["kind"] == "hierarchical_label" and sh.parent is not None:
                    D.union(it, ("H", P, lb["text"]))
            for cu, sd in doc.sheets.items():
                for j, pn in enumerate(sd["pins"]):
                    D.union((P, "s", cu, j), ("H", f"{P}/{cu}", pn["name"]))

    def partition(self):
        """The nets as sets of (reference, pin), from this reading of the sheets."""
        groups = collections.defaultdict(set)
        for ref, num in self.pinkeys:
            groups[self.D.find(("P", ref, num))].add((ref, num))
        return {frozenset(g) for g in groups.values()}

    def chain(self, path):
        """The instance and its ancestors, root first."""
        out, sh = [], self.inst[path]
        while sh is not None:
            out.append(sh.path)
            sh = sh.parent
        return out[::-1]

    def linked(self, path, i):
        """Is this hierarchical label joined to a sheet pin on its sheet's symbol?"""
        sh = self.inst[path]
        if sh.parent is None:
            return False
        cu = path.rsplit("/", 1)[1]
        text = self.docs[sh.file].labels[i]["text"]
        sd = self.docs[sh.parent.file].sheets.get(cu)
        return bool(sd and any(p["name"] == text for p in sd["pins"]))


def _netlist_partition(nl):
    out = {}
    for name, nodes in nl.nets.items():
        g = frozenset((r, p) for r, p in nodes if not r.startswith("#"))
        if g:
            out[g] = name
    return out


def _export(sch, workdir):
    out = os.path.join(workdir, "proof.net")
    kicad.netlist(sch, out)
    return Netlist.load(out)


# ----------------------------------------------------------------------------- what style is it
def detect(sch):
    """How the sheets are joined: 'hierarchical' (sheet pins), 'flat' (global labels), 'mixed',
    'unlinked' (sub-sheets that share only supplies) or 'single' (one sheet)."""
    d = Design(sch)
    return _describe(d)


def _describe(d):
    subs = [sh for sh in d.h.sheets if sh.parent is not None]
    where = collections.defaultdict(set)
    hier = pins = 0
    for sh in d.h.sheets:
        doc = d.docs[sh.file]
        for lb in doc.labels:
            if lb["kind"] == "global_label" and lb["text"] not in d.power:
                where[lb["text"]].add(sh.path)
            if lb["kind"] == "hierarchical_label" and sh.parent is not None:
                hier += 1
        pins += sum(len(sd["pins"]) for sd in doc.sheets.values())
    crossing = sorted(n for n, p in where.items() if len(p) > 1)
    if not subs:
        st = "single"
    elif crossing and (pins or hier):
        st = "mixed"
    elif crossing or where:
        st = "flat"
    elif pins or hier:
        st = "hierarchical"
    else:
        st = "unlinked"
    return {"style": st, "sheets": len(d.h.sheets), "sheet_pins": pins, "hierarchical_labels": hier,
            "global_labels": sum(1 for sh in d.h.sheets for lb in d.docs[sh.file].labels if lb["kind"] == "global_label"),
            "global_nets": sorted(where), "crossing": crossing,
            "reused": sorted(os.path.basename(f) for f, n in d.count.items() if n > 1),
            "buses": any(doc.buses for doc in d.docs.values())}


# ----------------------------------------------------------------------------- new items
def _label(kind, text, pt, rot, shape="passive", size=1.27):
    """A label node's text. Local labels sit on the wire; the others carry a flag shape."""
    x, y, r = _mm(pt[0]), _mm(pt[1]), int(round(rot)) % 360
    if kind == "label":
        just = "left bottom" if r in (0, 90) else "right bottom"
        return (f'(label {_q(text)} (at {x} {y} {r}) (effects (font (size {size} {size})) (justify {just})) '
                f'(uuid {_q(_uid())}))')
    just = "left" if r in (0, 90) else "right"
    body = (f'({kind} {_q(text)} (shape {shape}) (at {x} {y} {r})' + (" (fields_autoplaced yes)" if kind == "global_label" else "")
            + f' (effects (font (size {size} {size})) (justify {just})) (uuid {_q(_uid())})')
    if kind == "global_label":
        body += (f' (property "Intersheetrefs" "${{INTERSHEET_REFS}}" (at {x} {y} 0) '
                 f'(effects (font (size {size} {size})) (justify {just}) (hide yes)))')
    return body + ")"


def _wire(a, b):
    return (f'(wire (pts (xy {_mm(a[0])} {_mm(a[1])}) (xy {_mm(b[0])} {_mm(b[1])})) (stroke (width 0) (type default)) '
            f'(uuid {_q(_uid())}))')


def _sheet_pin(name, shape, pt, side):
    rot, just = (180, "left") if side == "left" else (0, "right")
    return (f'(pin {_q(name)} {shape} (at {_mm(pt[0])} {_mm(pt[1])} {rot}) (uuid {_q(_uid())}) '
            f'(effects (font (size 1.27 1.27)) (justify {just})))')


SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")


def _shape(s):
    return s if s in SHAPES else "passive"


def _tag(name):
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "SHEET"


# ----------------------------------------------------------------------------- supply symbols
def _supplies(d):
    """For each supply in the design, a power symbol to copy: its library entry, lib_id and where its
    value text sits (from an instance drawn upright)."""
    out = {}
    for sh in d.h.sheets:
        doc = d.docs[sh.file]
        libs = find(doc.tree, "lib_symbols")
        defs = {str(n[1]): n for n in findall(libs, "symbol")} if libs else {}
        for s in sh.symbols:
            if not (s.is_power and s.value and s.lib) or s.value in out or s.rot or s.mirror:
                continue
            pins = s.lib.unit_pins(s.unit, s.style)
            lname = str(value(s.node, "lib_name", "") or s.lib_id)
            if len(pins) != 1 or pins[0]["type"] != "power_in" or lname not in defs:
                continue
            fx, fy = s.field_at.get("Value", (s.x, s.y - 3.81, 0, True))[:2]
            rx, ry = s.field_at.get("Reference", (s.x, s.y + 3.81, 0, False))[:2]
            node = defs[lname]
            out[s.value] = {"lib_id": s.lib_id, "lib_name": str(value(s.node, "lib_name", "") or ""), "def_name": lname,
                            "def": doc.text[node.span[0]:node.span[1]], "pin": pins[0]["number"],
                            "v": (fx - s.x, fy - s.y), "r": (rx - s.x, ry - s.y),
                            "box": (s.bbox[0] - s.x, s.bbox[1] - s.y, s.bbox[2] - s.x, s.bbox[3] - s.y)}
    return out


def _project_name(d):
    for doc in d.docs.values():
        for n in findall(doc.tree, "symbol"):
            inst = find(n, "instances")
            pr = find(inst, "project") if inst else None
            if pr:
                return str(pr[1])
    return os.path.splitext(os.path.basename(d.sch))[0]


def _supply_spot(t, name, pt, obstacles):
    """Where a supply symbol's value text can go clear of the page, or None when the symbol does not fit."""
    x, y = pt[0] / UNIT, pt[1] / UNIT
    bx = t["box"]
    body = (round((x + bx[0]) * UNIT), round((y + bx[1]) * UNIT), round((x + bx[2]) * UNIT), round((y + bx[3]) * UNIT))
    if any(_overlaps(body, o[2]) for o in obstacles):
        return None
    w = _text_w(name) / UNIT
    vx, vy = t["v"]
    for dx, dy, just in ((vx, vy, None), (bx[2] + 0.5, (bx[1] + bx[3]) / 2, "left"), (bx[0] - 0.5, (bx[1] + bx[3]) / 2, "right")):
        x0 = x + dx - (w / 2 if just is None else 0 if just == "left" else w)
        box = (round(x0 * UNIT), round((y + dy - 0.9) * UNIT), round((x0 + w) * UNIT), round((y + dy + 0.9) * UNIT))
        if not any(_overlaps(box, o[2]) for o in obstacles):
            return (dx, dy, just, [body, box])
    return None


def _power_symbol(t, name, pt, ref, path, project, spot=None):
    x, y = pt[0] / UNIT, pt[1] / UNIT
    f = lambda v: f"{v:.4f}".rstrip("0").rstrip(".")
    lib_name = f" (lib_name {_q(t['lib_name'])})" if t["lib_name"] else ""
    hid = "(effects (font (size 1.27 1.27)) (hide yes))"
    vx, vy = (spot[0], spot[1]) if spot else t["v"]
    vj = f" (justify {spot[2]})" if spot and spot[2] else ""
    return (f"(symbol (lib_id {_q(t['lib_id'])}){lib_name} (at {f(x)} {f(y)} 0) (unit 1) (exclude_from_sim no) (in_bom yes) "
            f"(on_board yes) (dnp no) (uuid {_q(_uid())}) "
            f"(property \"Reference\" {_q(ref)} (at {f(x + t['r'][0])} {f(y + t['r'][1])} 0) {hid}) "
            f"(property \"Value\" {_q(name)} (at {f(x + vx)} {f(y + vy)} 0) (effects (font (size 1.27 1.27)){vj})) "
            f"(property \"Footprint\" \"\" (at {f(x)} {f(y)} 0) {hid}) (property \"Datasheet\" \"\" (at {f(x)} {f(y)} 0) {hid}) "
            f"(property \"Description\" \"\" (at {f(x)} {f(y)} 0) {hid}) (pin {_q(t['pin'])} (uuid {_q(_uid())})) "
            f"(instances (project {_q(project)} (path {_q(path)} (reference {_q(ref)}) (unit 1)))))")


# ----------------------------------------------------------------------------- to flat
def _refuse_reuse(d):
    for f, n in d.count.items():
        doc = d.docs[f]
        if n > 1 and (any(l["kind"] == "hierarchical_label" for l in doc.labels) or any(sd["pins"] for sd in doc.sheets.values())):
            raise Refused(f"{os.path.basename(f)} is used {n} times. As flat pages its copies would share every "
                          "signal, so it has to stay hierarchical (or give each copy its own file first).")


def _plan_flat(d, nl):
    rep = {"relabelled": 0, "pins_removed": 0, "wires_removed": 0, "labels_added": 0, "renamed": {}}
    _refuse_reuse(d)
    D = d.D
    globals_of = collections.defaultdict(set)
    for k in D.keys():
        if k[0] == "G":
            globals_of[D.find(k)].add(k[1])
    names_at = collections.defaultdict(dict)                       # instance path -> label name -> net
    for k in D.keys():
        if k[0] == "N":
            names_at[k[1]][k[2]] = D.find(k)
    # every net that crosses a sheet boundary, where it will carry a global label, and its old names
    crossing = collections.defaultdict(lambda: {"paths": set(), "texts": [], "sheet": ""})
    for sh in d.h.sheets:
        doc = d.docs[sh.file]
        for i, lb in enumerate(doc.labels):
            if lb["kind"] == "hierarchical_label" and d.linked(sh.path, i):
                c = crossing[D.find((sh.path, "l", i))]
                c["paths"].add(sh.path)
                c["texts"].append(lb["text"])
                c["sheet"] = c["sheet"] or sh.name
    for sh in d.h.sheets:                                           # the parent pages of those nets
        for cu, sd in d.docs[sh.file].sheets.items():
            for j, pn in enumerate(sd["pins"]):
                net = D.find((sh.path, "s", cu, j))
                if net in crossing:
                    crossing[net]["paths"].add(sh.path)
                    crossing[net]["texts"].append(pn["name"])
    taken = {}
    gname = {}
    for net in sorted(crossing, key=lambda n: sorted(crossing[n]["texts"])):
        c = crossing[net]
        pref = sorted(globals_of[net] & d.power) or sorted(globals_of[net])
        cands = list(pref)
        pin = next(((r, p) for r, p in d.pinkeys if D.find(("P", r, p)) == net), None)
        kn = nl.net_of(*pin) if pin else None
        if kn and not kn.startswith(("Net-(", "unconnected-")) and kn != "NC":
            cands.append(kn)
        cands += sorted(set(c["texts"]), key=c["texts"].index)
        base = cands[0]
        cands += [f"{base}_{_tag(c['sheet'])}"] + [f"{base}_{k}" for k in range(2, 100)]

        def free(name):
            g = D.parent.get(("G", name)) is not None and D.find(("G", name)) != net
            here = any(names_at[p].get(name, net) != net for p in c["paths"])
            return not g and not here and taken.get(name, net) == net
        name = next(n for n in cands if free(n))
        taken[name] = net
        gname[net] = name
        if name not in c["texts"]:
            rep["renamed"][", ".join(sorted(set(c["texts"])))] = name
    # Hierarchical labels become global ones -- a supply becomes its power symbol, as drawn on a flat
    # page -- or local ones, when nothing links them to the parent.
    supplies, project = _supplies(d), _project_name(d)
    npwr = max([int(m.group(1)) for sh in d.h.sheets for s_ in sh.symbols for m in [re.match(r"#PWR0*(\d+)$", s_.ref)] if m] or [0])
    for sh in d.h.sheets:
        if sh.parent is None:
            continue
        doc = d.docs[sh.file]
        ends = {p for _, a, b in doc.wires for p in (a, b)} | {pt for *_, pt, _ in _pins(sh)}
        have_defs = {str(n[1]) for n in findall(find(doc.tree, "lib_symbols") or [], "symbol")}
        page = _obstacles(doc, sh)
        for i, lb in enumerate(doc.labels):
            if lb["kind"] != "hierarchical_label":
                continue
            if d.linked(sh.path, i):
                name = gname[D.find((sh.path, "l", i))]
                t = supplies.get(name)
                mine = _label_box("hierarchical_label", lb["text"], lb["pt"], lb["rot"], lb["size"])
                spot = _supply_spot(t, name, lb["pt"], [o for o in page if o[2] != mine]) if t else None
                if t and spot and lb["pt"] in ends and find(doc.tree, "lib_symbols") is not None:
                    npwr += 1
                    doc.edits.append((lb["node"], _power_symbol(t, name, lb["pt"], f"#PWR{npwr:03d}", sh.path, project, spot)))
                    page += [("symbol", name, b) for b in spot[3]]
                    if t["def_name"] not in have_defs:
                        libs = find(doc.tree, "lib_symbols")
                        doc.edits.append(((libs.span[1] - 1, libs.span[1] - 1), "\t" + t["def"] + "\n\t"))
                        have_defs.add(t["def_name"])
                    rep["supplies"] = rep.get("supplies", 0) + 1
                else:
                    doc.edits.append((lb["node"], _label("global_label", name, lb["pt"], lb["rot"], _shape(lb["shape"]), lb["size"])))
            else:
                doc.edits.append((lb["node"], _label("label", lb["text"], lb["pt"], lb["rot"], size=lb["size"])))
            rep["relabelled"] += 1
    # sheet pins go; wiring that only joined sheet pins goes with them; anything else they reached on the
    # parent page gets a global label where the pin was
    for sh in d.h.sheets:
        doc, loc = d.docs[sh.file], d.local[sh.path]
        members = collections.defaultdict(list)
        for it in loc.keys():
            members[loc.find(it)].append(it)
        label_groups = collections.defaultdict(set)
        for i, lb in enumerate(doc.labels):
            label_groups[lb["text"]].add(loc.find(("l", i)))
        sym_nodes = {str(value(n, "uuid", "")): n for n in findall(doc.tree, "symbol")}

        def plain(group):
            return all(it[0] in ("w", "j", "s") or (it[0] == "l" and doc.labels[it[1]]["kind"] == "label")
                       or (it[0] == "p" and sh.symbols[it[1]].is_power) for it in group)
        # Wiring whose only job was joining sheet pins: wires, dots, supply symbols on them, and local
        # labels whose name appears only in such wiring (decided for all of the page's groups together).
        pure = {loc.find(("s", cu, j)) for cu, sd in doc.sheets.items() for j in range(len(sd["pins"]))}
        pure = {g for g in pure if plain(members[g])}
        changed = True
        while changed:
            changed = False
            for g in list(pure):
                names = {doc.labels[it[1]]["text"] for it in members[g] if it[0] == "l"}
                if any(label_groups[n] - pure for n in names):
                    pure.discard(g)
                    changed = True

        def only_joins_pins(group):
            return bool(group) and loc.find(group[0]) in pure

        def stub(group):
            """A short wire from a sheet pin to a local label whose name the net also has elsewhere on
            the page: the stub can go, and one of those other labels become the global label."""
            return all(it[0] in ("w", "j", "s") or (it[0] == "l" and doc.labels[it[1]]["kind"] == "label") for it in group)
        dead_wires, dead_dots, dead_other = set(), set(), {}
        promote = {}                                     # net -> label name on this page that should turn global
        obstacles = [o for o in _obstacles(doc, sh) if o[0] != "sheet" or o[2]]
        attached = collections.defaultdict(list)         # point -> what connects there (a free wire end has one)
        for i, (_, a, b) in enumerate(doc.wires):
            attached[a].append(("w", i))
            attached[b].append(("w", i))
        for it, *_r in _pins(sh):
            attached[_r[3]].append(it)
        for i, lb in enumerate(doc.labels):
            attached[lb["pt"]].append(("l", i))
        for i, (_, pt) in enumerate(doc.junctions):
            attached[pt].append(("j", i))
        for cu, sd in doc.sheets.items():
            for j, pn in enumerate(sd["pins"]):
                attached[pn["pt"]].append(("s", cu, j))
        for cu, sd in doc.sheets.items():
            for j, pn in enumerate(sd["pins"]):
                doc.edits.append((pn["node"], ""))
                rep["pins_removed"] += 1
                group = members[loc.find(("s", cu, j))]
                net = D.find((sh.path, "s", cu, j))
                if only_joins_pins(group):
                    dead_wires |= {doc.wires[it[1]][0].span for it in group if it[0] == "w"}
                    dead_dots |= {it[1] for it in group if it[0] == "j"}
                    for it in group:
                        if it[0] == "l":
                            dead_other[doc.labels[it[1]]["node"].span] = doc.labels[it[1]]["node"]
                        elif it[0] == "p":
                            n = sym_nodes.get(sh.symbols[it[1]].uuid)
                            if n is not None:
                                dead_other[n.span] = n
                elif net in gname and stub(group) and all(doc.labels[it[1]]["text"] == gname[net] for it in group if it[0] == "l") \
                        and any(it[0] == "l" for it in group):
                    dead_wires |= {doc.wires[it[1]][0].span for it in group if it[0] == "w"}
                    dead_dots |= {it[1] for it in group if it[0] == "j"}
                    for it in group:
                        if it[0] == "l":
                            dead_other[doc.labels[it[1]]["node"].span] = doc.labels[it[1]]["node"]
                    promote[net] = gname[net]
                elif len(group) > 1 and net in gname and not any(
                        it[0] == "l" and doc.labels[it[1]]["kind"] != "label" and (
                            doc.labels[it[1]]["text"] == gname[net] if doc.labels[it[1]]["kind"] == "global_label"
                            else sh.parent is not None and d.linked(sh.path, it[1])) for it in group):
                    rot = 0 if pn["rot"] == 180 else 180                  # the flag lies over the sheet's edge
                    doc.adds.append(_label("global_label", gname[net], pn["pt"], rot, _shape(pn["shape"]), pn["size"]))
                    rep["labels_added"] += 1
        seen = set()
        for node, a, b in doc.wires:
            if node.span in dead_wires and node.span not in seen:
                seen.add(node.span)
                doc.edits.append((node, ""))
                rep["wires_removed"] += 1
        for i in dead_dots:
            doc.edits.append((doc.junctions[i][0], ""))
        going = set(promote.values())                    # local labels of these names are removed or moved
        obstacles = [o for o in obstacles if not (o[0] == "label" and o[1] in going)]
        for net, name in promote.items():                # the label that now carries the net to the other pages
            if any(lb["kind"] == "global_label" and lb["text"] == name and lb["node"].span not in dead_other for lb in doc.labels):
                continue
            cands = [(i, lb) for i, lb in enumerate(doc.labels) if lb["kind"] == "label" and lb["text"] == name
                     and lb["node"].span not in dead_other and D.find((sh.path, "l", i)) == net]
            # A global label's flag runs along its wire, so it goes on a free wire end, pointing out, rather
            # than over the middle of a wire where the local label's text sat above it.
            end = None
            for i, lb in cands:
                for wi in doc.wires_through(lb["pt"]):
                    node, a, b = doc.wires[wi]
                    for far, near in ((a, b), (b, a)):
                        if far != lb["pt"] and len(attached[far]) == 1 and (far[0] == near[0] or far[1] == near[1]):
                            rot = 0 if far[0] > near[0] else 180 if far[0] < near[0] else 270 if far[1] > near[1] else 90
                            end = (i, lb, far, rot)
                            break
                    if end:
                        break
                if end:
                    break
            placed = False
            if end:                                        # at the free end, the wire shortened if the flag needs room
                i, lb, far, rot = end
                wi = next(w for w in doc.wires_through(lb["pt"]) if far in doc.wires[w][1:])
                node, a, b = doc.wires[wi]
                near = a if far == b else b
                step = (PITCH // 2) * ((far[0] > near[0]) - (far[0] < near[0])), (PITCH // 2) * ((far[1] > near[1]) - (far[1] < near[1]))
                p_ = far
                while abs(p_[0] - near[0]) + abs(p_[1] - near[1]) >= PITCH:
                    box = _label_box("global_label", name, p_, rot, lb["size"])
                    if not any(_overlaps(box, o[2]) for o in obstacles if not (o[0] == "label" and o[1] == name)):
                        break
                    p_ = (p_[0] - step[0], p_[1] - step[1])
                else:
                    p_ = None
                pts = findall(find(node, "pts") or [], "xy")
                if p_ is not None and len(pts) == 2 and not (p_ != far and (p_ in attached or doc.wires_through(p_) != [wi])):
                    if p_ != far:
                        doc.edits.append((find(node, "pts"), f"(pts (xy {_mm(near[0])} {_mm(near[1])}) (xy {_mm(p_[0])} {_mm(p_[1])}))"))
                    doc.edits.append((lb["node"], ""))
                    doc.adds.append(_label("global_label", name, p_, rot, "bidirectional", lb["size"]))
                    obstacles.append(("label", name, _label_box("global_label", name, p_, rot, lb["size"])))
                    placed = True
            if not placed:                                 # a short tap off the wire, with a dot, carrying the flag
                for i, lb in cands:
                    wires = doc.wires_through(lb["pt"])
                    if not wires:
                        continue
                    _, a, b = doc.wires[wires[0]]
                    dirs = [(0, -1, 90), (0, 1, 270)] if a[1] == b[1] else [(1, 0, 0), (-1, 0, 180)]
                    for dx, dy, rot in dirs:
                        tip = (lb["pt"][0] + dx * PITCH, lb["pt"][1] + dy * PITCH)
                        box = _label_box("global_label", name, tip, rot, lb["size"])
                        if tip in attached or doc.wires_through(tip) or any(_overlaps(box, o[2]) for o in obstacles
                                                                              if not (o[0] == "label" and o[1] == name)):
                            continue
                        doc.edits.append((lb["node"], ""))
                        doc.adds.append(_wire(lb["pt"], tip))
                        if lb["pt"] not in (a, b):
                            doc.adds.append(f'(junction (at {_mm(lb["pt"][0])} {_mm(lb["pt"][1])}) (diameter 0) (color 0 0 0 0) (uuid {_q(_uid())}))')
                        doc.adds.append(_label("global_label", name, tip, rot, "bidirectional", lb["size"]))
                        obstacles.append(("label", name, box))
                        placed = True
                        break
                    if placed:
                        break
            if not placed and cands:                       # nowhere clear: the label turns global where it is
                keep = cands[0][1]
                doc.edits.append((keep["node"], _label("global_label", name, keep["pt"], keep["rot"], "bidirectional", keep["size"])))
            if cands or end:
                rep["labels_promoted"] = rep.get("labels_promoted", 0) + 1
            else:                                          # nothing to promote: keep one stub's label, as a global one
                span, node = next((sp, n) for sp, n in dead_other.items() if n[0] == "label" and str(n[1]) == name)
                del dead_other[span]
                lb = next(l for l in doc.labels if l["node"].span == span)
                doc.edits.append((node, _label("global_label", name, lb["pt"], lb["rot"], "bidirectional", lb["size"])))
        for node in dead_other.values():
            doc.edits.append((node, ""))
            rep["symbols_removed" if node[0] == "symbol" else "labels_removed"] = \
                rep.get("symbols_removed" if node[0] == "symbol" else "labels_removed", 0) + 1
    return rep


# ----------------------------------------------------------------------------- to hierarchical
STUB = round(2.54 * UNIT)
PITCH = round(2.54 * UNIT)


def _occupied(doc, sh):
    """Points that already connect something on a page (a new pin or stub must not land on them)."""
    pts = set()
    for _, a, b in doc.wires:
        pts |= {a, b}
    pts |= {lb["pt"] for lb in doc.labels}
    pts |= {pt for _, pt in doc.junctions}
    for sd in doc.sheets.values():
        pts |= {p["pt"] for p in sd["pins"]}
    for it, s, num, name, pt, _ in _pins(sh):
        pts.add(pt)
    return pts


def _clear(doc, occupied, a, b):
    """No existing point on the segment a-b, and no wire through any of its grid points."""
    n = max(abs(b[0] - a[0]), abs(b[1] - a[1])) // PITCH * 2 or 1
    for k in range(n + 1):
        p = (a[0] + (b[0] - a[0]) * k // n, a[1] + (b[1] - a[1]) * k // n)
        if p in occupied or doc.wires_through(p):
            return False
    return True


MM = UNIT


def _text_w(text, size=1.27):
    """Width of stroke-font text on the page, in file units (KiCad's default font, its own glyph widths)."""
    from .. import font
    return round(font.ink_width(text, size) * UNIT)


def _label_box(kind, text, pt, rot, size=1.27):
    """The page area a label's text (and flag) covers."""
    w = _text_w(text, size) + (round(3 * MM) if kind != "label" else round(0.5 * MM))
    x, y = pt
    r = int(round(rot)) % 360
    if kind == "label":                                   # text sits on the wire, reading away from the anchor
        return {0: (x, y - round(1.8 * MM), x + w, y), 180: (x - w, y - round(1.8 * MM), x, y),
                90: (x - round(1.8 * MM), y - w, x, y), 270: (x - round(1.8 * MM), y, x, y + w)}[r]
    h = round(1.1 * MM)
    return {0: (x, y - h, x + w, y + h), 180: (x - w, y - h, x, y + h), 90: (x - h, y - w, x + h, y), 270: (x - h, y, x + h, y + w)}[r]


def _overlaps(a, b, gap=round(0.4 * MM)):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _texts(doc):
    """Boxes of the page's free text and text boxes."""
    out = []
    for n in findall(doc.tree, "text"):
        x, y, r = _at(n)
        size = _font_size(n)
        lines = str(n[1]).split("\n")
        w = max(_text_w(l, size) for l in lines)
        h = round(len(lines) * size * 1.65 * UNIT)
        e = find(n, "effects")
        j = [str(v) for v in (find(e, "justify") or [])[1:]] if e else []
        if int(round(r)) % 180:
            w, h = h, w
        x, y = round(x * UNIT), round(y * UNIT)
        x0 = x if "left" in j else x - w if "right" in j else x - w // 2
        y0 = y if "top" in j else y - h if "bottom" in j else y - h // 2
        out.append((x0, y0, x0 + w, y0 + h))
    for n in findall(doc.tree, "text_box"):
        x, y, _ = _at(n)
        sz = find(n, "size")
        if sz:
            out.append((round(x * UNIT), round(y * UNIT), round((x + float(sz[1])) * UNIT), round((y + float(sz[2])) * UNIT)))
    return out


def _obstacles(doc, sh):
    """What a new label must not sit on: sheet symbols with their name and file fields, part symbols
    with their visible fields, labels, and free text."""
    out = []
    for cu, sd in doc.sheets.items():
        x, y = sd["at"]
        w, h = sd["size"]
        out.append(("sheet", cu, (x, y - round(2.6 * MM), x + w, y + h + round(2.6 * MM))))
        for p in sd["pins"]:
            px, py = p["pt"]
            tw = _text_w(p["name"])
            inner = (px + round(1.2 * MM), py - round(0.9 * MM), px + round(1.2 * MM) + tw, py + round(0.9 * MM)) if px == x \
                else (px - round(1.2 * MM) - tw, py - round(0.9 * MM), px - round(1.2 * MM), py + round(0.9 * MM))
            out.append(("pinname", cu, inner))
    for s in sh.symbols:
        b = s.bbox
        out.append(("symbol", s.ref, (round(b[0] * UNIT), round(b[1] * UNIT), round(b[2] * UNIT), round(b[3] * UNIT))))
        for k, (fx, fy, fr, shown) in s.field_at.items():
            v = s.fields.get(k, "")
            if shown and v and not s.is_power or (s.is_power and k == "Value" and shown):
                tw = _text_w(v)
                out.append(("field", s.ref, (round(fx * UNIT) - tw // 2, round((fy - 0.9) * UNIT), round(fx * UNIT) + tw // 2,
                                             round((fy + 0.9) * UNIT))))
    for lb in doc.labels:
        out.append(("label", lb["text"], _label_box(lb["kind"], lb["text"], lb["pt"], lb["rot"], lb["size"])))
    out += [("text", "", b) for b in _texts(doc)]
    return out


def _plan_hierarchical(d):
    rep = {"relabelled": 0, "pins_added": 0, "kept_supplies": [], "local": [], "grown": [], "renamed": {}, "crowded": []}
    D = d.D
    where = collections.defaultdict(set)                            # global name -> instances with a global label of it
    for sh in d.h.sheets:
        for lb in d.docs[sh.file].labels:
            if lb["kind"] == "global_label":
                where[lb["text"]].add(sh.path)
    todo = []
    for g in sorted(where):
        if g in d.power:
            rep["kept_supplies"].append(g)                           # a supply: KiCad joins it to the power symbols
            continue
        paths = where[g]
        chains = [d.chain(p) for p in paths]
        lca = None
        for level in zip(*chains):
            if len(set(level)) != 1:
                break
            lca = level[0]
        passing = {x for ch in chains for x in ch[ch.index(lca) + 1:]}
        todo.append((g, paths, lca, passing))
    # A file with several instances must be changed the same way for each of them.
    per_file = collections.defaultdict(set)
    for g, paths, lca, passing in todo:
        for sh in d.h.sheets:
            role = "pass" if sh.path in passing else "lca" if sh.path == lca else "has" if sh.path in paths else None
            if role:
                per_file[sh.file].add((g, role, sh.path in paths))
    for f, n in d.count.items():
        if n > 1:
            roles = collections.defaultdict(set)
            for sh in d.h.sheets:
                if sh.file == f:
                    for g, paths, lca, passing in todo:
                        roles[sh.path].add((g, "pass" if sh.path in passing else "lca" if sh.path == lca else None))
            if len({frozenset(v) for v in roles.values()}) > 1:
                raise Refused(f"{os.path.basename(f)} is used {n} times, and its copies connect differently; "
                              "draw that part by hand, or give each copy its own file first.")

    def names_in(path):
        doc = d.docs[d.inst[path].file]
        return {lb["text"]: D.find((path, "l", i)) for i, lb in enumerate(doc.labels)}

    def local_name(path, g, net):
        """The name a signal takes on one sheet: its own, unless a different net there already has it."""
        used = names_in(path)
        doc = d.docs[d.inst[path].file]
        pin_names = {}
        for cu, sd in doc.sheets.items():
            for j, pn in enumerate(sd["pins"]):
                pin_names.setdefault(pn["name"], set()).add(D.find((path, "s", cu, j)))
        for cand in [g, f"{g}_{_tag(d.inst[path].name or 'ROOT')}"] + [f"{g}_{k}" for k in range(2, 100)]:
            if used.get(cand, net) == net and pin_names.get(cand, {net}) == {net}:
                return cand
        raise Refused(f"no free name for {g} on sheet {d.inst[path].name}")

    stubs = collections.defaultdict(list)          # parent path -> [(child path, name on the child, name on the parent, shape, kind)]
    done_files = set()
    for g, paths, lca, passing in todo:
        net = D.find(("G", g))
        shape = "passive"
        for sh in d.h.sheets:
            for lb in d.docs[sh.file].labels:
                if lb["kind"] == "global_label" and lb["text"] == g:
                    shape = _shape(lb["shape"])
                    break
        name_on = {p: local_name(p, g, net) for p in passing | {lca}}
        for p in passing | {lca}:
            if name_on[p] != g:
                rep["renamed"][f"{g} on {d.inst[p].name or 'the root'}"] = name_on[p]
        if len(paths) == 1 and not passing:
            rep["local"].append(g)
        for p in sorted(passing | {lca}):
            sh = d.inst[p]
            if (sh.file, g) in done_files:
                continue
            done_files.add((sh.file, g))
            doc = d.docs[sh.file]
            kind = "hierarchical_label" if p in passing else "label"
            for i, lb in enumerate(doc.labels):
                if lb["kind"] == "global_label" and lb["text"] == g:
                    doc.edits.append((lb["node"], _label(kind, name_on[p], lb["pt"], lb["rot"], _shape(lb["shape"]), lb["size"])))
                    rep["relabelled"] += 1
        for p in sorted(passing):
            parent = d.inst[p].parent.path
            own = parent in paths or parent == lca
            stubs[parent].append((p, name_on[p], name_on[parent], shape, own))
    # Sheet pins with a short wire and a label on the parent page. Each pin faces the sheets (or parts)
    # it joins; a slot is taken only where the pin's name, the stub and the label land clear of every
    # wire, part, label and text on the page. The first stub on a page that only passes the signal up
    # carries its hierarchical label, the others a local label of the same name.
    for parent, items in sorted(stubs.items()):
        sh = d.inst[parent]
        doc = d.docs[sh.file]
        occupied = _occupied(doc, sh)
        obstacles = _obstacles(doc, sh)
        up_done = set()
        partners = collections.defaultdict(list)                 # name on the parent -> centres of what it joins
        for child, cname, pname, shape, own in items:
            sd = doc.sheets[child.rsplit("/", 1)[1]]
            partners[pname].append((child, sd["at"][0] + sd["size"][0] // 2))
        for lb in doc.labels:
            if lb["kind"] == "global_label" and lb["text"] in partners:
                partners[lb["text"]].append((None, lb["pt"][0]))
        used_at = {}                                              # child -> {(side, y)} taken on its symbol
        for child, *_ in items:
            sd = doc.sheets[child.rsplit("/", 1)[1]]
            x0, y0 = sd["at"]
            used_at[child] = {("left" if p["pt"][0] == x0 else "right" if p["pt"][0] == x0 + sd["size"][0] else "other", p["pt"][1])
                              for p in sd["pins"]}
        # A signal that joins just two neighbouring sheet symbols facing each other: one straight wire
        # between their pins, the way a person draws it, and no labels.
        wired = set()
        by_name = collections.defaultdict(list)
        for it in items:
            by_name[it[2]].append(it)
        for pname, its in sorted(by_name.items()):
            if len(its) != 2 or any(c is None for c, _ in partners[pname]) or len(partners[pname]) != 2:
                continue
            (ca, na, _, sa, _), (cb, nb, _, sb, _) = sorted(its, key=lambda t: doc.sheets[t[0].rsplit("/", 1)[1]]["at"][0])
            A, B = doc.sheets[ca.rsplit("/", 1)[1]], doc.sheets[cb.rsplit("/", 1)[1]]
            ax1, bx0 = A["at"][0] + A["size"][0], B["at"][0]
            lo = max(A["at"][1], B["at"][1]) + PITCH
            hi = min(A["at"][1] + A["size"][1], B["at"][1] + B["size"][1]) - PITCH // 2
            if bx0 - ax1 < 2 * STUB or lo > hi:
                continue
            for y in range(lo, hi + 1, PITCH):
                if ("right", y) in used_at[ca] or ("left", y) in used_at[cb]:
                    continue
                seg = (ax1, y - 1, bx0, y + 1)
                if not _clear(doc, occupied, (ax1, y), (bx0, y)) or any(_overlaps(seg, o[2], 0) for o in obstacles
                                                                         if o[0] not in ("sheet",) or o[1] not in (ca.rsplit("/", 1)[1], cb.rsplit("/", 1)[1])):
                    continue
                shape = sa if sa != "passive" else sb
                for child, cname, side, x in ((ca, na, "right", ax1), (cb, nb, "left", bx0)):
                    sd = doc.sheets[child.rsplit("/", 1)[1]]
                    doc.edits.append(((sd["node"].span[1] - 1, sd["node"].span[1] - 1),
                                      "\t" + _sheet_pin(cname, _shape(shape), (x, y), side) + "\n\t"))
                    used_at[child].add((side, y))
                    tw = _text_w(cname)
                    obstacles.append(("pinname", child.rsplit("/", 1)[1],
                                      (x - round(1.2 * MM) - tw, y - round(0.9 * MM), x - round(1.2 * MM), y + round(0.9 * MM)) if side == "right"
                                      else (x + round(1.2 * MM), y - round(0.9 * MM), x + round(1.2 * MM) + tw, y + round(0.9 * MM))))
                    rep["pins_added"] += 1
                doc.adds.append(_wire((ax1, y), (bx0, y)))
                occupied |= {(ax1, y), (bx0, y)}
                obstacles.append(("wire", pname, seg))
                wired |= {(ca, pname), (cb, pname)}
                rep["wired"] = rep.get("wired", 0) + 1
                break
        by_child = collections.defaultdict(list)
        for it in items:
            if (it[0], it[2]) not in wired:
                by_child[it[0]].append(it)
        for child, lst in sorted(by_child.items()):
            cu = child.rsplit("/", 1)[1]
            sd = doc.sheets[cu]
            x0, y0 = sd["at"]
            w, hgt = sd["size"]
            cx = x0 + w // 2
            used = used_at[child]
            new_h = hgt
            mine = lambda: (o for o in obstacles if not (o[0] in ("sheet", "pinname") and o[1] == cu))
            inner_texts = [o[2] for o in obstacles if o[0] in ("text", "symbol", "field", "label", "pinname")]

            def facing(pname, shape):
                xs = [x for c, x in partners[pname] if c != child]
                if xs:
                    return "right" if sum(xs) / len(xs) > cx else "left"
                return "left" if shape == "input" else "right"

            order = sorted(lst, key=lambda t: (facing(t[2], t[3]), t[1]))
            for _, cname, pname, shape, own in order:
                first = facing(pname, shape)
                up = (d.inst[parent].parent is not None) and not own and (parent, pname) not in up_done
                kind = "hierarchical_label" if up else "label"

                def fits(side, y, grow):
                    if (side, y) in used or (not grow and y > y0 + hgt - PITCH // 2):
                        return None
                    px = x0 if side == "left" else x0 + w
                    end = (px - STUB, y) if side == "left" else (px + STUB, y)
                    if not _clear(doc, occupied, (px, y), end):
                        return None
                    rot = 180 if side == "left" else 0
                    box = _label_box(kind, pname, end, rot)
                    tw = _text_w(cname)
                    name_box = (px + round(1.2 * MM), y - round(0.9 * MM), px + round(1.2 * MM) + tw, y + round(0.9 * MM)) if side == "left" \
                        else (px - round(1.2 * MM) - tw, y - round(0.9 * MM), px - round(1.2 * MM), y + round(0.9 * MM))
                    stub_box = (min(px, end[0]), y - 1, max(px, end[0]), y + 1)
                    if any(_overlaps(box, o[2]) or _overlaps(stub_box, o[2], 0) for o in mine()):
                        return None
                    if any(_overlaps(name_box, b) for b in inner_texts if x0 <= (b[0] + b[2]) // 2 <= x0 + w):
                        return None
                    return side, (px, y), end, rot, box, name_box

                spot = None
                for grow in (False, True):
                    for side in (first, "left" if first == "right" else "right"):
                        for k in range(1, 400 if grow else max(2, hgt // PITCH + 1)):
                            spot = fits(side, y0 + k * PITCH, grow)
                            if spot:
                                break
                        if spot or grow:
                            break
                    if spot:
                        break
                if not spot:                                        # nothing clear: the first free slot on the facing side
                    for k in range(1, 400):
                        y = y0 + k * PITCH
                        px = x0 if first == "left" else x0 + w
                        end = (px - STUB, y) if first == "left" else (px + STUB, y)
                        if (first, y) not in used and _clear(doc, occupied, (px, y), end):
                            rot = 180 if first == "left" else 0
                            spot = (first, (px, y), end, rot, _label_box(kind, pname, end, rot), None)
                            rep["crowded"].append(f"{cname} on {d.inst[child].name}")
                            break
                side, pt, end, rot, box, name_box = spot
                used.add((side, pt[1]))
                occupied |= {pt, end}
                obstacles.append(("label", pname, box))
                if name_box:
                    inner_texts.append(name_box)
                    obstacles.append(("pinname", cu, name_box))
                new_h = max(new_h, pt[1] - y0 + PITCH)
                doc.edits.append(((sd["node"].span[1] - 1, sd["node"].span[1] - 1), "\t" + _sheet_pin(cname, shape, pt, side) + "\n\t"))
                doc.adds.append(_wire(pt, end))
                if up:
                    up_done.add((parent, pname))
                doc.adds.append(_label(kind, pname, end, rot, shape if up else "passive"))
                rep["pins_added"] += 1
            if new_h != hgt:
                doc.edits.append((find(sd["node"], "size"), f"(size {_mm(w)} {_mm(new_h)})"))
                rep["grown"].append(d.inst[child].name)
                obstacles = [o if not (o[0] == "sheet" and o[1] == cu) else ("sheet", cu, (x0, y0 - round(2.6 * MM), x0 + w,
                                                                                            y0 + new_h + round(2.6 * MM)))
                             for o in obstacles]
    return rep


# ----------------------------------------------------------------------------- convert and prove
def _copy_tree(d, dest):
    """The hierarchy's files (and the KiCad project) under dest, same layout; returns the new root path."""
    base = os.path.dirname(d.sch)
    for f in d.h.files:
        rel = os.path.relpath(f, base)
        if rel.startswith(".."):
            raise Refused(f"{os.path.basename(f)} lives outside the schematic's folder; convert it by hand")
        os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
        shutil.copyfile(f, os.path.join(dest, rel))
    pro = os.path.splitext(d.sch)[0] + ".kicad_pro"
    if os.path.exists(pro):
        shutil.copyfile(pro, os.path.join(dest, os.path.basename(pro)))
    return os.path.join(dest, os.path.basename(d.sch))


def _diff(before, after, limit=6):
    """Pins that moved to a different net, in words."""
    where_b = {pin: g for g in before for pin in g}
    where_a = {pin: g for g in after for pin in g}
    out = []
    for pin in sorted(set(where_b) | set(where_a)):
        b, a = where_b.get(pin, frozenset()), where_a.get(pin, frozenset())
        if b != a:
            gained = sorted(a - b)[:3]
            lost = sorted(b - a)[:3]
            txt = f"{pin[0]} pin {pin[1]}"
            if gained:
                txt += " now joins " + ", ".join(f"{r}.{p}" for r, p in gained)
            if lost:
                txt += (" and" if gained else "") + " no longer reaches " + ", ".join(f"{r}.{p}" for r, p in lost)
            out.append(txt)
            if len(out) >= limit:
                break
    return out


# Notes that explain how the sheets connect go stale when the style changes.
STALE = re.compile(r"sheet[- ]pins?|hierarchical|global (labels?|nets?)|wired sheet to sheet|off-?page|same name are", re.I)


def convert(sch, target, write=True, workdir=None):
    """Redraw the schematic in `target` style ('flat' or 'hierarchical'). Returns a report:
    {"ok", "style" (before), "target", "changed" (files), "proof", "report", "message"}; ok is False
    (and nothing written) when the conversion is refused or the proof fails."""
    if target not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}")
    d = Design(sch)
    desc = _describe(d)
    res = {"ok": False, "style": desc["style"], "target": target, "changed": [], "proof": None, "report": {}}
    if desc["style"] in ("single", "unlinked") or desc["style"] == target:
        res.update(ok=True, message={"single": "One sheet: nothing joins sheets, so the style does not apply.",
                                     "unlinked": "The sheets share only supplies: nothing to redraw."}.get(desc["style"],
                                     f"Already {target}."))
        return res
    if desc["buses"]:
        res["message"] = "The schematic uses buses; redraw those by hand (the conversion handles single wires only)."
        return res
    if target == "flat":
        try:
            _refuse_reuse(d)
        except Refused as e:
            res["message"] = str(e)
            return res
    tmp = workdir or tempfile.mkdtemp(prefix="tw-style-")
    try:
        nl0 = _export(d.sch, tmp)
        before = _netlist_partition(nl0)
        if d.partition() != set(before):
            res["message"] = ("Tracewright's reading of the sheets does not match KiCad's netlist, so it will not redraw "
                              "them: " + "; ".join(_diff(set(before), d.partition(), 3)))
            return res
        try:
            rep = _plan_flat(d, nl0) if target == "flat" else _plan_hierarchical(d)
        except Refused as e:
            res["message"] = str(e)
            return res
        new = {f: doc.result() for f, doc in d.docs.items()}
        new = {f: t for f, t in new.items() if t is not None}
        work = os.path.join(tmp, "converted")
        root = _copy_tree(d, work)
        base = os.path.dirname(d.sch)
        for f, t in new.items():
            with open(os.path.join(work, os.path.relpath(f, base)), "w", encoding="utf-8") as fh:
                fh.write(t)
        nl1 = _export(root, os.path.join(tmp, "converted"))
        after = _netlist_partition(nl1)
        parts0 = {r: (p["value"], p["footprint"]) for r, p in nl0.parts.items()}
        parts1 = {r: (p["value"], p["footprint"]) for r, p in nl1.parts.items()}
        same = set(before) == set(after) and parts0 == parts1
        res["proof"] = {"nets": len(before), "pins": sum(len(g) for g in before), "parts": len(parts0), "same": same,
                        "differences": [] if same else (_diff(set(before), set(after)) or ["the parts list changed"])}
        if not same:
            res["message"] = "The redrawn sheets would change the connections, so nothing was written: " + \
                "; ".join(res["proof"]["differences"][:3])
            return res
        after_desc = _describe(Design(root))
        stale = []
        for f, doc in d.docs.items():
            for n in findall(doc.tree, "text") + findall(doc.tree, "text_box"):
                for line in str(n[1]).splitlines():
                    if STALE.search(line):
                        stale.append(line.strip()[:120])
        if stale:
            rep["stale_notes"] = stale[:8]
        res["report"] = rep
        res["after"] = after_desc["style"]
        if write:
            for f, t in new.items():
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(t)
        res["changed"] = sorted(os.path.relpath(f, base) for f in new)
        res["ok"] = True
        res["message"] = (f"Redrawn {target}: {len(new)} sheet file{'s' if len(new) != 1 else ''} changed; KiCad's netlist "
                          f"is the same ({len(before)} nets, {res['proof']['pins']} pins).")
        return res
    finally:
        if not workdir:
            shutil.rmtree(tmp, ignore_errors=True)
