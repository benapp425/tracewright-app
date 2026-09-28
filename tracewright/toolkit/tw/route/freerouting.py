"""Freerouting (Specctra DSN / SES) as the whole-board autorouter.

KiCad exports the board as DSN (existing copper included, and kept), Freerouting routes it
headless, and the session (SES) is imported back. Freerouting runs with its GUI, API server and
usage analytics switched off (it reports usage by default). Needs Java 17+ and the jar: set
TW_FREEROUTING, or put freerouting-*.jar in ~/.cache/tracewright/ (the Tracewright app installs it).
"""
import os, re, glob, json, shutil, subprocess, time
from .. import env, kicad

KPY_EXPORT = r'''
import sys, os, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
# Rule areas that restrict nothing (they only name a region for custom DRC rules, like the
# router's "TW neck" areas) are exported as hard keepouts and stall Freerouting: leave them out.
grave = []
for z in list(b.Zones()):
    if z.GetIsRuleArea() and not (z.GetDoNotAllowTracks() or z.GetDoNotAllowVias() or z.GetDoNotAllowPads()
                                  or z.GetDoNotAllowFootprints() or z.GetDoNotAllowZoneFills()):
        b.Remove(z)
        grave.append(z)
ok = pcbnew.ExportSpecctraDSN(b, sys.argv[2])
print("TWJSON {\"ok\": %s, \"dropped_rule_areas\": %d}" % ("true" if ok else "false", len(grave)))
sys.stdout.flush()
os._exit(0)
'''

KPY_IMPORT = r'''
import sys, os, json, pcbnew
path, ses = sys.argv[1], sys.argv[2]
pro = os.path.splitext(path)[0] + ".kicad_pro"
keep = open(pro, "rb").read() if os.path.exists(pro) else None
b = pcbnew.LoadBoard(path)
ok = pcbnew.ImportSpecctraSES(b, ses)
if ok:
    b.BuildConnectivity()
    zones = [z for z in b.Zones()]
    if zones:
        pcbnew.ZONE_FILLER(b).Fill(zones)
    pcbnew.SaveBoard(path, b)
    if keep is not None and open(pro, "rb").read() != keep:
        open(pro, "wb").write(keep)
print("TWJSON " + json.dumps({"ok": bool(ok), "tracks": len([t for t in b.GetTracks()])}))
sys.stdout.flush()
os._exit(0)
'''


def find_jar():
    j = os.environ.get("TW_FREEROUTING")
    if j and os.path.exists(j):
        return j
    cands = []
    for d in (os.path.expanduser("~/.cache/tracewright"), os.path.expanduser("~/Library/Application Support/Tracewright/vendor"),
              os.path.join(os.environ.get("APPDATA", ""), "Tracewright", "vendor"),
              os.path.expanduser("~/.local/share/tracewright/vendor")):
        cands += glob.glob(os.path.join(d, "freerouting*.jar"))
    return sorted(cands)[-1] if cands else None


def java():
    j = shutil.which("java")
    if not j:
        return None, "Java is not installed (Freerouting needs Java 17 or newer)"
    try:
        out = subprocess.run([j, "-version"], capture_output=True, text=True, timeout=30)
        m = re.search(r'version "(\d+)', out.stderr + out.stdout)
        if m and int(m.group(1)) < 17:
            return None, f"Java {m.group(1)} is too old for Freerouting (needs 17+)"
    except Exception:
        pass
    return j, None


def _kpy_inline(code, *args, timeout=900):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        rc, out, err = kicad.kpy(path, *args, timeout=timeout, check=False)
    finally:
        os.unlink(path)
    return kicad.last_json(out) or {"ok": False, "error": (err or out)[-1500:]}


STALL_PASSES = 50          # Freerouting 2.1 ignores its pass limit and retries what it cannot route until pass 999


def route(project=None, max_passes=30, threads=None, on_progress=None, log=print, timeout=1200):
    project = project or env.project()
    jar = find_jar()
    if not jar:
        return {"summary": {"ok": False, "error": "Freerouting is not installed (set TW_FREEROUTING or install it from "
                                                   "the Tracewright settings)"}}
    jv, why = java()
    if not jv:
        return {"summary": {"ok": False, "error": why}}
    work = project.out("route")
    dsn, ses = os.path.join(work, "board.dsn"), os.path.join(work, "board.ses")
    for f in (dsn, ses):
        if os.path.exists(f):
            os.remove(f)
    t0 = time.time()
    r = _kpy_inline(KPY_EXPORT, project.pcb, dsn)
    if not r.get("ok") or not os.path.exists(dsn):
        return {"summary": {"ok": False, "error": f"DSN export failed: {r.get('error', '')}"}}
    threads = threads or max(1, (os.cpu_count() or 2) - 1)
    cmd = [jv, "-jar", jar, "-de", dsn, "-do", ses, "-mp", str(max_passes), "-mt", str(threads), "-da", "-dct", "0",
           "--gui.enabled=false", "--api_server.enabled=false", "--usage_and_diagnostic_data.disable_analytics=true"]
    log("  freerouting: " + " ".join(os.path.basename(c) if os.sep in c else c for c in cmd[1:]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=work)
    import threading
    why = []
    watchdog = threading.Timer(timeout, lambda: (why.append(f"no result after {timeout // 60:.0f} min"), proc.kill()))
    watchdog.daemon = True
    watchdog.start()
    lines = []
    best, best_pass = None, 0
    try:
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            m = re.search(r"pass #?(\d+).*?(\d+) unrouted", line, re.I) or re.search(r"(\d+) unrouted", line, re.I)
            if m and on_progress:
                ev = {"status": "progress", "engine": "freerouting", "line": line[-200:]}
                if len(m.groups()) == 2:
                    ev["pass"], ev["unrouted"] = int(m.group(1)), int(m.group(2))
                on_progress(ev)
            if m and len(m.groups()) == 2:
                n, left = int(m.group(1)), int(m.group(2))
                if best is None or left < best:
                    best, best_pass = left, n
                elif left and n - best_pass >= STALL_PASSES:   # the same connections again and again: stop
                    why.append(f"no progress in {STALL_PASSES} passes with {best} connections it cannot route")
                    proc.kill()
                    break
        proc.wait(timeout=60)
    except Exception:
        proc.kill()
    finally:
        watchdog.cancel()
    with open(os.path.join(work, "freerouting.log"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if why or not os.path.exists(ses):
        return {"summary": {"ok": False, "engine": "freerouting", "seconds": round(time.time() - t0, 1),
                            "error": f"Freerouting stopped: {why[0]}; the board is unchanged" if why else
                                     "Freerouting produced no session file; the board is unchanged", "log": lines[-12:]}}
    r = _kpy_inline(KPY_IMPORT, project.pcb, ses)
    summary = {"ok": bool(r.get("ok")), "engine": "freerouting", "seconds": round(time.time() - t0, 1),
               "tracks": r.get("tracks"), "log": lines[-8:]}
    if on_progress:
        on_progress({"status": "done", "engine": "freerouting"})
    return {"summary": summary}
