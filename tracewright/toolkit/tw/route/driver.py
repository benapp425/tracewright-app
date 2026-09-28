"""Route a board with the grid router: net classes -> profiles, pads -> islands, rip-up and reroute.

    ./tw route                      every unrouted connection
    ./tw route --nets SDA SCL       just these nets
    ./tw route --clear              tear up the selected nets' copper first

Order, the way a person routes: escape vias from pads to the planes (nets with a pour on another
layer), then supplies (wide classes), then signals short-first. Multi-pin nets grow as a tree from
the island with the most pads, joining the nearest island each time. When a connection fails, the
nets in the way are found (route over them at a penalty), ripped up, and routed again after it.

Two signal layers (F.Cu, B.Cu): inner layers are taken to be planes. `on_progress` is called after
every net so the caller can show routing as it happens.
"""
import math, time, collections
from .. import env, geom
from ..board import Board
from ..pro import ProjectSettings
from . import router as R

VIA_COST = 1000.0          # a via costs as much as 10 mm of track: change layer to reach a pin or cross a bus
CHEAP_VIA = 500.0


def dump_for_router(b):
    """The router's input (board mm) from a parsed board."""
    d = {"outline": [], "pads": [], "rules": [], "tracks": [], "vias": [], "footprints": []}
    if b.outline:
        d["outline"] = [{"outline": b.outline[0], "holes": []}]
        d["cutouts"] = b.outline[1:]
    for p in b.pads():
        layers = [l for l in p.layers if l.endswith(".Cu")]
        shp = {l: [{"outline": pl} for pl in p.polys] for l in layers if l in R.LAYERS}
        d["pads"].append({"ref": p.ref, "num": p.num, "net": p.net, "pos": (p.x, p.y), "layers": layers, "shape": shp,
                          "drill": min(p.drill_w, p.drill_h) if p.drill else 0.0, "size": (p.w, p.h),
                          "orient": p.angle, "npth": p.kind == "np_thru_hole"})
    for z in b.zones:
        if not z.is_rule_area:
            continue
        ko = z.keepout or {}
        layers = []
        for l in z.layers:
            layers += list(R.LAYERS) if l in ("*.Cu", "F&B.Cu") else [l]
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


def fine_pitch_areas(b, pitch=0.8, margin=1.5):
    """[(ref, (x0, y0, x1, y1))] around footprints whose pads of different nets sit closer than `pitch`."""
    out = []
    for fp in b.fp_list:
        pads = [p for p in fp.pads if p.net and any(l in R.LAYERS for l in p.layers)]
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
        segs_in = [(x["layer"], x["a"], x["b"], x["w"]) for k, x in members if k == "seg" and x["layer"] in R.LAYERS]
        if segs_in:
            cells += Rt.cells_of(segs_in)
            pts += [s[1] for s in segs_in]
        out.append((cells, pts, len(pads_in)))
    return out


class GridRoute:
    def __init__(self, project=None, nets=None, clear=False, on_progress=None, via_cost=VIA_COST, log=print):
        self.p = project or env.project()
        self.b = Board.load(self.p.pcb)
        self.pro = ProjectSettings.load(self.p.pro) if self.p.pro else ProjectSettings({})
        self.only = set(nets) if nets else None
        self.clear = clear
        self.on_progress = on_progress or (lambda ev: None)
        self.log = log
        self.via_cost = via_cost
        self.failed = {}

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
        dump = dump_for_router(b)
        if self.clear:
            dump["tracks"] = [t for t in dump["tracks"] if not self._net_ok(t["net"])]
            dump["vias"] = [v for v in dump["vias"] if not self._net_ok(v["net"])]
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
        self.necks = fine_pitch_areas(b)
        profs, clear = profiles_for(self.pro, self.net_class, nets, necks=bool(self.necks))
        B = R.Board(dump, profs, clear, self.net_class, necks=[r for _, r in self.necks])
        B.stamp_pads()
        B.stamp_rules()
        for hole in dump.get("cutouts", []):
            B.stamp("", "Default", list(R.LAYERS), "poly", hole, extra=0.3, block_all_vias=True)
        for p in dump["pads"]:
            if p["npth"] and p["drill"] > 0:          # nothing crosses a non-plated hole
                B.stamp("", "Default", list(R.LAYERS), "circle", (p["pos"][0], p["pos"][1], p["drill"] / 2), extra=0.2,
                        block_all_vias=True)
        for t in dump["tracks"]:
            if t["layer"] in R.LAYERS:
                B.stamp_track(t["net"], t["layer"], t["a"], t["b"], t["w"])
        for v in dump["vias"]:
            B.stamp_via(v["net"], v["pos"], v["d"])
        B.snapshot()
        Rt = R.Router(B, via=self.via_cost)
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
            layer = next((l for l in p["layers"] if l in R.LAYERS), None)
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
        li = R.LAYERS.index(layer)
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
            if len(cu) < 2 and not any(l not in R.LAYERS for l in cu):
                continue
            polys = [pl for z in b.zones if z.net == net and not z.is_rule_area for pl in z.outline]
            if not polys:
                continue
            prof = self.net_class.get(net, "Default")
            pr = B.profiles[prof]
            lt, lv = B.legal(net, prof)
            x0, y0, x1, y1 = b.bbox()
            r = int(reach / R.RES)
            gy = y0 + pitch / 2
            while gy < y1:
                gx = x0 + pitch / 2
                while gx < x1:
                    if any(geom.inside((gx, gy), pl) for pl in polys):
                        j0, i0 = B.cell(gx, gy)
                        best = None
                        for dj in range(-r, r + 1):
                            for di in range(-r, r + 1):
                                j, i = j0 + dj, i0 + di
                                if 0 <= j < B.ny and 0 <= i < B.nx and lv[j * B.nx + i] and lt[0][j * B.nx + i] \
                                        and lt[1][j * B.nx + i]:
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
    def route_job(self, job, keep_going=False):
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
            rec = Rt.connect(job.net, job.prof, tree, nxt[0], fcu_factor=job.fcu)
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
        n_esc = self.escapes()
        if n_esc:
            self.log(f"  {n_esc} escape vias to the planes")
        jobs = self.jobs()
        by_net = {j.net: j for j in jobs}
        queue = [j.net for j in jobs]
        rips, failed = {}, {}
        done = 0
        total = len(jobs)
        while queue:
            net = queue.pop(0)
            job = by_net[net]
            before = len(Rt.routes)
            fail = self.route_job(job)
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
            Rt.add_history(Rt.last_taken)
            Rt.rip(who)
            Rt.rebuild()
            for w in who:
                self._emit("ripped", w, by=net)
                done = max(0, done - 1)
            queue = [net] + who + [q for q in queue if q not in who and q != net]
        self.failed = failed
        n_stitch = self.stitch() if self.planes else 0
        if n_stitch:
            self.log(f"  {n_stitch} stitching vias")
        segs = [(r["net"], s) for r in Rt.routes if not r.get("fixed") for s in r["segments"]]
        vias = [(r["net"], v) for r in Rt.routes if not r.get("fixed") for v in r["vias"]]
        length = sum(geom.dist(a, b) for _, (l, a, b, w) in segs)
        summary = {"nets": total, "routed": total - len(failed), "failed": failed, "tracks": len(segs), "vias": len(vias),
                   "length_mm": round(length, 1), "seconds": round(time.time() - t0, 1), "escapes": n_esc,
                   "stitching_vias": n_stitch, "neck_areas": [ref for ref, _ in self.necks]}
        return summary, segs, vias

    def ops(self, segs, vias):
        ops = []
        have = {z.name for z in self.b.zones if z.is_rule_area}
        for ref, (x0, y0, x1, y1) in self.necks:
            name = f"TW neck {ref}"
            if name not in have:
                ops.append({"op": "rule_area", "name": name, "layers": list(R.LAYERS),
                            "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "no_tracks": False, "no_vias": False,
                            "no_pour": False, "no_footprints": False})
        if self.clear:
            nets = sorted({n for n in self.b.nets if self._net_ok(n)})
            ops.append({"op": "delete", "nets": nets, "kinds": ["track", "via"]})
        ops.append({"op": "tracks", "items": [{"net": n, "layer": l, "a": [round(a[0], 4), round(a[1], 4)],
                                               "b": [round(b[0], 4), round(b[1], 4)], "w": w} for n, (l, a, b, w) in segs]})
        ops.append({"op": "vias", "items": [{"net": n, "x": round(p[0], 4), "y": round(p[1], 4), "d": d, "drill": dr}
                                            for n, (p, d, dr) in vias]})
        if any(not z.is_rule_area for z in self.b.zones):
            ops.append({"op": "fill"})
        return ops


def route(project=None, nets=None, engine="grid", clear=False, apply=True, on_progress=None, live="auto", log=print):
    """Route and (by default) write the copper to the board. Returns {'summary', 'apply'}."""
    project = project or env.project()
    if engine == "freerouting":
        from . import freerouting
        return freerouting.route(project, on_progress=on_progress, log=log)
    g = GridRoute(project, nets=nets, clear=clear, on_progress=on_progress, log=log).setup()
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
    return out
