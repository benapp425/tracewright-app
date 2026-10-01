"""Simulation with KiCad's own ngspice (the shared library inside KiCad), each run in a process of its own
(sim_worker.py): ngspice keeps global state and can end its process on a bad netlist.

    res = sim.run(netlist, probes=["v(out)", "i(v1)"])      # {"ok", "plot", "scale", "vectors": {probe: {values}}, "log"}
    sim.summary(res, probe)                                  # {"final", "min", "max", "mean"} (magnitudes for AC)
    sim.at(res, probe, x)                                    # the probe's value at a time / frequency / sweep point
    sim.svg(res, probes, title)                              # a small line plot for the docs
    sim.simulate(folder, name, netlist, probes, title, check) # run and keep: <name>.cir, .csv, .svg, .json
    sim.runs(folder)                                         # the kept runs, newest first

    ./tw sim docs/sim/x.cir [v(out) ...]                     # run a saved netlist, print each probe's summary
"""
import json, math, os, subprocess, sys
from . import env

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_worker.py")


def library():
    """KiCad's libngspice, or None."""
    if os.environ.get("TW_NGSPICE"):
        return os.environ["TW_NGSPICE"]
    app = env.kicad().get("app")
    for cand in ([os.path.join(app, "Contents", "PlugIns", "sim", "libngspice.0.dylib"), os.path.join(app, "Contents", "Frameworks", "libngspice.0.dylib")]
                 if app else []) + ["/usr/lib/x86_64-linux-gnu/libngspice.so.0", "/usr/lib/libngspice.so.0", "/usr/local/lib/libngspice.so"]:
        if os.path.exists(cand):
            return cand
    return None


def run(netlist, probes=None, points=2000, timeout=120):
    lib = library()
    if not lib:
        return {"ok": False, "error": "no ngspice: it comes with KiCad 8 or later (Contents/PlugIns/sim/libngspice)"}
    try:
        p = subprocess.run([sys.executable, WORKER], input=json.dumps({"lib": lib, "netlist": netlist, "probes": probes or [],
                                                                         "points": points}),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the simulation took more than {timeout} s"}
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("TWJSON "):
            return json.loads(line[7:])
    return {"ok": False, "error": (p.stderr or p.stdout or "ngspice stopped")[-800:]}


def summary(res, probe):
    v = ((res.get("vectors") or {}).get(probe) or {}).get("values")
    if not v:
        return None
    return {"final": v[-1], "min": min(v), "max": max(v), "mean": sum(v) / len(v)}


def at(res, probe, x):
    """The probe's value at x on the scale (linear in between), or None."""
    vec = res.get("vectors") or {}
    sc = (vec.get(res.get("scale")) or {}).get("values") if res.get("scale") else None
    v = (vec.get(probe) or {}).get("values")
    if not v or not sc:
        return None
    for k in range(len(sc) - 1):
        if sc[k] <= x <= sc[k + 1] or sc[k] >= x >= sc[k + 1]:
            t = 0 if sc[k + 1] == sc[k] else (x - sc[k]) / (sc[k + 1] - sc[k])
            return v[k] + (v[k + 1] - v[k]) * t
    return None


def corner(res, probe, drop_db=3.0):
    """For an AC run: the first frequency where the probe falls drop_db below its start (a filter's corner), or None."""
    vec = res.get("vectors") or {}
    f = (vec.get(res.get("scale")) or {}).get("values")
    v = (vec.get(probe) or {}).get("values")
    if not f or not v or v[0] <= 0:
        return None
    ref = 20 * math.log10(v[0])
    want = ref - drop_db
    for k in range(1, len(v)):
        if v[k] > 0 and 20 * math.log10(v[k]) <= want:
            a, b = 20 * math.log10(max(v[k - 1], 1e-300)), 20 * math.log10(v[k])
            t = 0.0 if a == b else (a - want) / (a - b)           # between the two points, on a log frequency scale
            return 10 ** (math.log10(f[k - 1]) + (math.log10(f[k]) - math.log10(f[k - 1])) * t)
    return None


def si(v, unit=""):
    """A value with an SI prefix: 0.00153, "s" -> "1.53 ms"; 1000, "Hz" -> "1 kHz"."""
    if v == 0:
        return f"0 {unit}".strip()
    a = abs(v)
    for m, p in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if a >= m * 0.9995:
            return f"{float(f'{v / m:.3g}'):g} {p}{unit}".strip()
    return f"{v:.3g} {unit}".strip()


def ticks(lo, hi, n=5, cover=True):
    """About n round-number ticks (steps of 1, 2, 2.5 or 5 x 10^k): reaching past lo and hi when cover (an axis that
    grows to its ticks), else only those within lo..hi."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, n - 1)
    k = 10 ** math.floor(math.log10(raw))
    step = min((m * k for m in (1, 2, 2.5, 5, 10)), key=lambda st: abs(math.log(st / raw)))
    a, b = (math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)) if cover else (math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9))
    return [0.0 if i == 0 else i * step for i in range(a, b + 1)]


def svg(res, probes, title="", width=640, height=300):
    """A line plot of the probes against the scale, as SVG text: an AC sweep as a Bode magnitude (dB on a log
    frequency axis, decade ticks, each probe's -3 dB point marked), the rest on round-number ticks with units."""
    from html import escape
    vec = res.get("vectors") or {}
    scale = res.get("scale")
    sc = (vec.get(scale) or {}).get("values") or []
    series = [(p, (vec.get(p) or {}).get("values") or []) for p in probes]
    series = [(p, v) for p, v in series if v]
    if not sc or not series:
        return None
    ac = scale == "frequency"
    xunit = {"time": "s", "frequency": "Hz"}.get(scale, "V" if str(scale or "").startswith(("v", "V")) else "")
    if ac:
        series = [(p, [20 * math.log10(max(y, 1e-30)) for y in v]) for p, v in series]
        yunit = "dB"
    else:
        kinds = {str(p)[:2].lower() for p, _ in series}
        yunit = "V" if kinds == {"v("} else "A" if kinds == {"i("} else ""
    xs = [math.log10(x) if ac and x > 0 else x for x in sc]
    ys = [y for _, v in series for y in v if math.isfinite(y)]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if ac:
        y0 = max(y0, y1 - 120)                            # 120 dB is plenty to see a roll-off
    if y1 - y0 < 1e-12:
        y0, y1 = y0 - 1, y1 + 1
    yt = ticks(y0, y1)
    y0, y1 = min(y0, yt[0]), max(y1, yt[-1])
    L, R, T, B = 64, 18, 30, 34
    X = lambda x: L + (x - x0) / ((x1 - x0) or 1) * (width - L - R)
    Y = lambda y: height - B - (min(max(y, y0), y1) - y0) / (y1 - y0) * (height - T - B)
    cols = ["#e8803a", "#3a8ee8", "#2fa36b", "#b453c4"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="-apple-system, Helvetica, sans-serif" font-size="11">',
           f'<rect width="{width}" height="{height}" fill="#fff"/>',
           f'<text x="{L}" y="18" font-weight="600" fill="#222">{escape(title)}</text>']
    for t in yt:
        out.append(f'<line x1="{L}" x2="{width - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}" stroke="#ececec"/>'
                   f'<text x="{L - 6}" y="{Y(t) + 4:.1f}" text-anchor="end" fill="#666">{escape((f"{t:g} dB" if ac else si(t, yunit)).replace("-", "−"))}</text>')
    if ac:
        xt = [d for d in range(math.ceil(x0 - 1e-9), math.floor(x1 + 1e-9) + 1)]
        if len(xt) < 2:
            xt = [math.log10(t) for t in ticks(10 ** x0, 10 ** x1, 5, cover=False) if t > 0]
        labels = [(t, si(10 ** t, "Hz")) for t in xt]
    else:
        labels = [(t, si(t, xunit)) for t in ticks(x0, x1, 6, cover=False)]
    for i, (t, lab) in enumerate(labels):
        xx = X(t)
        anchor = "start" if xx - L < 24 else "end" if width - R - xx < 24 else "middle"
        out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{T}" y2="{height - B}" stroke="#f3f3f3"/>'
                   f'<text x="{xx:.1f}" y="{height - 14}" text-anchor="{anchor}" fill="#666">{escape(lab)}</text>')
    out.append(f'<rect x="{L}" y="{T}" width="{width - L - R}" height="{height - T - B}" fill="none" stroke="#ddd"/>')
    for i, (p, v) in enumerate(series):
        c = cols[i % 4]
        pts = " ".join(f"{X(xs[k]):.1f},{Y(v[k]):.1f}" for k in range(min(len(xs), len(v))) if math.isfinite(v[k]))
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="1.7" stroke-linejoin="round" points="{pts}"/>')
        if ac:
            f3 = corner(res, p)
            if f3:
                yy = Y(v[0] - 3.0103)
                out.append(f'<circle cx="{X(math.log10(f3)):.1f}" cy="{yy:.1f}" r="3.5" fill="{c}"/>'          # labelled below-left: clear of the curve
                           f'<text x="{X(math.log10(f3)) - 8:.1f}" y="{yy + 16:.1f}" text-anchor="end" fill="{c}">−3 dB at {escape(si(f3, "Hz"))}</text>')
    lx = width - R                                        # the legend, in the title row: the traces stay clear
    for i, (p, _) in reversed(list(enumerate(series))):
        out.append(f'<text x="{lx}" y="18" text-anchor="end" fill="{cols[i % 4]}" font-weight="600">{escape(p)}</text>')
        lx -= 7 * len(p) + 16
    out.append("</svg>")
    return "\n".join(out)


def judge(res, check):
    """(a PASS / FAIL line, "ok" | "fail") for a pass criterion {probe, at?, min?, max?, corner?: {min?, max?}}, or
    (None, "ok") without one."""
    if not check or not check.get("probe"):
        return None, "ok"
    pr = check["probe"]
    if check.get("corner"):
        val = corner(res, pr)
        lo, hi = (check["corner"] or {}).get("min"), (check["corner"] or {}).get("max")
        what = f"{pr} -3 dB at {val:.4g} Hz" if val else f"{pr}: no -3 dB point in the sweep"
    else:
        val = at(res, pr, float(check["at"])) if check.get("at") is not None else (summary(res, pr) or {}).get("final")
        lo, hi = check.get("min"), check.get("max")
        what = (f"{pr} = {val:.5g}" + (f" at {check['at']}" if check.get("at") is not None else "")) if val is not None else f"{pr}: no value"
    ok = val is not None and (lo is None or val >= float(lo)) and (hi is None or val <= float(hi))
    bounds = " and ".join(x for x in ((f"at least {lo}" if lo is not None else ""), (f"at most {hi}" if hi is not None else "")) if x)
    return f"{'PASS' if ok else 'FAIL'}: {what}" + (f" (wanted {bounds})" if bounds else ""), "ok" if ok else "fail"


def report(res, probes):
    """A line per probe: its final, lowest and highest value (and an AC probe's -3 dB point)."""
    lines = []
    for pr in probes:
        sm = summary(res, pr)
        lines.append(f"{pr}: " + (f"final {sm['final']:.6g}, min {sm['min']:.6g}, max {sm['max']:.6g}" if sm else "no such vector"))
        if res.get("scale") == "frequency" and sm:
            c = corner(res, pr)
            if c:
                lines.append(f"  -3 dB at {c:.4g} Hz")
    return lines


def simulate(folder, name, netlist, probes, title="", check=None, requirement=""):
    """Run a netlist and keep it in folder (docs/sim): <name>.cir (the netlist), .csv (the waveforms), .svg (the
    plot), .json (what it showed: each probe's summary, the criterion and its verdict). Returns the .json's content
    with "lines" (the report) and, when the run failed, "error"."""
    import csv, time
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + ".cir"), "w", encoding="utf-8") as f:
        f.write(netlist.rstrip() + "\n")
    res = run(netlist, probes)
    info = {"name": name, "title": title or name, "probes": probes, "criteria": check or None, "requirement": requirement or None,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not res.get("ok"):
        for ext in ("csv", "svg"):
            if os.path.exists(os.path.join(folder, f"{name}.{ext}")):
                os.remove(os.path.join(folder, f"{name}.{ext}"))
        info.update(status="error", error=res.get("error") or "ngspice stopped", log=(res.get("log") or [])[-10:], lines=[])
    else:
        vec = res.get("vectors") or {}
        scale = res.get("scale")
        cols = ([scale] if scale in vec else []) + [p for p in probes if p in vec]
        if cols:
            n = max(len(vec[c]["values"]) for c in cols)
            with open(os.path.join(folder, name + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for i in range(n):
                    w.writerow([("%.6g" % vec[c]["values"][i]) if i < len(vec[c]["values"]) else "" for c in cols])
        pic = svg(res, probes, title or name)
        if pic:
            with open(os.path.join(folder, name + ".svg"), "w") as f:
                f.write(pic)
        elif os.path.exists(os.path.join(folder, name + ".svg")):
            os.remove(os.path.join(folder, name + ".svg"))
        verdict, status = judge(res, check)
        info.update(plot=res.get("plot"), scale=scale, summary={p: summary(res, p) for p in probes if p in vec},
                    check=verdict, status=status, lines=report(res, probes) + ([verdict] if verdict else []))
        if scale == "frequency":
            info["corners"] = {p: corner(res, p) for p in probes if p in vec}
    with open(os.path.join(folder, name + ".json"), "w") as f:
        json.dump(info, f, indent=1)
    return info


def runs(folder):
    """The saved runs in a folder (docs/sim), newest first."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            try:
                with open(os.path.join(folder, n)) as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            base = n[:-5]
            info["files"] = {k: f"{base}.{k}" for k in ("cir", "csv", "svg") if os.path.exists(os.path.join(folder, f"{base}.{k}"))}
            out.append(info)
    return sorted(out, key=lambda x: x.get("at") or "", reverse=True)
