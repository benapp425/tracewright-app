"""Run the checks and write build/checks.json, build/checks_report.md and build/readiness.md.

Project-specific checks live in design/checks/*.py (same @check decorator) and are loaded too.
The readiness report keeps two lists apart on purpose: what the checks verified from the files,
and what only the built board can show (docs/bring-up.md, tracewright.json hardware_checks).
"""
import os, sys, json, glob, importlib.util, time
from . import selected, run, SEVERITIES, REGISTRY, STOPPED
from .context import Context


_LOADED = {}      # module name -> (mtime, [check ids it registered])
_OWNER = {}       # project check id -> the project root it belongs to (one app process serves many projects)


def load_project_checks(root):
    """Import design/checks/*.py (again, when a file changed since it was loaded)."""
    loaded = []
    for f in sorted(glob.glob(os.path.join(root, "design", "checks", "*.py"))):
        if os.path.basename(f).startswith(("test_", "_")):
            continue
        name = "tw_project_check_" + os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(f)))) + "_" + \
               os.path.splitext(os.path.basename(f))[0]
        mt = os.path.getmtime(f)
        if name in _LOADED and _LOADED[name][0] == mt:
            continue
        if name in _LOADED:                                  # changed: drop what it registered before
            ids = set(_LOADED[name][1])
            REGISTRY[:] = [c for c in REGISTRY if c.id not in ids]
            sys.modules.pop(name, None)
        before = {c.id for c in REGISTRY}
        spec = importlib.util.spec_from_file_location(name, f)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            _LOADED[name] = (mt, [])
            print(f"project check {os.path.basename(f)} failed to load: {e}", file=sys.stderr)
            continue
        _LOADED[name] = (mt, [c.id for c in REGISTRY if c.id not in before])
        for cid in _LOADED[name][1]:
            _OWNER[cid] = os.path.abspath(root)
        loaded.append(f)
    return loaded


def run_all(project=None, only=None, skip=None, refresh=False, offline=None, progress=None, write=True, stop=None):
    """Run the checks and write the reports. `stop` (a threading.Event) ends the run early: the checks
    that finished are recorded and the rest keep their previous result."""
    ctx = Context(project, refresh=refresh, offline=offline)
    if stop is not None:
        ctx.stop = stop
    load_project_checks(ctx.p.root)
    others = [cid for cid, r in _OWNER.items() if r != os.path.abspath(ctx.p.root)]
    checks = selected(only, list(skip or []) + others, ctx.cfg)
    res = run(ctx, checks, progress)
    res["project"] = ctx.p.name
    stopped = ctx.stopped()
    if stopped:
        res["stopped"] = True
    if write:
        os.makedirs(ctx.p.build, exist_ok=True)
        path = os.path.join(ctx.p.build, "checks.json")
        if only or stopped:                               # a partial run updates just the checks it finished
            try:
                with open(path) as f:
                    old = json.load(f)
                if stopped:
                    res["checks"] = [c for c in res["checks"] if c.get("reason") != STOPPED]
                ids = {c["id"] for c in res["checks"]}
                res["checks"] = [c for c in old.get("checks", []) if c["id"] not in ids] + res["checks"]
                res["counts"] = {s: sum(1 for c in res["checks"] for f in c["findings"] if f["severity"] == s)
                                 for s in SEVERITIES}
                res["partial"] = True
            except (OSError, ValueError):
                pass
        with open(path, "w") as f:
            json.dump(res, f, indent=1)
        with open(os.path.join(ctx.p.build, "checks_report.md"), "w") as f:
            f.write(report_md(res))
        with open(os.path.join(ctx.p.build, "readiness.md"), "w") as f:
            f.write(readiness_md(ctx, res))
    return res


def verdict(res):
    c = res["counts"]
    crashed = any(ch["status"] == "error" for ch in res["checks"])
    if crashed or c["error"]:
        return "not ready"
    if c["warning"]:
        return "review warnings"
    return "checks pass"


def report_md(res):
    lines = [f"# {res.get('project', '')} design checks", "",
             f"Generated {res['generated']} by `./tw check` ({res['seconds']} s). Verdict: **{verdict(res)}** "
             f"({res['counts']['error']} errors, {res['counts']['warning']} warnings, {res['counts']['info']} notes).", "",
             "These checks read the generated files (netlist, board, KiCad's ERC/DRC, the plotted sheets). What only "
             "the assembled board can show is listed in `build/readiness.md`.", "",
             "| Check | Group | Result | Findings |", "|---|---|---|---|"]
    for c in res["checks"]:
        lines.append(f"| {c['title']} (`{c['id']}`) | {c['group']} | {c['status'].upper()} | {len(c['findings'])} |")
    for c in res["checks"]:
        if c["findings"]:
            lines += ["", f"## {c['title']}", ""]
            for f in c["findings"][:200]:
                w = f.get("where", {})
                loc = " ".join(f"{k}={w[k]}" for k in ("ref", "net", "sheet") if w.get(k))
                if "x" in w and w["x"] is not None:
                    loc += f" @({w['x']:.2f}, {w['y']:.2f})"
                lines.append(f"- **{f['severity']}** {f['message']}" + (f" -- {loc}" if loc else ""))
    return "\n".join(lines) + "\n"


def readiness_md(ctx, res):
    hw = ctx.setting("hardware_checks", []) or []
    doc = os.path.join(ctx.p.root, "docs", "bring-up.md")
    lines = [f"# {ctx.p.name}: readiness", "", f"Checked {res['generated']}.", "",
             "## Verified from the design files", ""]
    for c in res["checks"]:
        mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "error": "CHECK CRASHED", "skipped": "not run"}[c["status"]]
        if c.get("na"):
            mark = "n/a"
        extra = f" ({len(c['findings'])} findings)" if c["findings"] else ""
        if c["status"] == "skipped":
            extra = f" ({c.get('reason', '')})"
        lines.append(f"- {mark}: {c['title']}{extra}")
    lines += ["", "## Needs the built board (not verifiable from files)", ""]
    if hw:
        lines += [f"- {h}" if isinstance(h, str) else f"- {h.get('what')}: {h.get('how', '')}" for h in hw]
    else:
        lines += ["- Power-up current limit test, rail voltages and ripple under load",
                  "- Every interface enumerates / communicates (USB, I2C, SPI, UART, cameras...)",
                  "- Thermal behavior at worst-case load", "- Mechanical fit: connectors, holes, enclosure"]
    if os.path.exists(doc):
        lines += ["", f"The bring-up procedure is in `docs/bring-up.md`."]
    lines += ["", f"**Verdict: {verdict(res)}.** A board is ready only when every check passes (or each warning is "
              "reviewed and waived with a reason) and the hardware list above has a test plan."]
    return "\n".join(lines) + "\n"


def summary_text(res, limit=25):
    """Short text for the agent / terminal."""
    lines = [f"{verdict(res).upper()}: {res['counts']['error']} errors, {res['counts']['warning']} warnings, "
             f"{res['counts']['info']} notes ({res['seconds']} s)" + (" -- STOPPED before the end" if res.get("stopped") else "")]
    for c in res["checks"]:
        n = len(c["findings"])
        tag = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "error": "CRSH", "skipped": "skip"}[c["status"]]
        if c.get("na"):
            tag = "n/a "
        lines.append(f"  {tag} {c['id']:<22} {n:>3}  {c['title']}" + (f" ({c.get('reason')})" if c["status"] == "skipped" else ""))
    shown = 0
    for sev in ("error", "warning"):
        for c in res["checks"]:
            for f in c["findings"]:
                if f["severity"] != sev or shown >= limit:
                    continue
                w = f.get("where", {})
                loc = w.get("ref") or w.get("net") or w.get("sheet") or ""
                lines.append(f"  - [{sev}] {f['message']}" + (f"  ({loc})" if loc and loc not in f["message"] else ""))
                shown += 1
    total = res["counts"]["error"] + res["counts"]["warning"]
    if total > shown:
        lines.append(f"  ... {total - shown} more in build/checks_report.md")
    return "\n".join(lines)
