"""Benchmark the grid router: route boards from scratch (in memory; the files are not touched) with
each setting and compare what the route.quality scorecard reads -- nets completed, total length,
vias, median and worst detour (length over the pads' spanning tree) -- and the time it took.

    python3 -m tw.route.bench BOARD_PROJECT_DIR ... [--variants v1,v2]
"""
import sys, time, json, statistics, collections
from .. import env, geom
from ..checks.layout_quality import _mst
from . import driver


def measure(g, summary, segs, vias):
    """The scorecard of one routing result (planes and poured nets left out, as route.quality does)."""
    L = collections.defaultdict(float)
    V = collections.Counter()
    for net, (l, a, b, w) in segs:
        L[net] += geom.dist(a, b)
    for net, v in vias:
        V[net] += 1
    pads = collections.defaultdict(list)
    for p in g.dump["pads"]:
        if p["net"]:
            pads[p["net"]].append(tuple(p["pos"]))
    detours, two_pin_hops = [], 0
    for net, length in L.items():
        if net in g.planes:
            continue
        m = _mst(pads[net])
        if m > 1.0:
            detours.append(length / m)
        if len(pads[net]) == 2 and V[net] >= 3:
            two_pin_hops += 1
    return {"nets": summary["nets"], "routed": summary["routed"], "failed": len(summary["failed"]),
            "length_mm": round(sum(L[n] for n in L if n not in g.planes), 1),
            "vias": sum(V[n] for n in V if n not in g.planes),
            "detour_median": round(statistics.median(detours), 3) if detours else None,
            "detour_p90": round(sorted(detours)[int(0.9 * (len(detours) - 1))], 3) if detours else None,
            "detour_worst": round(max(detours), 2) if detours else None,
            "two_pin_nets_with_3_vias": two_pin_hops, "seconds": summary["seconds"],
            "pairs": [(a, b, c) for a, b, c in summary.get("coupled_pairs", [])],
            "straightened": summary.get("straightened", 0), "left_as_routed": summary.get("left_as_routed", 0),
            "failed_nets": summary["failed"]}


VARIANTS = {"v1": {}, "v2": {"v2": True}, "v2-dirs": {"layer_dirs": True, "cleanup": False},
            "v2-clean": {"layer_dirs": False, "cleanup": True}}


def run(project_dir, variant="v1", log=lambda *a: None):
    p = env.Project(project_dir)
    g = driver.GridRoute(p, clear=True, log=log, **VARIANTS[variant]).setup()
    summary, segs, vias = g.run()
    return measure(g, summary, segs, vias)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    variants = ["v1", "v2"]
    if "--variants" in argv:
        i = argv.index("--variants")
        variants = argv[i + 1].split(",")
        del argv[i:i + 2]
    rows = []
    for d in argv:
        for v in variants:
            t0 = time.time()
            m = run(d, v)
            m["board"], m["variant"] = d.rstrip("/").split("/")[-1], v
            rows.append(m)
            print(json.dumps(m), flush=True)
    return rows


if __name__ == "__main__":
    main()
