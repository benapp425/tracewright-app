"""Design checks: independent readers of the generated files, each tested with planted faults.

A check is a function `fn(ctx) -> [Finding]` registered with @check. It reads only what KiCad and
the fab outputs say (netlist, board file, ERC / DRC reports, plotted SVGs), never the scripts that
produced the design, so a mistake in those scripts shows up. An empty list is a pass; a check that
crashes is reported as an error, never as a pass.

Severity:
  error     the board would be wrong or unbuildable (or a check could not verify what it must)
  warning   probably wrong, or a documented trap; a person should look
  info      worth knowing; never blocks a release

A check that has nothing to look at on this board (no USB-C receptacle, no relays) raises
NotApplicable(reason): it is reported as skipped with the reason, never as a pass. A check that
cannot tell whether the board is right (no currents declared, a lookup that did not answer) says so
with a warning or an error, again never a silent pass. Each check runs with a time limit
(@check(timeout=...), tracewright.json checks.timeout_s) and a run can be stopped between checks.
"""
import os, json, time, traceback, fnmatch, threading

REGISTRY = []
SEVERITIES = ("error", "warning", "info")


class Finding:
    __slots__ = ("check", "severity", "message", "where", "hint", "key")

    def __init__(self, check, severity, message, where=None, hint=None, key=None):
        assert severity in SEVERITIES, severity
        self.check, self.severity, self.message = check, severity, message
        self.where = where or {}
        self.hint = hint
        self.key = key or f"{check}:{message}"

    def to_json(self):
        d = {"check": self.check, "severity": self.severity, "message": self.message, "key": self.key}
        if self.where:
            d["where"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.where.items()}
        if self.hint:
            d["hint"] = self.hint
        return d

    def __repr__(self):
        return f"[{self.severity} {self.check}] {self.message}"


class NotApplicable(Exception):
    """Raised by a check that has nothing to look at on this board; the reason is shown."""


class Stopped(Exception):
    """The run was stopped (ctx.stop set) while a check was working."""


DEFAULT_TIMEOUT = 240.0
STOPPED = "the run was stopped"


class Check:
    def __init__(self, id, title, group, fn, needs, doc, default=True, timeout=None):
        self.id, self.title, self.group, self.fn, self.needs = id, title, group, fn, tuple(needs)
        self.doc = doc
        self.default = default
        self.timeout = timeout


def check(id, title, group, needs=(), default=True, timeout=None):
    def deco(fn):
        REGISTRY[:] = [c for c in REGISTRY if c.id != id]           # a reloaded module replaces its checks
        REGISTRY.append(Check(id, title, group, fn, needs, (fn.__doc__ or "").strip(), default, timeout))
        return fn
    return deco


GROUPS = ["KiCad", "Schematic", "Parts & BOM", "Placement", "Routing", "Power", "High-speed", "Manufacturing",
          "Assembly", "Lessons"]


def load_all():
    """Import every check module (registration happens on import)."""
    from . import kicad_reports, schematic_checks, bom, placement, routing, power, signal, dfm, lessons, cpl, assembly  # noqa: F401
    from . import integrity, power_layout, si_layout, layout_quality  # noqa: F401
    return REGISTRY


def selected(only=None, skip=None, cfg=None):
    load_all()
    disabled = set((cfg or {}).get("checks", {}).get("disabled", []))
    out = []
    for c in REGISTRY:
        if only and not any(fnmatch.fnmatch(c.id, p) or c.group == p for p in only):
            continue
        if skip and any(fnmatch.fnmatch(c.id, p) for p in skip):
            continue
        if c.id in disabled:
            continue
        if not only and not c.default:
            continue
        out.append(c)
    return out


def _call(c, ctx, limit):
    """c.fn(ctx) on a worker thread, waiting at most `limit` seconds (a hung check must not hang the
    run: its thread is abandoned and the check reported as timed out). Returns (findings, error)."""
    box = {}

    def work():
        try:
            box["found"] = c.fn(ctx) or []
        except BaseException as e:                                  # noqa: B902 - reported, never swallowed
            box["error"] = e
            box["tb"] = traceback.format_exc()
    t = threading.Thread(target=work, name=f"check-{c.id}", daemon=True)
    t.start()
    end = time.time() + limit
    while t.is_alive() and time.time() < end and not ctx.stopped():
        t.join(0.2)
    if t.is_alive():
        ctx.abandoned.append(c.id)
        if ctx.stopped():
            return None, Stopped()
        return None, TimeoutError(f"no answer after {limit:.0f} s")
    if "error" in box:
        e = box["error"]
        e.tb = box.get("tb", "")
        return None, e
    return box["found"], None


def run(ctx, checks, progress=None):
    """Run checks; returns the result dict written to build/checks.json. progress(check, result) is
    called when each check starts (status "running") and when it ends."""
    results = []
    t_all = time.time()
    base = float(ctx.setting("checks.timeout_s", 0) or 0)
    for c in checks:
        t0 = time.time()
        row = {"id": c.id, "title": c.title, "group": c.group}
        if ctx.stopped():
            results.append({**row, "status": "skipped", "reason": STOPPED, "findings": [], "seconds": 0.0})
            if progress:
                progress(c, results[-1])
            continue
        missing = [n for n in c.needs if not ctx.available(n)]
        if missing:
            results.append({**row, "status": "skipped", "reason": "needs " + ", ".join(missing), "findings": [],
                            "seconds": 0.0})
            if progress:
                progress(c, results[-1])
            continue
        if progress:
            progress(c, {**row, "status": "running", "findings": []})
        limit = max(base, c.timeout or DEFAULT_TIMEOUT)
        found, err = _call(c, ctx, limit)
        secs = round(time.time() - t0, 2)
        if isinstance(err, NotApplicable):
            results.append({**row, "status": "skipped", "reason": str(err) or "not applicable", "findings": [],
                            "seconds": secs, "na": True})
        elif isinstance(err, Stopped) or (err is not None and ctx.stopped()):
            results.append({**row, "status": "skipped", "reason": STOPPED, "findings": [], "seconds": secs})
        elif isinstance(err, TimeoutError):                              # never a pass
            results.append({**row, "status": "error", "seconds": secs,
                            "findings": [Finding(c.id, "error", f"check timed out ({err}); its result is unknown",
                                                 hint="Run it again on its own (Run this check). If it keeps timing out, "
                                                      "tell Tracewright's developer which board and check.",
                                                 key=f"{c.id}:timeout").to_json()]})
        elif err is not None:                                           # a broken check is never a pass
            results.append({**row, "status": "error", "seconds": secs,
                            "findings": [Finding(c.id, "error", f"check crashed: {type(err).__name__}: {err}",
                                                 hint=getattr(err, "tb", "")[-1200:]).to_json()]})
        else:
            waived = ctx.waivers()
            kept, waived_n = [], 0
            for f in found:
                if f.key in waived:
                    waived_n += 1
                else:
                    kept.append(f)
            status = "fail" if any(f.severity == "error" for f in kept) else \
                ("warn" if any(f.severity == "warning" for f in kept) else "pass")
            results.append({**row, "status": status, "findings": [f.to_json() for f in kept], "waived": waived_n,
                            "seconds": secs})
        if progress:
            progress(c, results[-1])
    counts = {s: 0 for s in SEVERITIES}
    for r in results:
        for f in r["findings"]:
            counts[f["severity"]] += 1
    return {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t_all, 1),
            "counts": counts, "checks": results, "inputs": ctx.inputs_summary()}
