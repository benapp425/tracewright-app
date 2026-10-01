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
import math, time, collections
import numpy as np
from .. import env, geom
from ..board import Board
from ..pro import ProjectSettings
from . import router as R

VIA_COST = 1000.0          # a via costs as much as 10 mm of track: change layer to reach a pin or cross a bus
CHEAP_VIA = 500.0
V2_FCU_CROSS = 1.10        # F.Cu along y (B.Cu along x already); off by default: longer routes on SMD boards
PAIR_K = 25.0              # v2: cost a cell for a pair's second half away from its partner's side
REFINE_MIN_S = 20.0        # v2: the second look's time budget is the routing time, at least this
HIST_AMOUNT = 40.0         # PathFinder history added to contested cells at each rip-up


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
        d["vias"].append({"pos": (v.x, v.y), "d": v.d, "drill": v.drill, "net": v.net})
    return d


NECK_W, NECK_CL = 0.15, 0.15      # escapes from fine-pitch pads (JLC: 0.1 / 0.1 minimum)


def profiles_for(pro, net_class, nets, necks=False, neck_w=NECK_W, neck_cl=NECK_CL):
    used = {net_class.get(n, "Default") for n in nets} | {"Default"}
    profs, clear = {}, {}
    for name in sorted(used):
        c = pro.cls(name)
        w, cl = float(c["track_width"]), float(c["clearance"])
        neck = None
        if necks and (w > neck_w or cl > neck_cl):
            neck = name + "~neck"
            profs[neck] = R.Profile(neck, min(w, neck_w), min(cl, neck_cl), float(c["via_diameter"]),
                                    float(c["via_drill"]), fixed_cl=True)
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


def islands(B, Rt, net, pads, existing):
    """Groups of pads already joined by copper of the net: [(cells, points, n_pads)]."""
    parent = list(range(len(pads)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    segs = [t for t in existing["tracks"] if t["net"] == net]
    vias = [v for v in existing["vias"] if v["net"] == net]
    # a union-find over pads, track pieces and vias that touch
    nodes = [("pad", p) for p in pads] + [("seg", s) for s in segs] + [("via", v) for v in vias]
    parent = list(range(len(nodes)))

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
            if ko == "seg":
                return any(geom.dist(e, v["pos"]) <= v["d"] / 2 for e in (o["a"], o["b"]))
            if ko == "pad":
                return any(geom.inside(v["pos"], pl["outline"]) for l in o["shape"] for pl in o["shape"][l])
            return geom.dist(o["pos"], v["pos"]) < 0.01
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
    out = []
    for members in groups.values():
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
        out.append((cells, pts, len(pads_in)))
    return out


class GridRoute:
    def __init__(self, project=None, nets=None, clear=False, on_progress=None, via_cost=VIA_COST, log=print, v2=None,
                 layer_dirs=None, cleanup=None):
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
        from .. import stackup
        self.plan = stackup.get(getattr(self.p, "cfg", None))
        if self.plan and len(self.b.copper) != self.plan["layers"]:
            self.plan = None                          # not applied to the board yet: as without one
        self.routing = tuple(stackup.routing_layers(self.plan, self.b.copper))

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
        profs, clear = profiles_for(self.pro, self.net_class, nets, necks=bool(self.necks))
        B = R.Board(dump, profs, clear, self.net_class, necks=[r for _, r in self.necks], layers=self.routing)
        B.stamp_pads()
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
            B.stamp_via(v["net"], v["pos"], v["d"])
        B.snapshot()
        Rt = R.Router(B, via=self.via_cost, directions=stackup_dirs(self.plan))
        if self.layer_dirs:                           # layer directions: F.Cu runs along y, B.Cu along x
            Rt.fcu_cross = V2_FCU_CROSS
        Rt._prof_of = {n: self.net_class.get(n, "Default") for n in nets}
        self.B, self.Rt = B, Rt
        self.planes = plane_nets(b)
        return self

    # ------------------------------------------------------------------ jobs
    def jobs(self):
        pads_by = collections.defaultdict(list)
        for p in self.dump["pads"]:
            if p["net"] and self._net_ok(p["net"]):
                pads_by[p["net"]].append(p)
        out = []
        for net, pads in pads_by.items():
            if len(pads) < 2 or net in self.planes:
                continue
            prof = self.net_class.get(net, "Default")
            xs = [q["pos"][0] for q in pads]
            ys = [q["pos"][1] for q in pads]
            span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            w = self.B.profiles[prof].w
            power = w >= 0.39 or any(k in prof.upper() for k in ("PWR", "POWER", "SUPPLY"))
            out.append(Job(net, prof, pads, (0 if power else 1, span)))
        out.sort(key=lambda j: j.key)
        return out

    # ------------------------------------------------------------------ plane escapes
    def escapes(self, radius=1.6):
        """SMD pads of plane nets on a layer without that pour: a short stub to a via."""
        B, Rt = self.B, self.Rt
        made = 0
        for p in self.dump["pads"]:
            net = p["net"]
            if net not in self.planes or not self._net_ok(net) or p["drill"] > 0:
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
        for d, j, i in sorted(cand)[:400]:
            for l, idx in src[:40]:
                a = divmod(idx, B.nx)
                dg = Rt._dogleg(lt[li], a, (j, i))
                if dg:
                    pts = [B.xy(*c) for c in dg]
                    segs = [(layer, pa, pb, pr.w) for pa, pb in zip(pts, pts[1:]) if pa != pb]
                    rec = Rt.add(net, prof, segs, [(pts[-1], pr.via_d, pr.via_drill)])
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
                                rec = Rt.add(net, prof, [], [(pos, pr.via_d, pr.via_drill)])
                                self._emit("stitch", net, rec)
                                made += 1
                                lt, lv = B.legal(net, prof)
                    gx += pitch
                gy += pitch
        return made

    # ------------------------------------------------------------------ routing
    def route_job(self, job, keep_going=False, allow_vias=True, cell_cost=None, layers=None):
        B, Rt = self.B, self.Rt
        isl = islands(B, Rt, job.net, job.pads, {"tracks": [t for t in self.dump["tracks"]],
                                                  "vias": self.dump["vias"]})
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
                             layers=layers)
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
        t0 = time.time()
        B, Rt = self.B, self.Rt
        n_esc = self.n_esc = self.escapes()
        if n_esc:
            self.log(f"  {n_esc} escape vias to the planes")
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
        done = self._negotiate(queue, by_net, pairs, rips, failed, 0, total, max_rips)
        if failed and self.v2:
            done = self.second_chance(by_net, pairs, failed, done, total, max_rips)
        self._finish_run(t0, failed, total)
        return self._result(t0, failed, total)

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
            fail = self.route_job(job, cell_cost=cc, layers=self.layers_of(mate) if cc is not None else None)
            if fail is not None and cc is not None:      # no room beside its partner: route it on its own
                Rt.rip([net])
                Rt.rebuild()
                fail = self.route_job(job)
                cc = None
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
            if rips.get(net, 0) >= max_rips:
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

    def _finish_run(self, t0, failed, total):
        self.failed = failed
        if self.do_cleanup:
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
        summary = {"nets": total, "routed": total - len(failed), "failed": failed, "tracks": len(segs), "vias": len(vias),
                   "length_mm": round(length, 1), "seconds": round(time.time() - t0, 1), "escapes": n_esc,
                   "stitching_vias": n_stitch, "neck_areas": [ref for ref, _ in self.necks],
                   "coupled_pairs": self._coupled_pairs(),
                   "straightened": getattr(self, "straightened", 0), "left_as_routed": getattr(self, "refine_left", 0)}
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
        # moved, or a different margin) is redrawn, or DRC would hold the necked tracks to the full clearance
        have = {z.name: geom.bbox([q for pl in z.outline for q in pl]) for z in self.b.zones if z.is_rule_area and z.outline}
        for ref, (x0, y0, x1, y1) in self.necks:
            name = f"TW neck {ref}"
            if name not in have or max(abs(a - b) for a, b in zip(have[name], (x0, y0, x1, y1))) > 0.05:
                ops.append({"op": "rule_area", "name": name, "layers": list(self.routing),
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


def route(project=None, nets=None, engine="grid", clear=False, apply=True, on_progress=None, live="auto", log=print, v2=None):
    """Route and (by default) write the copper to the board. Returns {'summary', 'apply'}. v2: pairs routed
    together (the enclosed half first, the other beside it) and a second look at nets with vias or
    detours; the default follows tracewright.json route.v2 (on unless set false)."""
    project = project or env.project()
    if engine == "freerouting":
        from . import freerouting
        return freerouting.route(project, on_progress=on_progress, log=log)
    if v2 is None:
        v2 = ((getattr(project, "cfg", None) or {}).get("route") or {}).get("v2", True)
    g = GridRoute(project, nets=nets, clear=clear, on_progress=on_progress, log=log, v2=v2).setup()
    summary, segs, vias = g.run()
    out = {"summary": summary}
    if apply and (segs or vias or clear):
        from ..pcb import client, rules
        if g.necks:
            rules.ensure_rules(project, dict([rules.neck_rule(NECK_CL)]))
        out["apply"] = client.apply(project, g.ops(segs, vias), live=live)
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
