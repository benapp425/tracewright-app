"""Route a board with the grid router: net classes -> profiles, pads -> islands, rip-up and reroute.

    ./tw route                      every unrouted connection
    ./tw route --nets SDA SCL       just these nets
    ./tw route --clear              tear up the selected nets' copper first

Order, the way a person routes: escape vias from pads to the planes (nets with a pour on another
layer), then supplies (wide classes), then signals short-first. Multi-pin nets grow as a tree from
the island with the most pads, joining the nearest island each time. When a connection fails, the
nets in the way are found (route over them at a penalty), ripped up, and routed again after it.

The routing layers are F.Cu and B.Cu (inner layers taken to be planes), or the signal layers of the project's
stack-up plan (tracewright.json "stackup", see tw/stackup.py), each in its direction. `on_progress` is called after
every net so the caller can show routing as it happens.
"""
import json, math, time, collections
import numpy as np
from .. import env, geom
from ..board import Board
from ..pro import ProjectSettings
from . import router as R

VIA_COST = 1000.0          # a via costs as much as 10 mm of track: change layer to reach a pin or cross a bus
CHEAP_VIA = 500.0
PF_PRES = 4.0              # negotiation: a cell another net holds, at first (a free cell costs 10 a step)
PF_GROW = 1.5              # ... and each round dearer by this
PF_HIST = 3.0              # a cell contested in a round, dearer from then on
V2_FCU_CROSS = 1.10        # F.Cu along y (B.Cu along x already); off by default: longer routes on SMD boards
PAIR_K = 25.0              # v2: cost a cell for a pair's second half away from its partner's side
REFINE_MIN_S = 20.0        # v2: the second look's time budget is the routing time, at least this
HIST_AMOUNT = 40.0         # PathFinder history added to contested cells at each rip-up
SKETCH_K = 30.0            # cost a cell away from the user's sketch of where a track should run (route ... along)
SKETCH_W = 0.8             # mm either side of the sketch that is free


def dump_for_router(b, routing=R.LAYERS):
    """The router's input (board mm) from a parsed board; routing: the layers tracks may go on."""
    d = {"outline": [], "pads": [], "rules": [], "tracks": [], "vias": [], "footprints": []}
    if b.outline:
        d["outline"] = [{"outline": b.outline[0], "holes": []}]
        d["cutouts"] = b.outline[1:]
    for p in b.pads():
        layers = [l for l in p.layers if l.endswith(".Cu")]
        shp = {l: [{"outline": pl} for pl in p.polys] for l in layers if l in routing}
        d["pads"].append({"ref": p.ref, "num": p.num, "net": p.net, "pos": (p.x, p.y), "layers": layers, "shape": shp,
                          "drill": min(p.drill_w, p.drill_h) if p.drill else 0.0, "size": (p.w, p.h),
                          "orient": p.angle, "npth": p.kind == "np_thru_hole"})
    for z in b.zones:
        if not z.is_rule_area:
            continue
        ko = z.keepout or {}
        layers = []
        for l in z.layers:
            layers += list(routing) if l == "*.Cu" else ["F.Cu", "B.Cu"] if l == "F&B.Cu" else [l]
        d["rules"].append({"owner": z.owner or "", "name": z.name, "rule": True, "layers": layers,
                           "no_tracks": bool(ko.get("tracks")), "no_vias": bool(ko.get("vias")),
                           "no_pour": bool(ko.get("copperpour")), "poly": [{"outline": pl} for pl in z.outline]})
    for t in b.tracks:
        pts = geom.arc_points(t.a, t.mid, t.b) if t.mid else [t.a, t.b]
        for a, c in zip(pts, pts[1:]):
            d["tracks"].append({"a": a, "b": c, "w": t.w, "layer": t.layer, "net": t.net})
    for v in b.vias:
        d["vias"].append({"pos": (v.x, v.y), "d": v.d, "drill": v.drill, "net": v.net,
                          "layers": v.span(b.copper), "kind": v.kind or "through"})
    return d


NECK_W, NECK_CL = 0.15, 0.15      # escapes from fine-pitch pads (JLC: 0.1 / 0.1 minimum)


def neck_values(b):
    """(width, clearance) of tracks between fine-pitch pads: 0.1 mm under a ball grid of 0.8 mm or finer on four or more
    layers (one track between the balls; the fabs make 0.09 mm there), else 0.15 mm."""
    from .. import escape
    if len(b.copper) >= 4 and any((g := escape.grid(fp)) and g["pitch"] <= 0.8 for fp in b.fp_list):
        return 0.1, 0.1
    return NECK_W, NECK_CL


def neck_board_rules(project, neck_w, neck_cl, via=None):
    """The board's minimum track and clearance no higher than the neck-down (else KiCad's DRC flags every escape), and
    its minimum via no larger than the fan-out via used in the neck areas (via: (d, drill), the fab's smallest)."""
    if not project.pro:
        return {}
    from ..pro import set_board_rules
    vals = {"min_track_width": neck_w, "min_clearance": neck_cl} if neck_w < 0.127 else {}
    if via:
        vals.update(min_via_diameter=via[0], min_through_hole_diameter=via[1])
    return set_board_rules(project.pro, vals, lower_only=True) if vals else {}


def profiles_for(pro, net_class, nets, necks=False, neck_w=NECK_W, neck_cl=NECK_CL, neck_via=None):
    """Routing profiles per net class; with neck areas, each class's neck-down profile too (neck_via: (d, drill), the
    fan-out via used under fine-pitch parts, when smaller than the class's)."""
    used = {net_class.get(n, "Default") for n in nets} | {"Default"}
    profs, clear = {}, {}
    for name in sorted(used):
        c = pro.cls(name)
        w, cl = float(c["track_width"]), float(c["clearance"])
        neck = None
        if necks:                     # every class, its own values the neck's already or not: inside a neck area DRC
            neck = name + "~neck"     # holds everything to the neck clearance, whatever the other net's class says
            vd, vh = float(c["via_diameter"]), float(c["via_drill"])
            if neck_via:
                vd, vh = min(vd, neck_via[0]), min(vh, neck_via[1])
            profs[neck] = R.Profile(neck, min(w, neck_w), min(cl, neck_cl), vd, vh, fixed_cl=True)
        profs[name] = R.Profile(name, w, cl, float(c["via_diameter"]), float(c["via_drill"]), neck=neck)
        clear[name] = cl
    return profs, clear


def fine_pitch_areas(b, pitch=0.8, margin=1.5, routing=R.LAYERS):
    """[(ref, (x0, y0, x1, y1))] around footprints whose pads of different nets sit closer than `pitch`."""
    out = []
    for fp in b.fp_list:
        pads = [p for p in fp.pads if p.net and any(l in routing for l in p.layers)]
        if len(pads) < 3:
            continue
        close = False
        for i, a in enumerate(pads):
            for c in pads[i + 1:]:
                if a.net != c.net and geom.dist((a.x, a.y), (c.x, c.y)) < pitch:
                    close = True
                    break
            if close:
                break
        if close:
            pts = [q for p in pads for q in p.poly]
            x0, y0, x1, y1 = geom.bbox(pts)
            out.append((fp.ref, (x0 - margin, y0 - margin, x1 + margin, y1 + margin)))
    return out


def plane_nets(b):
    """{net: set(layers)} for nets poured over a real share (>= 20 %) of the board."""
    if not b.outline:
        return {}
    area = abs(geom.area(b.outline[0])) or 1.0
    out = collections.defaultdict(set)
    for z in b.zones:
        if z.is_rule_area or not z.net or not z.outline:
            continue
        if abs(geom.area(z.outline[0])) >= 0.2 * area:
            for l in z.layers:
                out[z.net].add(l)
    return dict(out)


def stackup_dirs(plan):
    """The router's layer directions from a stack-up plan (None: F.Cu and B.Cu as they always were)."""
    if not plan:
        return None
    from .. import stackup
    return stackup.directions(plan)


class Job:
    def __init__(self, net, prof, pads, key):
        self.net, self.prof, self.pads, self.key = net, prof, pads, key
        self.fcu = 1.0


def islands(B, Rt, net, pads, existing, ports=None):
    """Groups of pads already joined by copper of the net: [(cells, points, n_pads)]. ports: the net's breakout escapes
    ([{"ref", "pad", "layer", "at"}], tw.breakout): an island holding a broken-out pad is taken up at the end of its
    escape only (the escape was laid for it; starting from its via on another layer would leave it hanging)."""
    out = []
    for members in connected(net, pads, existing):
        pads_in = [x for k, x in members if k == "pad"]
        if not pads_in:
            continue
        cells, pts = [], []
        for p in pads_in:
            cells += R.pad_cells(B, p, Rt._prof_of.get(net, "Default"))
            pts.append(tuple(p["pos"]))
        segs_in = [(x["layer"], x["a"], x["b"], x["w"]) for k, x in members if k == "seg" and x["layer"] in B.layers]
        if segs_in:
            cells += Rt.cells_of(segs_in)
            pts += [s[1] for s in segs_in]
        for k, v in members:                          # a via's landing on each routing layer it reaches (fan-outs)
            if k != "via":
                continue
            j, i = B.cell(*v["pos"])
            cells += [(li, j * B.nx + i) for li, l in enumerate(B.layers) if v.get("layers") is None or l in v["layers"]]
            pts.append(tuple(v["pos"]))
        mine = [e for e in (ports or ()) if e.get("layer") in B.layers and e.get("segments")
                and any(p["ref"] == e["ref"] and p["num"] == e["pad"] for p in pads_in)]
        if mine and len(pads_in) == 1:                # the ball alone: its way out is the escape's end
            cells, pts = [], []
            for e in mine:
                li = B.layers.index(e["layer"])
                j0, i0 = B.cell(*e["at"])
                cells.append((li, j0 * B.nx + i0))
                pts.append(tuple(e["at"]))
        out.append((cells, pts, len(pads_in)))
    return out


def connected(net, pads, existing):
    """The net's pads, track pieces and vias in groups joined by copper: [[("pad" | "seg" | "via", item), ...]]."""
    segs = [t for t in existing["tracks"] if t["net"] == net]
    vias = [v for v in existing["vias"] if v["net"] == net]
    # a union-find over pads, track pieces and vias that touch
    nodes = [("pad", p) for p in pads] + [("seg", s) for s in segs] + [("via", v) for v in vias]
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def touch(a, b):
        ka, x = a
        kb, y = b
        if ka == "pad" and kb == "seg":
            return y["layer"] in x["layers"] and any(geom.inside(e, pl["outline"]) for pl in x["shape"].get(y["layer"], [])
                                                     for e in (y["a"], y["b"]))
        if ka == "seg" and kb == "pad":
            return touch(b, a)
        if ka == "seg" and kb == "seg":
            return x["layer"] == y["layer"] and min(geom.dist(p, q) for p in (x["a"], x["b"]) for q in (y["a"], y["b"])) < 0.02 or \
                (x["layer"] == y["layer"] and (geom.seg_point_dist(y["a"], x["a"], x["b"]) < 0.02 or
                                               geom.seg_point_dist(y["b"], x["a"], x["b"]) < 0.02))
        if "via" in (ka, kb):
            v = x if ka == "via" else y
            o, ko = (y, kb) if ka == "via" else (x, ka)
            span = v.get("layers")                     # None: a through via (every layer)
            if ko == "seg":
                return (span is None or o["layer"] in span) and any(geom.dist(e, v["pos"]) <= v["d"] / 2 for e in (o["a"], o["b"]))
            if ko == "pad":
                return any(geom.inside(v["pos"], pl["outline"]) for l in o["shape"] if span is None or l in span for pl in o["shape"][l])
            other = o.get("layers")
            return geom.dist(o["pos"], v["pos"]) < 0.01 and (span is None or other is None or bool(set(span) & set(other)))
        return False
    n = len(nodes)
    if segs or vias:
        for i in range(n):
            for j in range(i + 1, n):
                if touch(nodes[i], nodes[j]):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
    groups = collections.defaultdict(list)
    for i in range(n):
        groups[find(i)].append(nodes[i])
    return list(groups.values())


class GridRoute:
    def __init__(self, project=None, nets=None, clear=False, on_progress=None, via_cost=VIA_COST, log=print, v2=None,
                 layer_dirs=None, cleanup=None, sketch=None):
        self.p = project or env.project()
        self.v2 = bool(v2)
        self.layer_dirs = False if layer_dirs is None else layer_dirs      # measured: longer routes on SMD boards
        self.do_cleanup = self.v2 if cleanup is None else cleanup
        self.couple = self.v2
        self.b = Board.load(self.p.pcb)
        self.pro = ProjectSettings.load(self.p.pro) if self.p.pro else ProjectSettings({})
        self.only = set(nets) if nets else None
        self.clear = clear
        self.on_progress = on_progress or (lambda ev: None)
        self.log = log
        self.via_cost = via_cost
        self.failed = {}
        self.hist_amount = HIST_AMOUNT
        self.sketch = sketch or None                  # [{"p": [[x, y], ...], "layer"?}]: route the nets along it
        from .. import stackup
        self.plan = stackup.get(getattr(self.p, "cfg", None))
        if self.plan and len(self.b.copper) != self.plan["layers"]:
            self.plan = None                          # not applied to the board yet: as without one
        self.routing = tuple(stackup.routing_layers(self.plan, self.b.copper))
        from .. import routeplan                       # the routing plan: presets, and which nets are left for a person
        self.preset = routeplan.preset(self.p)
        pre = routeplan.PRESETS[self.preset]
        if via_cost == VIA_COST:
            self.via_cost = pre["via"]
        self.bends = (pre["bend45"], pre["bend90"])
        try:
            self.modes = routeplan.classify(self.p)
        except Exception:
            self.modes = {}
        self.left_for_hand = []
        self.off_plan = []

    def _plan_layers(self, net, pads):
        """(layers, escapes) from the layer plan: the routing layers the net keeps to, and round each of its pads on
        another layer a 1.5 mm patch of that layer (the pad's way to its via). (None, None) without a plan."""
        m = self.modes.get(net) or self.modes.get(net.rsplit("/", 1)[-1]) or {}
        want = [l for l in (m.get("layers") or []) if l in self.routing]
        if not want or len(want) == len(self.routing):
            return None, None
        B = self.B
        esc, r = [], int(round(1.5 / R.RES))
        for p in pads:
            for l in p["layers"]:
                if l not in B.layers or l in want:
                    continue
                j0, i0 = B.cell(*p["pos"])
                jj, ii = np.mgrid[max(0, j0 - r):min(B.ny, j0 + r + 1), max(0, i0 - r):min(B.nx, i0 + r + 1)]
                inside = (jj - j0) ** 2 + (ii - i0) ** 2 <= r * r
                esc.append((B.layers.index(l), (jj[inside] * B.nx + ii[inside]).ravel()))
        return want, esc

    def _mode(self, net):
        m = self.modes.get(net) or self.modes.get(net.rsplit("/", 1)[-1]) or {}
        return m.get("mode", "auto"), m.get("order", 3)

    def _net_ok(self, n):
        if not n or n.startswith("unconnected-"):
            return False
        if self.only is None:
            return True
        return n in self.only or n.rsplit("/", 1)[-1] in self.only

    def setup(self):
        b = self.b
        if not b.outline:
            raise ValueError("the board has no closed outline; draw Edge.Cuts first")
        dump = dump_for_router(b, self.routing)
        if self.clear:                   # what ops() takes off the board is no obstacle (junk copper included)
            gone = lambda n: self._net_ok(n) or (self.only is None and (not n or n.startswith("unconnected-")))
            dump["tracks"] = [t for t in dump["tracks"] if not gone(t["net"])]
            dump["vias"] = [v for v in dump["vias"] if not gone(v["net"])]
        self.dump = dump
        nl_class = {}
        try:
            from ..netlist import Netlist
            import os
            f = os.path.join(self.p.build, f"{self.p.stem}.net")
            if os.path.exists(f):
                nl_class = Netlist.load(f).net_class
        except Exception:
            pass
        nets = sorted(b.nets)
        self.net_class = {n: self.pro.class_of(n, nl_class.get(n)) for n in nets}
        self.necks = fine_pitch_areas(b, routing=self.routing)
        self.neck_w, self.neck_cl = neck_values(b)
        from .. import regions as regmod                 # the user's regions: finer tracks, more spacing
        try:
            self.regions = regmod.for_router(self.p, b)
        except Exception:
            self.regions = []
        fine = [box for _, box, kind, _, _ in self.regions if kind == "neck"]
        for _, _, kind, tw_, cl_ in self.regions:
            if kind == "neck":
                self.neck_w, self.neck_cl = min(self.neck_w, tw_), min(self.neck_cl, cl_)
        try:                                             # under fine-pitch parts: the fab's smallest via, as the fan-out uses
            from .. import escape as _esc
            _r = _esc._rules(self.p, b)
            neck_via = (_r["via_d"], _r["via_drill"])
        except Exception:
            neck_via = None
        self.neck_via = neck_via
        profs, clear = profiles_for(self.pro, self.net_class, nets, necks=bool(self.necks or fine), neck_w=self.neck_w,
                                    neck_cl=self.neck_cl, neck_via=neck_via)
        B = R.Board(dump, profs, clear, self.net_class, necks=[r for _, r in self.necks] + fine, layers=self.routing)
        try:                                             # the board's hole-to-hole and hole clearance minimums (before any
            _r = ((json.load(open(self.p.pro)).get("board") or {}).get("design_settings") or {}).get("rules", {}) \
                if self.p.pro else {}                     # copper is stamped: a via's hole keeps them from all of it)
        except Exception:
            _r = {}
        B.h2h = float(_r.get("min_hole_to_hole", 0.25))
        B.hole_cl = float(_r.get("min_hole_clearance", 0.25))
        B.stamp_pads()
        for _, box, kind, _, cl_ in self.regions:     # more spacing: pads inside keep the region's clearance round them
            if kind != "spacing":
                continue
            for p in dump["pads"]:
                if not (box[0] <= p["pos"][0] <= box[2] and box[1] <= p["pos"][1] <= box[3]):
                    continue
                cls = self.net_class.get(p["net"], "Default")
                extra = cl_ - clear.get(cls, 0.2)
                if extra > 0:
                    for lname, pl in p["shape"].items():
                        for poly in pl:
                            B.stamp(p["net"] or f"__nc_{p['ref']}_{p['num']}", cls, [lname], "poly", poly["outline"], extra=extra)
        B.stamp_rules()
        for hole in dump.get("cutouts", []):
            B.stamp("", "Default", list(B.layers), "poly", hole, extra=0.3, block_all_vias=True)
        for p in dump["pads"]:
            if p["npth"] and p["drill"] > 0:          # nothing crosses a non-plated hole
                B.stamp("", "Default", list(B.layers), "circle", (p["pos"][0], p["pos"][1], p["drill"] / 2), extra=0.2,
                        block_all_vias=True)
        for t in dump["tracks"]:
            if t["layer"] in B.layers:
                B.stamp_track(t["net"], t["layer"], t["a"], t["b"], t["w"])
        for v in dump["vias"]:
            B.stamp_via(v["net"], v["pos"], v["d"], v.get("layers") if v.get("kind", "through") != "through" else None,
                        drill=v.get("drill"))
        B.snapshot()
        Rt = R.Router(B, bend45=self.bends[0], bend90=self.bends[1], via=self.via_cost, directions=stackup_dirs(self.plan))
        if self.layer_dirs:                           # layer directions: F.Cu runs along y, B.Cu along x
            Rt.fcu_cross = V2_FCU_CROSS
        Rt._prof_of = {n: self.net_class.get(n, "Default") for n in nets}
        self.B, self.Rt = B, Rt
        self.planes = plane_nets(b)
        from .. import breakout                         # the dense parts' escapes: nets are taken up at their ports
        self.ports = breakout.ports(self.p) if not self.clear else {}
        self.fields = [box for _, box in breakout.fields(self.p)] if self.ports else []
        return self

    # ------------------------------------------------------------------ jobs
    def jobs(self):
        self.left_for_hand = []                            # as of this pass
        pads_by = collections.defaultdict(list)
        for p in self.dump["pads"]:
            if p["net"] and self._net_ok(p["net"]):
                pads_by[p["net"]].append(p)
        out = []
        for net, pads in pads_by.items():
            if len(pads) < 2 or net in self.planes:
                continue
            mode, order = self._mode(net)
            named = self.only is not None and (net in self.only or net.rsplit("/", 1)[-1] in self.only)
            if mode == "hand" and not named:               # left for a person (or Claude in the editor): named, it routes
                if len(islands(self.B, self.Rt, net, pads, {"tracks": self.dump["tracks"], "vias": self.dump["vias"]})) > 1:
                    self.left_for_hand.append(net)         # still open (one already routed by hand is not news)
                continue
            prof = self.net_class.get(net, "Default")
            xs = [q["pos"][0] for q in pads]
            ys = [q["pos"][1] for q in pads]
            span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            w = self.B.profiles[prof].w
            power = w >= 0.39 or any(k in prof.upper() for k in ("PWR", "POWER", "SUPPLY"))
            job = Job(net, prof, pads, (0 if mode == "guided" else 1, order, 0 if power else 1, span))    # guided first
            job.layers, job.escape = self._plan_layers(net, pads)
            out.append(job)
        out.sort(key=lambda j: j.key)
        return out

    # ------------------------------------------------------------------ plane escapes
    def escapes(self, radius=1.6):
        """SMD pads of plane nets on a layer without that pour: a short stub to a via."""
        B, Rt = self.B, self.Rt
        made = 0
        down = set()                                  # pads whose copper already reaches a via of their net (a fan-out)
        for net in self.planes:
            pads = [p for p in self.dump["pads"] if p["net"] == net]
            for g in connected(net, pads, self.dump) if pads else ():
                if any(k == "via" for k, _ in g):
                    down |= {(x["ref"], x["num"]) for k, x in g if k == "pad"}
        for p in self.dump["pads"]:
            net = p["net"]
            if net not in self.planes or not self._net_ok(net) or p["drill"] > 0:
                continue
            if (p["ref"], p["num"]) in down:
                continue
            pour_layers = self.planes[net]
            layer = next((l for l in p["layers"] if l in B.layers), None)
            if layer is None or layer in pour_layers and len(pour_layers) == 1:
                continue
            if layer in pour_layers:
                continue                              # the pour on this layer reaches the pad
            if self._escape(p, layer, radius):
                made += 1
        return made

    def _escape(self, p, layer, radius):
        B, Rt = self.B, self.Rt
        net = p["net"]
        prof = self.net_class.get(net, "Default")
        pr = B.profiles[prof]
        lt, lv = B.legal(net, prof)
        li = B.layers.index(layer)
        j0, i0 = B.cell(*p["pos"])
        r = int(radius / R.RES)
        cand = []
        for dj in range(-r, r + 1):
            for di in range(-r, r + 1):
                j, i = j0 + dj, i0 + di
                if 0 <= j < B.ny and 0 <= i < B.nx and lv[j * B.nx + i]:
                    cand.append((math.hypot(dj, di), j, i))
        src = [c for c in R.pad_cells(B, p, prof) if c[0] == li]
        w_ = B.profiles[pr.neck].w if pr.neck and B.neck_mask[j0, i0] else pr.w     # in a neck area: the width it was checked at
        near = sorted((geom.dist(v["pos"], p["pos"]), v["pos"]) for v in self.dump["vias"]
                      if v["net"] == net and v.get("kind", "through") == "through" and geom.dist(v["pos"], p["pos"]) <= radius)
        for _, pos in near:                           # a via of its own net already there (a BGA's, under a capacitor)
            jv, iv = B.cell(*pos)
            for l, idx in src[:40]:
                dg = Rt._dogleg(lt[li], divmod(idx, B.nx), (jv, iv))
                if dg:
                    pts = [B.xy(*c) for c in dg]
                    segs = [(layer, pa, pb, w_) for pa, pb in zip(pts, pts[1:]) if pa != pb]
                    rec = Rt.add(net, prof, segs, [])
                    self._emit("escape", net, rec)
                    return True
        for d, j, i in sorted(cand)[:400]:
            for l, idx in src[:40]:
                a = divmod(idx, B.nx)
                dg = Rt._dogleg(lt[li], a, (j, i))
                if dg:
                    pts = [B.xy(*c) for c in dg]
                    segs = [(layer, pa, pb, w_) for pa, pb in zip(pts, pts[1:]) if pa != pb]
                    vp = B.profiles[pr.neck] if pr.neck and B.neck_mask[j, i] else pr      # a neck area's via there
                    rec = Rt.add(net, prof, segs, [(pts[-1], vp.via_d, vp.via_drill)])
                    self._emit("escape", net, rec)
                    return True
        return False

    def stitch(self, pitch=None, reach=1.2):
        """Vias joining a net's pours on two layers, on a `pitch` grid (each where it is legal)."""
        B, Rt, b = self.B, self.Rt, self.b
        if pitch is None:                              # ~ 5 x 4 vias on a small board, 8 mm pitch on big ones
            w, h = b.size()
            pitch = max(5.0, min(8.0, min(w, h) / 4.0))
        made = 0
        for net, layers in self.planes.items():
            if not self._net_ok(net):
                continue
            cu = [l for l in layers if l.endswith(".Cu")]
            if len(cu) < 2:                            # poured on one layer: nothing to join
                continue
            polys = [pl for z in b.zones if z.net == net and not z.is_rule_area for pl in z.outline]
            if not polys:
                continue
            prof = self.net_class.get(net, "Default")
            pr = B.profiles[prof]
            lt, lv = B.legal(net, prof)
            x0, y0, x1, y1 = b.bbox()
            r = int(reach / R.RES)
            # already stitched there (a second run adds nothing); the router's own copy, so vias being cleared do not count
            have = [tuple(v["pos"]) for v in self.dump.get("vias", []) if v.get("net") == net]
            gy = y0 + pitch / 2
            while gy < y1:
                gx = x0 + pitch / 2
                while gx < x1:
                    if any(geom.dist((gx, gy), q) < pitch / 2 for q in have):
                        gx += pitch
                        continue
                    if any(geom.inside((gx, gy), pl) for pl in polys):
                        j0, i0 = B.cell(gx, gy)
                        best = None
                        for dj in range(-r, r + 1):
                            for di in range(-r, r + 1):
                                j, i = j0 + dj, i0 + di
                                if 0 <= j < B.ny and 0 <= i < B.nx and lv[j * B.nx + i] and \
                                        all(lt[k][j * B.nx + i] for k in range(len(B.layers))):
                                    d = dj * dj + di * di
                                    if best is None or d < best[0]:
                                        best = (d, j, i)
                        if best:
                            pos = B.xy(best[1], best[2])
                            if any(geom.inside(pos, pl) for pl in polys):
                                vp = B.profiles[pr.neck] if pr.neck and B.neck_mask[best[1], best[2]] else pr
                                rec = Rt.add(net, prof, [], [(pos, vp.via_d, vp.via_drill)])
                                self._emit("stitch", net, rec)
                                made += 1
                                lt, lv = B.legal(net, prof)
                    gx += pitch
                gy += pitch
        return made

    # ------------------------------------------------------------------ routing
    def route_job(self, job, keep_going=False, allow_vias=True, cell_cost=None, layers=None, keep_to=None):
        B, Rt = self.B, self.Rt
        escape = None
        if layers is None and getattr(job, "layers", None):     # the layer plan, with its pads' escapes
            layers, escape = job.layers, job.escape
        isl = islands(B, Rt, job.net, job.pads, {"tracks": [t for t in self.dump["tracks"]],
                                                  "vias": self.dump["vias"]}, ports=self.ports.get(job.net))
        if len(isl) < 2:
            return None
        order = sorted(isl, key=lambda g: (g[2], len(g[0])), reverse=True)
        tree, pts = list(order[0][0]), list(order[0][1])
        rest = order[1:]
        first_fail = None
        while rest:
            nxt = min(rest, key=lambda g: min(geom.dist(a, q) for a in g[1] for q in pts))
            rest.remove(nxt)
            rec = Rt.connect(job.net, job.prof, tree, nxt[0], fcu_factor=job.fcu, allow_vias=allow_vias, cell_cost=cell_cost,
                             layers=layers, keep_to=keep_to, escape=escape)
            if rec is None:
                fail = (list(tree), nxt[0], f"({nxt[1][0][0]:.1f}, {nxt[1][0][1]:.1f})")
                if not keep_going:
                    return fail
                first_fail = first_fail or fail
                continue
            tree += Rt.cells_of(rec["segments"]) + nxt[0]
            pts += [a for _, a, _, _ in rec["segments"]] + nxt[1]
        return first_fail

    def _emit(self, status, net, rec=None, **kw):
        ev = {"status": status, "net": net}
        if rec:
            ev["tracks"] = [{"net": net, "layer": l, "a": list(a), "b": list(b), "w": w} for l, a, b, w in rec["segments"]]
            ev["vias"] = [{"net": net, "x": p[0], "y": p[1], "d": d, "drill": dr} for p, d, dr in rec["vias"]]
        ev.update(kw)
        try:
            self.on_progress(ev)
        except Exception:
            pass

    def run(self, max_rips=6):
        t0 = self.t_start = time.time()
        if getattr(self, "budget_s", None) is None:
            self.budget_s = float(((getattr(self.p, "cfg", None) or {}).get("route") or {}).get("budget_s") or 0) or None
        B, Rt = self.B, self.Rt
        n_esc = self.n_esc = self.escapes()
        if n_esc:
            self.log(f"  {n_esc} escape vias to the planes")
        for x0, y0, x1, y1 in getattr(self, "fields", ()):      # no via of the router's in a broken-out ball field
            B.via_ok &= ~B._rect(x0, y0, x1, y1)
        jobs = self.jobs()
        by_net = {j.net: j for j in jobs}
        self.by_pads = {j.net: j.pads for j in jobs}
        pairs = self.pairs(jobs) if self.couple else {}
        queue = [j.net for j in jobs]
        if pairs:                                       # a pair routes together, as soon as its first half is due
            q2, seen = [], set()
            for n in queue:
                if n in seen:
                    continue
                q2.append(n)
                seen.add(n)
                m = pairs.get(n)
                if m in by_net and m not in seen:
                    q2.append(m)
                    seen.add(m)
            queue = q2
        self.coupled = {}
        rips, failed = {}, {}
        total = len(jobs)
        done, queue = self._on_their_layers(queue, by_net, pairs, total)
        if self.ports and self.v2 and not self.sketch:   # a dense board: pairs and guided nets first, the rest negotiated
            first = [n for n in queue if n in pairs or self._mode(n)[0] == "guided"]
            done = self._negotiate(first, by_net, pairs, rips, failed, done, total, max_rips)
            done = self._pathfinder([n for n in queue if n not in first], by_net, failed, done, total)
        else:
            done = self._negotiate(queue, by_net, pairs, rips, failed, done, total, max_rips)
        if failed and self.v2:
            if getattr(self, "budget_s", None):           # the second chance gets a time of its own: half the budget
                self.t_start, self.budget_s, self.said_budget = time.time(), self.budget_s / 2, False
            done = self.second_chance(by_net, pairs, failed, done, total, max_rips)
        self._finish_run(t0, failed, total)
        return self._result(t0, failed, total)

    def _on_their_layers(self, queue, by_net, pairs, total):
        """A first pass for nets a breakout took out of a dense part (tw.breakout): each from the end of its escape on
        that escape's layer only, no vias, shortest first -- the far end is a fine-pitch part's via (every layer) or a
        pad on that layer. On each layer this is nearly planar once the pin plan has ordered the pins, and it needs
        no rip-up; the nets that do not go this way are left to the negotiation. Returns (routed, the queue left)."""
        Rt, B = self.Rt, self.B
        cand = []
        for net in queue:
            es = [e for e in self.ports.get(net, []) if e.get("layer") in B.layers and e.get("segments")]
            if len(es) != 1 or net in pairs or self._mode(net)[0] == "guided":
                continue
            far = [p["pos"] for p in by_net[net].pads if (p["ref"], p["num"]) != (es[0]["ref"], es[0]["pad"])]
            if not far:
                continue
            cand.append((min(geom.dist(es[0]["at"], q) for q in far), net, es[0]["layer"]))
        done, left = 0, []
        routed = set()
        for _, net, layer in sorted(cand):
            n0 = len(Rt.routes)
            fail = self.route_job(by_net[net], allow_vias=False, layers=[layer])
            if fail is None:
                done += 1
                routed.add(net)
                recs = [r for r in Rt.routes[n0:] if r["net"] == net]
                self._emit("routed", net, {"segments": [sg for r in recs for sg in r["segments"]],
                                           "vias": [v for r in recs for v in r["vias"]]}, done=done, total=total,
                           note="on its escape's layer")
            else:
                Rt.rip([net])
                Rt.rebuild()
        if cand:
            self.log(f"  {len(routed)} of {len(cand)} broken-out nets routed on their escapes' layers")
        return done, [q for q in queue if q not in routed]

    def _negotiate(self, queue, by_net, pairs, rips, failed, done, total, max_rips):
        """Route the queue, ripping up whatever stands in a net's way and routing that again after it
        (PathFinder history makes contested copper dearer each time); a net that has ripped others
        max_rips times gets cheaper vias, then is left failed. Returns the routed count."""
        B, Rt = self.B, self.Rt
        while queue:
            net = queue.pop(0)
            job = by_net[net]
            before = len(Rt.routes)
            cc = None
            mate = pairs.get(net)
            if mate and any(r["net"] == mate for r in Rt.routes):
                cc = self.corridor(net, mate, job.prof)
            elif self.sketch:
                cc = self.sketch_cost()
            keep = (cc == 0).reshape(len(B.layers), B.N) if (cc is not None and self.sketch and not mate) else None
            fail = self.route_job(job, cell_cost=cc, layers=self.layers_of(mate) if cc is not None and mate else None, keep_to=keep)
            if fail is not None and cc is not None:      # no room beside its partner: route it on its own
                Rt.rip([net])
                Rt.rebuild()
                fail = self.route_job(job)
                cc = None
            if fail is not None and getattr(job, "layers", None):    # no way on its planned layers: any, and say so
                Rt.rip([net])
                Rt.rebuild()
                job.layers = None
                fail = self.route_job(job)
                if fail is None:
                    self.off_plan.append(net)
            if fail is not None and mate in by_net and any(r["net"] == mate for r in Rt.routes) and not job.__dict__.get("swapped"):
                # the partner walled it in (a USB-C receptacle's D- pads sit between the D+ pads): route this
                # half first, then the partner beside it, the way the breakout is drawn by hand
                Rt.rip([net, mate])
                Rt.rebuild()
                job.swapped = by_net[mate].swapped = True
                if self.route_job(job) is None:
                    cc2 = self.corridor(mate, net, by_net[mate].prof)
                    if self.route_job(by_net[mate], cell_cost=cc2, layers=self.layers_of(net) if cc2 is not None else None) is None:
                        self.coupled[mate] = net
                        fail = None
                        recs = [r for r in Rt.routes[before:] if r["net"] in (net, mate)]
                        done += 1
                        self._emit("routed", mate, {"segments": [sg for r in recs if r["net"] == mate for sg in r["segments"]],
                                                    "vias": [v for r in recs if r["net"] == mate for v in r["vias"]]},
                                   done=done, total=total, note="pair routed together")
                    else:
                        Rt.rip([mate])
                        Rt.rebuild()
                        if self.route_job(by_net[mate]) is None:
                            fail = None
                        else:
                            Rt.rip([net, mate])
                            Rt.rebuild()
                            fail = self.route_job(job)
                            queue.insert(0, mate)
                else:
                    Rt.rip([net])
                    Rt.rebuild()
                    queue.insert(0, mate)
                    fail = self.route_job(job)
            if fail is None and cc is not None:
                self.coupled[net] = mate
            elif fail is None and net in self.coupled and not job.__dict__.get("swapped"):
                self.coupled.pop(net, None)
            if fail is None:
                failed.pop(net, None)
                done += 1
                recs = [r for r in Rt.routes[before:] if r["net"] == net]
                merged = {"segments": [s for r in recs for s in r["segments"]], "vias": [v for r in recs for v in r["vias"]]}
                self._emit("routed", net, merged, done=done, total=total)
                continue
            tree, tgt, where = fail
            if rips.get(net, 0) >= max_rips or self._over_budget():      # out of rips, or of time: no more rip-up
                Rt.rip([net])
                Rt.rebuild()
                vc = Rt.via_cost
                Rt.via_cost = min(vc, CHEAP_VIA)
                ok = self.route_job(job) is None
                if not ok:
                    Rt.rip([net])
                    Rt.rebuild()
                    self.route_job(job, keep_going=True)
                Rt.via_cost = vc
                recs = [r for r in Rt.routes if r["net"] == net and not r.get("fixed")]
                merged = {"segments": [s for r in recs for s in r["segments"]], "vias": [v for r in recs for v in r["vias"]]}
                if ok:
                    failed.pop(net, None)
                    done += 1
                    self._emit("routed", net, merged, done=done, total=total, note="cheaper vias")
                else:
                    failed[net] = where
                    self._emit("failed", net, merged, where=where, done=done, total=total)
                continue
            who = Rt.blockers(net, job.prof, tree, tgt, fcu_factor=job.fcu)
            Rt.rip([net])
            if who is None:
                Rt.rebuild()
                failed[net] = f"{where} (no path even on the fixed copper)"
                self._emit("failed", net, where=failed[net], done=done, total=total)
                continue
            rips[net] = rips.get(net, 0) + 1
            if not who:
                Rt.rebuild()
                queue.insert(0, net)
                continue
            who = sorted(w for w in who if w in by_net)
            Rt.add_history(Rt.last_taken, amount=self.hist_amount)
            Rt.rip(who)
            Rt.rebuild()
            for w in who:
                self._emit("ripped", w, by=net)
                done = max(0, done - 1)
            queue = [net] + who + [q for q in queue if q not in who and q != net]
        return done

    def _pathfinder(self, queue, by_net, failed, done, total, rounds=60):
        """Negotiated congestion (PathFinder, McMurchie and Ebeling), for boards too full for rip-up one net at a time.
        Every net is routed on the fixed copper alone: a cell another net's track holds is not a wall but costs more,
        the price rising each round, and cells contested in earlier rounds stay dearer for good. Round after round the
        nets that still share copper are routed again, until none do or the time budget is spent; then the nets
        still sharing are ripped (the most contested first, until the rest are clear) and routed on what is free.
        The nets already routed (pairs, the escapes' first pass) take part: one in another's way moves too.
        Returns the routed count; failed gets the nets that found no way."""
        B, Rt = self.B, self.Rt
        mine = set(queue) | {n for n in by_net if n not in self.coupled and n not in self.coupled.values()
                             and any(r["net"] == n and not r.get("fixed") for r in Rt.routes)}
        # what does not take part (pairs, the planes' escape vias) is fixed copper for the negotiation: a wall, not a price
        saved_fixed = (B.T0, B.V0)
        theirs = [r for r in Rt.routes if r["net"] in mine and not r.get("fixed")]
        Rt.rip(mine)
        Rt.rebuild()
        B.snapshot()
        Rt.routes += theirs
        Rt.rebuild()
        Rt.pf = {"hist": np.zeros(len(B.layers) * B.N, dtype=np.float32), "pres": PF_PRES}
        todo = list(queue)
        t_round = time.time()
        try:
            for rnd in range(rounds):
                for net in todo:
                    Rt.rip([net])
                    Rt.rebuild()
                    fail = self.route_job(by_net[net], keep_going=True)
                    if fail is not None:
                        failed[net] = fail[2]
                    else:
                        failed.pop(net, None)
                conf = self._conflicts(mine)
                self.log(f"  round {rnd + 1}: {len(conf)} nets share copper, {sum(len(c) for c in conf.values())} cells "
                         f"({time.time() - t_round:.0f} s)")
                t_round = time.time()
                if not conf or self._over_budget():
                    break
                for cells in conf.values():
                    Rt.pf["hist"][np.fromiter((l * B.N + i for l, i in cells), dtype=np.int64)] += PF_HIST
                Rt.pf["pres"] *= PF_GROW
                # the most contested last: the others settle first, it then finds what they left
                todo = sorted(conf, key=lambda n: len(conf[n]))
        finally:
            Rt.pf = None
            B.T0, B.V0 = saved_fixed
            Rt.rebuild(full=True)
        conf = self._conflicts(mine)
        self.contested = self._contested(conf)
        if self.contested:
            self.log("  contested: " + "; ".join(f"{c['nets']} nets at ({c['at'][0]:.1f}, {c['at'][1]:.1f}) on {c['layer']}"
                                                 for c in self.contested[:6]))
        again = []
        for net in sorted(conf, key=lambda n: -len(conf[n])):    # clear the board: the most contested go first
            if self._conflicts([net]):
                Rt.rip([net])
                Rt.rebuild()
                again.append(net)
        if again:
            self.log(f"  {len(again)} nets still sharing copper: ripped and routed again on what is free")
        for net in sorted(again, key=lambda n: len(conf[n])):
            fail = self.route_job(by_net[net])
            if fail is not None:
                Rt.rip([net])
                Rt.rebuild()
                self.route_job(by_net[net], keep_going=True)
                failed[net] = fail[2]
            else:
                failed.pop(net, None)
        done = 0
        for net in by_net:
            recs = [r for r in Rt.routes if r["net"] == net and not r.get("fixed")]
            if net not in failed:
                done += 1
            if net in failed:
                self._emit("failed", net, {"segments": [s for r in recs for s in r["segments"]],
                                           "vias": [v for r in recs for v in r["vias"]]}, where=failed[net], done=done,
                           total=total)
            elif recs and net in mine:
                self._emit("routed", net, {"segments": [s for r in recs for s in r["segments"]],
                                           "vias": [v for r in recs for v in r["vias"]]}, done=done, total=total,
                           note="negotiated")
        return done

    def _contested(self, conf, cell=1.0):
        """Where nets still share copper after a negotiation, the busiest places first: [{at, layer, nets, names}] on a
        1 mm grid -- the board's real bottlenecks (more room, a layer, or another pin there)."""
        B = self.B
        spots = collections.defaultdict(set)
        for net, cells in conf.items():
            for l, idx in cells:
                x, y = B.xy(idx // B.nx, idx % B.nx)
                spots[(l, round(x / cell), round(y / cell))].add(net)
        out = [{"at": [k[1] * cell, k[2] * cell], "layer": B.layers[k[0]], "nets": len(v), "names": sorted(v)}
               for k, v in spots.items() if len(v) >= 2]
        return sorted(out, key=lambda c: -c["nets"])[:40]

    def _conflicts(self, nets):
        """{net: [(layer index, cell)]} for the nets whose copper another net's comes too near: the cells under its
        tracks and inside its vias where the grid (its own profile, the neck's in a neck area) says another net's
        copper is within clearance."""
        B, Rt = self.B, self.Rt
        by = collections.defaultdict(list)
        for r in Rt.routes:
            if r["net"] in nets and not r.get("fixed"):
                by[r["net"]].append(r)
        out = {}
        for net, recs in by.items():
            prof = recs[0]["profile"]
            lt, _ = B.legal(net, prof)
            cells = Rt.cells_of([s for r in recs for s in r["segments"]])
            for r in recs:
                for pos, d, _ in r["vias"]:
                    j0, i0 = B.cell(*pos)
                    pr = B.profiles[prof]
                    hw = B.profiles[pr.neck].hw if pr.neck and B.neck_mask[j0, i0] else pr.hw
                    k = max(0, int((d / 2 - hw) / R.RES))
                    for dj in range(-k, k + 1):
                        for di in range(-k, k + 1):
                            if dj * dj + di * di <= k * k and 0 <= j0 + dj < B.ny and 0 <= i0 + di < B.nx:
                                idx = (j0 + dj) * B.nx + i0 + di
                                cells += [(l, idx) for l in range(len(B.layers))]
            bad = [(l, i) for l, i in cells if not lt[l][i]]
            if bad:
                out[net] = bad
        return out

    def _over_budget(self):
        """Past the route's time budget (tracewright.json route.budget_s, or the route's own): the nets left are routed
        where they fit, nothing more is ripped up, and the route ends with what failed named."""
        b = getattr(self, "budget_s", None)
        if not b:
            return False
        over = time.time() - getattr(self, "t_start", time.time()) > b
        if over and not getattr(self, "said_budget", False):
            self.said_budget = True
            self.log(f"  {b:g} s spent: no more rip-up, the rest routed where it fits")
        return over

    def _finish_run(self, t0, failed, total):
        self.failed = failed
        if self.do_cleanup and not self.sketch:       # a sketch is the user's way: not straightened into a shorter one
            self._pair_mates = set(self.coupled.values())
            # the second look takes at most as long again as the routing did (20 s at least)
            n_better = self.refine(budget=max(REFINE_MIN_S, time.time() - t0)) if self.v2 else self.cleanup()
            self.straightened = n_better
            if n_better:
                self.log(f"  {n_better} nets straightened on a second pass")
            if getattr(self, "refine_left", 0):
                self.log(f"  {self.refine_left} nets left as routed (time)")
        self.n_stitch = self.stitch() if self.planes else 0
        if self.n_stitch:
            self.log(f"  {self.n_stitch} stitching vias")

    def _result(self, t0, failed, total):
        Rt, n_esc, n_stitch = self.Rt, self.n_esc, self.n_stitch
        segs = [(r["net"], s) for r in Rt.routes if not r.get("fixed") for s in r["segments"]]
        vias = [(r["net"], v) for r in Rt.routes if not r.get("fixed") for v in r["vias"]]
        length = sum(geom.dist(a, b) for _, (l, a, b, w) in segs)
        per = {}
        for r in Rt.routes:
            if r.get("fixed"):
                continue
            e = per.setdefault(r["net"], {"length_mm": 0.0, "vias": 0, "layers": set()})
            e["length_mm"] += sum(geom.dist(a, b) for l, a, b, w in r["segments"])
            e["vias"] += len(r["vias"])
            e["layers"] |= {l for l, a, b, w in r["segments"]}
        self.per_net = {n: {"length_mm": round(v["length_mm"], 1), "vias": v["vias"], "layers": sorted(v["layers"])} for n, v in per.items()}
        summary = {"nets": total, "routed": total - len(failed), "failed": failed, "tracks": len(segs), "vias": len(vias),
                   "preset": self.preset, "left_for_hand": sorted(self.left_for_hand), "off_layer_plan": sorted(self.off_plan),
                   "length_mm": round(length, 1), "seconds": round(time.time() - t0, 1), "escapes": n_esc,
                   "stitching_vias": n_stitch, "neck_areas": [ref for ref, _ in self.necks],
                   "coupled_pairs": self._coupled_pairs(),
                   "straightened": getattr(self, "straightened", 0), "left_as_routed": getattr(self, "refine_left", 0)}
        if getattr(self, "contested", None):            # a negotiation's bottlenecks (dense boards)
            summary["contested"] = self.contested
        return summary, segs, vias

    def _coupled_pairs(self):
        """[(a, b, coupling)] once per pair: the coupling of the half routed beside the other."""
        out, seen = [], set()
        for a, b in sorted(getattr(self, "coupled", {}).items()):
            k = frozenset((a, b))
            if k in seen or not any(r["net"] == a for r in self.Rt.routes) or not any(r["net"] == b for r in self.Rt.routes):
                continue
            seen.add(k)
            out.append((a, b, round(self.coupling(a, b), 2)))
        return out

    def second_chance(self, by_net, pairs, failed, done, total, max_rips):
        """Nets that lost the negotiation (two nets taking turns to rip each other up until one ran out of
        tries) get one more round on the board as it stands: a renewed rip budget, and contested copper
        weighed three times as much, so the loser's rivals look for another way. Kept only if more nets
        end up routed; otherwise the board goes back to how it was."""
        Rt = self.Rt
        lost = sorted(n for n in failed if "no path even on the fixed copper" not in str(failed[n]))
        if not lost:                                    # walled in by pads and keepouts: rip-up cannot help
            return done
        saved = (list(Rt.routes), dict(failed), dict(self.coupled), Rt.hist.copy(), Rt.use_hist, done)
        Rt.rip(lost)                                    # their partial copper
        Rt.rebuild()
        for n in lost:
            failed.pop(n, None)
        self.hist_amount = 3 * HIST_AMOUNT
        try:
            done = self._negotiate(list(lost), by_net, pairs, {}, failed, done, total, max_rips)
        finally:
            self.hist_amount = HIST_AMOUNT
        if len(failed) < len(saved[1]):
            self.log(f"  second chance: {len(saved[1]) - len(failed)} of {len(saved[1])} failed nets routed")
            return done
        Rt.routes, Rt.hist, Rt.use_hist = saved[0], saved[3], saved[4]
        Rt.rebuild()
        failed.clear()
        failed.update(saved[1])
        self.coupled = saved[2]
        for n in lost:                                  # the live view shows the board as it was
            recs = [r for r in Rt.routes if r["net"] == n and not r.get("fixed")]
            self._emit("failed", n, {"segments": [sg for r in recs for sg in r["segments"]],
                                     "vias": [v for r in recs for v in r["vias"]]}, where=failed[n], done=saved[5], total=total)
        return saved[5]

    # ------------------------------------------------------------------ v2: a second look
    def _cost(self, recs):
        length = sum(geom.dist(a, b) for r in recs for (_, a, b, _) in r["segments"])
        return length + sum(len(r["vias"]) for r in recs) * self.Rt.via_cost / 100.0      # a via as its mm of track

    def pairs(self, jobs):
        """{net: partner} for interface pairs (USB, MIPI, HDMI, Ethernet ...) among the jobs."""
        from ..checks.signal import find_pairs, pair_kind
        out = {}
        for a, b in find_pairs([j.net for j in jobs]):
            if pair_kind(a):
                out[a], out[b] = b, a
        return out

    def sketch_cost(self):
        """A cost field keeping a route to the user's sketch: free within SKETCH_W of it (on its layer, if it names
        one), SKETCH_K a cell elsewhere -- the search still goes round what is in the way."""
        if getattr(self, "_sketch_cc", None) is not None:
            return self._sketch_cc
        B = self.B
        cc = np.full(len(B.layers) * B.N, SKETCH_K, dtype=np.float32)
        for line in self.sketch:
            pts = line.get("p") or []
            ls = [B.layers.index(line["layer"])] if line.get("layer") in B.layers else list(range(len(B.layers)))
            for a, b in zip(pts, pts[1:]):
                j0, i0 = B.cell(min(a[0], b[0]) - SKETCH_W, min(a[1], b[1]) - SKETCH_W)
                j1, i1 = B.cell(max(a[0], b[0]) + SKETCH_W, max(a[1], b[1]) + SKETCH_W)
                j0, i0, j1, i1 = max(0, j0), max(0, i0), min(B.ny - 1, j1), min(B.nx - 1, i1)
                if j1 < j0 or i1 < i0:
                    continue
                X, Y = B.GX[j0:j1 + 1, i0:i1 + 1], B.GY[j0:j1 + 1, i0:i1 + 1]
                jj, ii = np.nonzero(R._seg_dist(X, Y, a[0], a[1], b[0], b[1]) <= SKETCH_W)
                for l in ls:
                    cc[l * B.N + (jj + j0) * B.nx + (ii + i0)] = 0.0
        self._sketch_cc = cc
        return cc

    def layers_of(self, net):
        """On a board with more than two routing layers, the layers a routed net's tracks are on (a pair's second
        half keeps to them: one reference plane, one impedance), else None (any layer)."""
        if len(self.B.layers) <= 2:
            return None
        ls = {sg[0] for r in self.Rt.routes if r["net"] == net for sg in r["segments"]}
        return ls or None

    def corridor(self, net, mate, prof):
        """A cost field for the second half of a pair: free in a band at the pair's pitch beside the first
        half's route, PAIR_K a cell everywhere else, so the two run side by side."""
        B, Rt = self.B, self.Rt
        recs = [[sg for sg in r["segments"] if sg[0] in B.layers] for r in Rt.routes if r["net"] == mate]
        recs = [r for r in recs if r]
        if not recs:
            return None
        pitch, tol = self.pair_pitch(net, mate, prof)
        cc = np.full(len(B.layers) * B.N, PAIR_K, dtype=np.float32)
        mine = [tuple(p["pos"]) for p in self.by_pads.get(net, [])]
        cross = lambda a, b, x, y: (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
        for segs in recs:
            # one side of the partner for the whole of its route: the side this half's pads are on, each
            # judged against the partner's track nearest to it (both sides when the ends disagree: the
            # pair has to cross once)
            vote = 0.0
            for q in mine:
                _, a, b, _ = min(segs, key=lambda sg: geom.seg_point_dist(q, sg[1], sg[2]))
                if geom.dist(a, b) > 1e-6:
                    vote += np.sign(cross(a, b, *q)) / (1.0 + geom.seg_point_dist(q, a, b))
            s0 = float(np.sign(vote)) if abs(vote) > 0.05 else 0.0
            for lname, a, b, _ in segs:
                l = B.layers.index(lname)
                pad = pitch + tol + 0.3
                j0, i0 = B.cell(min(a[0], b[0]) - pad, min(a[1], b[1]) - pad)
                j1, i1 = B.cell(max(a[0], b[0]) + pad, max(a[1], b[1]) + pad)
                j0, i0, j1, i1 = max(0, j0), max(0, i0), min(B.ny - 1, j1), min(B.nx - 1, i1)
                X, Y = B.GX[j0:j1 + 1, i0:i1 + 1], B.GY[j0:j1 + 1, i0:i1 + 1]
                d = R._seg_dist(X, Y, a[0], a[1], b[0], b[1])
                band = (d >= pitch - R.RES * 0.5) & (d <= pitch + tol)
                if s0 and geom.dist(a, b) > 1.0:
                    band &= np.sign(cross(a, b, X, Y)) == s0
                jj, ii = np.nonzero(band)
                cc[l * B.N + (jj + j0) * B.nx + (ii + i0)] = 0.0
        return cc

    def pair_pitch(self, net, mate, prof):
        """Centre-to-centre distance of the pair's two tracks: the class's pair gap, or the closest the
        clearance allows when that is wider (the router keeps every net at its clearance); and the band's
        width beyond it (three grid cells)."""
        B = self.B
        c = self.pro.cls(self.net_class.get(net, "Default"))
        w = B.profiles[prof].w
        wm = B.profiles[self.net_class.get(mate, "Default")].w if self.net_class.get(mate) in B.profiles else w
        gap = max(float(c.get("diff_pair_gap") or 0.0), B.profiles[prof].cl)
        return w / 2 + wm / 2 + gap + R.RES, 3 * R.RES

    def coupling(self, net, mate):
        """How much of `net`'s track runs beside its partner at the pair's pitch (0..1)."""
        Rt = self.Rt
        mine = [sg for r in Rt.routes if r["net"] == net for sg in r["segments"]]
        theirs = [sg for r in Rt.routes if r["net"] == mate for sg in r["segments"]]
        if not mine or not theirs:
            return 0.0
        pitch, tol = self.pair_pitch(net, mate, self.net_class.get(net, "Default"))
        tol += R.RES
        near = total = 0.0
        for l, a, b, _ in mine:
            L = geom.dist(a, b)
            n = max(1, int(L / 0.2))
            for k in range(n):
                t = (k + 0.5) / n
                q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                dmin = min((geom.seg_point_dist(q, x, y) for l2, x, y, _ in theirs if l2 == l), default=9e9)
                total += L / n
                if abs(dmin - pitch) <= tol:
                    near += L / n
        return near / total if total else 0.0

    def refine(self, budget=None):
        """A second look at the nets a reviewer would query, each rerouted with everything else in place
        and kept only when it costs less (length, and a via as 10 mm of track): a net with vias is tried
        on one layer first, then as before; a net that wanders (over 1.4x its shortest length) again.
        The worst go first; budget (seconds) stops the pass, leaving the rest as they were."""
        Rt = self.Rt
        t_end = time.time() + budget if budget else None
        by_net = {j.net: j for j in self.jobs()}
        pads = collections.defaultdict(list)
        for p in self.dump["pads"]:
            if p["net"]:
                pads[p["net"]].append(tuple(p["pos"]))
        from ..checks.layout_quality import _mst
        cur = {}
        for r in Rt.routes:
            if not r.get("fixed") and r["net"] in by_net:
                cur.setdefault(r["net"], []).append(r)
        todo = []
        mates = getattr(self, "_pair_mates", set())
        for n, recs in cur.items():
            # the half a partner was routed beside may move only along its partner; the partner stays
            if n in self.failed or (n in mates and n not in self.coupled):
                continue
            vias = sum(len(r["vias"]) for r in recs)
            length = sum(geom.dist(a, b) for r in recs for (_, a, b, _) in r["segments"])
            m = _mst(pads[n]) or 1.0
            if vias or length / m > 1.4:
                todo.append((-(vias * 10 + length / m), n))
        better = 0
        self.refine_left = 0
        for i, (_, net) in enumerate(sorted(todo)):
            if t_end and time.time() > t_end:
                self.refine_left = len(todo) - i
                break
            old = [r for r in Rt.routes if r["net"] == net and not r.get("fixed")]
            c0 = self._cost(old)
            mate = self.coupled.get(net)
            cc = self.corridor(net, mate, by_net[net].prof) if mate else None
            k0 = self.coupling(net, mate) if mate else None
            best = None
            for vias_ok in ((False, True) if any(r["vias"] for r in old) else (True,)):
                Rt.rip([net])
                Rt.rebuild()
                n0 = len(Rt.routes)
                fail = self.route_job(by_net[net], allow_vias=vias_ok, cell_cost=cc,
                                      layers=self.layers_of(mate) if cc is not None else None)
                new = [r for r in Rt.routes[n0:] if r["net"] == net]
                ok = fail is None and new and self._cost(new) < (best[0] if best else c0) - 0.5
                if ok and mate:                         # still beside its partner
                    ok = self.coupling(net, mate) >= k0 - 0.05
                if ok:
                    best = (self._cost(new), new)
                Rt.rip([net])
            Rt.routes += best[1] if best else old
            Rt.rebuild()
            if best:
                better += 1
        return better

    def cleanup(self, rounds=2):
        """Each routed net ripped and routed again with everything else in place, worst detour first; the
        new route is kept when it costs less (length, and a via as 10 mm). Nets routed early did not know
        the later ones, later ones went round the earlier: a second look straightens both."""
        Rt = self.Rt
        by_net = {j.net: j for j in self.jobs()}
        pads = collections.defaultdict(list)
        for p in self.dump["pads"]:
            if p["net"]:
                pads[p["net"]].append(tuple(p["pos"]))
        from ..checks.layout_quality import _mst
        mst = {n: _mst(pads[n]) or 1.0 for n in by_net}
        better = 0
        for _ in range(rounds):
            cur = {}
            for r in Rt.routes:
                if not r.get("fixed") and r["net"] in by_net:
                    cur.setdefault(r["net"], []).append(r)
            order = sorted(cur, key=lambda n: -self._cost(cur[n]) / mst[n])
            # only what could improve: a detour, or a layer change a straight run might not need
            order = [n for n in order if self._cost(cur[n]) / mst[n] > 1.25 or any(r["vias"] for r in cur[n])]
            changed = 0
            for net in order:
                if net in self.failed:
                    continue
                old = [r for r in Rt.routes if r["net"] == net and not r.get("fixed")]
                if not old:
                    continue
                c0 = self._cost(old)
                Rt.rip([net])
                Rt.rebuild()
                n0 = len(Rt.routes)
                fail = self.route_job(by_net[net])
                new = [r for r in Rt.routes[n0:] if r["net"] == net]
                if fail is None and new and self._cost(new) < c0 - 0.5:
                    changed += 1
                    continue
                Rt.rip([net])
                Rt.routes += old
                Rt.rebuild()
            better += changed
            if not changed:
                break
        return better

    def ops(self, segs, vias):
        ops = []
        # the neck areas as the router used them: a missing one is added, one drawn for an older layout (the part
        # moved, a different margin, another stack-up) is redrawn, or DRC would hold the necked tracks to the full clearance.
        # On every copper layer: the planes' fills under the part keep the neck clearance too (a rule matches a zone's
        # fill only on a layer the area is on; at the class clearance the via field walls its own net's vias in)
        have = {z.name: (geom.bbox([q for pl in z.outline for q in pl]), set(z.layers)) for z in self.b.zones
                if z.is_rule_area and z.outline}
        for ref, (x0, y0, x1, y1) in self.necks:
            name = f"TW neck {ref}"
            if name not in have or max(abs(a - b) for a, b in zip(have[name][0], (x0, y0, x1, y1))) > 0.05 \
                    or have[name][1] != set(self.b.copper):
                ops.append({"op": "rule_area", "name": name, "layers": list(self.b.copper),
                            "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "no_tracks": False, "no_vias": False,
                            "no_pour": False, "no_footprints": False})
        if self.clear:
            nets = sorted({n for n in self.b.nets if self._net_ok(n)})
            if self.only is None:        # all of it again: copper left on no net, or on a pad's unconnected-(...) net
                nets += sorted({n for n in self.b.nets if n.startswith("unconnected-")}) + [""]   # (a part moved onto it) goes too
            ops.append({"op": "delete", "nets": nets, "kinds": ["track", "via"]})
        ops.append({"op": "tracks", "items": [{"net": n, "layer": l, "a": [round(a[0], 4), round(a[1], 4)],
                                               "b": [round(b[0], 4), round(b[1], 4)], "w": w} for n, (l, a, b, w) in segs]})
        ops.append({"op": "vias", "items": [{"net": n, "x": round(p[0], 4), "y": round(p[1], 4), "d": d, "drill": dr}
                                            for n, (p, d, dr) in vias]})
        if any(not z.is_rule_area for z in self.b.zones):
            ops.append({"op": "fill"})
        return ops


def route(project=None, nets=None, engine="grid", clear=False, apply=True, on_progress=None, live="auto", log=print, v2=None,
          sketch=None, tune_after=True, budget_s=None):
    """Route and (by default) write the copper to the board. Returns {'summary', 'apply'}. v2: pairs routed
    together (the enclosed half first, the other beside it) and a second look at nets with vias or
    detours; the default follows tracewright.json route.v2 (on unless set false). sketch: [{"p": [[x, y], ...],
    "layer"?}], the user's line of where the nets should run (a review flag's route sketch)."""
    project = project or env.project()
    if engine == "freerouting":
        from . import freerouting
        return freerouting.route(project, on_progress=on_progress, log=log)
    if v2 is None:
        v2 = ((getattr(project, "cfg", None) or {}).get("route") or {}).get("v2", True)
    g = GridRoute(project, nets=nets, clear=clear, on_progress=on_progress, log=log, v2=v2, sketch=sketch).setup()
    g.budget_s = budget_s
    summary, segs, vias = g.run()
    out = {"summary": summary}
    _report(project, g, summary)
    if apply and (segs or vias or clear):
        from ..pcb import client, rules
        if g.necks:
            rules.ensure_rules(project, dict([rules.neck_rule(g.neck_cl)]), replace=True)
            used = {(round(d_, 3), round(dr_, 3)) for _, (p_, d_, dr_) in vias}
            nvia = getattr(g, "neck_via", None)
            neck_board_rules(project, g.neck_w, g.neck_cl,
                             via=nvia if nvia and (round(nvia[0], 3), round(nvia[1], 3)) in used else None)
        out["apply"] = client.apply(project, g.ops(segs, vias), live=live)
        if out["apply"].get("ok") and g.ports:          # escapes laid for nets that were joined another way: off again
            from .. import breakout
            out["summary"]["unused_escapes"] = breakout.prune(project, keep_failed=list(summary.get("failed") or []))
        finish = ((getattr(project, "cfg", None) or {}).get("route") or {}).get("finish", "none")
        if out["apply"].get("ok") and summary.get("failed") and finish == "freerouting":
            # what the grid router left open on a dense board, finished by Freerouting (push and shove, negotiated
            # congestion) with every track and via already there held fixed
            from . import freerouting
            if freerouting.find_jar() and freerouting.java()[0]:
                log(f"  {len(summary['failed'])} nets left: Freerouting finishes them, the rest held fixed")
                fr = freerouting.route(project, keep_routed=True, log=log, on_progress=on_progress)
                out["summary"]["finished_by"] = "freerouting" if fr["summary"].get("ok") else None
                if not fr["summary"].get("ok"):
                    out["summary"]["finish_error"] = fr["summary"].get("error")
                else:
                    from .. import ratsnest
                    open_ = sorted({l[4] for l in ratsnest.ratsnest(Board.load(project.pcb))})
                    out["summary"]["still_open"] = open_
                    log(f"  after Freerouting: {len(open_)} net{'s' if len(open_) != 1 else ''} still open"
                        + (": " + ", ".join(n.rsplit("/", 1)[-1] for n in open_[:12]) if open_ else ""))
        if out["apply"].get("ok") and tune_after:        # pairs within their skew budget, length groups within theirs
            from . import tune as tunemod
            try:
                t = tunemod.tune(project, log=log)
                out["summary"]["tuned"] = [x["net"] for x in t["tuned"]]
                if t["left"]:
                    out["summary"]["not_tuned"] = t["left"]
            except Exception as e:                       # tuning is a finish: the route stands without it
                log(f"tuning skipped: {e}")
        if out["apply"].get("ok") and g.planes:
            from ..pcb import stitch
            added = []
            for net in g.planes:
                if g._net_ok(net):
                    pr = g.B.profiles[g.net_class.get(net, "Default")]
                    added += stitch.repair(project, net, pr.via_d, pr.via_drill, live=live)
            out["summary"]["island_vias"] = len(added)
            left = islands_left(project, [n for n in g.planes if g._net_ok(n)])
            if left:
                out["summary"]["islands"] = left
    return out


def _report(project, g, summary):
    """build/route-report.json: per net, how it went -- routed (length, vias, layers), failed (where it got stuck),
    left for hand routing -- with its mode and the rules it was to keep."""
    import json, os
    nets = {}
    short = lambda n: str(n).rsplit("/", 1)[-1]
    hand = {short(n) for n in g.left_for_hand}
    failed = {short(n): v for n, v in (summary.get("failed") or {}).items()}
    per = {short(n): v for n, v in g.per_net.items()}
    for net, m in g.modes.items():                 # the netlist's names; the board's may differ by the sheet path
        e = {"mode": m["mode"], "why": m["why"], "rules": m["rules"]}
        k = short(net)
        if k in hand:
            e["status"] = "hand"
        elif k in failed:
            f = failed[k]
            e["status"] = "failed"
            e["detail"] = f if isinstance(f, str) else f"no path to the pad near {f[2]}" if isinstance(f, (list, tuple)) and len(f) > 2 else str(f)
        elif k in per:
            e.update(status="routed", **per[k])
            if "no vias" in m["rules"] and per[k]["vias"]:
                e["note"] = f"{per[k]['vias']} via{'s' if per[k]['vias'] > 1 else ''} on a line meant to have none"
            if k in {short(n) for n in getattr(g, "off_plan", [])}:
                e["note"] = (e.get("note", "") + "; " if e.get("note") else "") + "no room on its planned layers: routed on others"
        else:
            e["status"] = "untouched"
        nets[net] = e
    os.makedirs(project.build, exist_ok=True)
    with open(os.path.join(project.build, "route-report.json"), "w") as f:
        json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "preset": g.preset, "summary": {k: v for k, v in summary.items()
                   if k in ("nets", "routed", "vias", "length_mm", "seconds")}, "nets": nets}, f, indent=1, default=str)


def islands_left(project, nets):
    """{net: ["F.Cu at (x, y)", ...]}: pour islands holding a pad that no via could join to the net's main copper."""
    from ..pcb import stitch
    from ..board import Board
    b = Board.load(project.pcb)
    out = {}
    for net in nets:
        s = stitch.stray(b, net)
        if s:
            out[net] = [f"{l} at ({x}, {y})" for l, (x, y) in s]
    return out
