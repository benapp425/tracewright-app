"""Signing a design off before it is ordered. The Sign-off page (Checks > Sign-off) brings together what the user
needs to decide: whether the checks ran on the design as it is now and what they found, each waiver (a waiver
on an error only counts once the user approves it), the stages, what only the built board can show, and how
the run went. Signing off records who, when, the history commit and a fingerprint of the design files
(tracewright.json "signoff"); ordering needs a sign-off whose fingerprint still matches.

Waivers live in tracewright.json checks.waive: {key, reason, by: claude | user, at, severity, message,
approved, approved_at}; Claude writes them with its `waive` tool, the user approves, rejects or adds them."""
import datetime, hashlib, json, os, time

from . import gates


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def design_hash(project):
    """A fingerprint of the design: every schematic sheet, the board and the KiCad project, by content."""
    h = hashlib.sha256()
    files = []
    for d, dirs, fs in os.walk(project.tw.hw):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("backups", "build"))
        files += [os.path.join(d, f) for f in fs if f.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro"))]
    for f in sorted(files):
        h.update(os.path.relpath(f, project.root).encode() + b"\0")
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def current(project):
    """The sign-off with valid: False when the design changed after it (None when there is none)."""
    s = project.cfg.get("signoff")
    if not s:
        return None
    return {**s, "valid": s.get("hash") == design_hash(project)}


# ------------------------------------------------------------------ waivers
def waivers(project):
    out = []
    for x in (project.cfg.get("checks") or {}).get("waive") or []:
        w = dict(x) if isinstance(x, dict) else {"key": str(x)}
        if w.get("key"):
            out.append(w)
    return out


def _save_waivers(project, items):
    project.cfg.setdefault("checks", {})["waive"] = items
    project.save()


def set_waiver(project, key, reason, by, severity="", message=""):
    """Add or replace the waiver for a finding. By the user it holds at once; by Claude, on an error it waits for
    the user's approval."""
    items = [w for w in waivers(project) if w["key"] != key]
    w = {"key": key, "reason": reason.strip(), "by": by, "at": now()}
    if severity:
        w["severity"] = severity
    if message:
        w["message"] = message[:200]
    if by == "user":
        w["approved"] = True
    items.append(w)
    _save_waivers(project, items)
    return w


def approve(project, key):
    items = waivers(project)
    w = next((x for x in items if x["key"] == key), None)
    if not w:
        raise KeyError(f"no waiver for {key}")
    w["approved"], w["approved_at"] = True, now()
    _save_waivers(project, items)
    return w


def remove_waiver(project, key):
    items = waivers(project)
    kept = [x for x in items if x["key"] != key]
    if len(kept) == len(items):
        raise KeyError(f"no waiver for {key}")
    _save_waivers(project, kept)


# ------------------------------------------------------------------ status
def status(project):
    """Everything the Sign-off page shows, and what stands in the way of signing off."""
    res, fresh = gates.checks_fresh(project)
    found = {}
    if res:
        for c in res.get("checks", []):
            for f in c.get("findings", []):
                found[f["key"]] = {**f, "check_title": c.get("title", "")}
    items = []
    for w in waivers(project):
        f = found.get(w["key"])
        sev = (f or {}).get("severity") or w.get("severity") or ""
        state = "proposed" if f and f.get("waiver") else "applies" if (w.get("approved") or w.get("by") == "user" or sev != "error") else "proposed"
        if res and not f and state != "proposed":
            state = "applies"                              # waived in the last run (so not among its findings)
        items.append({**w, "severity": sev, "state": state, "message": (f or {}).get("message") or w.get("message", ""),
                      "check": (f or {}).get("check_title", "")})
    counts = (res or {}).get("counts") or {}
    blockers = []
    if not res:
        blockers.append("The checks have not run yet.")
    elif not fresh:
        blockers.append("The design changed after the checks last ran: run them again.")
    if counts.get("error"):
        n = counts["error"]
        prop = sum(1 for w in items if w["state"] == "proposed")
        blockers.append(f"{n} error{'s' if n != 1 else ''} left" + (f", {prop} with a waiver waiting for your approval" if prop else "") + ".")
    so = current(project)
    return {"checks": {"generated": (res or {}).get("generated"), "fresh": bool(res and fresh), "counts": counts,
                       "waived": (res or {}).get("waived") or {}},
            "waivers": sorted(items, key=lambda w: ({"proposed": 0, "applies": 1}.get(w["state"], 2), w.get("severity") != "error", w["key"])),
            "stages": project.stages(), "signoff": so, "can_sign": not blockers, "blockers": blockers,
            "bringup": os.path.exists(os.path.join(project.root, "docs", "bring-up.md")),
            "report": run_report(project)}


def sign(project, who, note="", commit=None):
    st = status(project)
    if not st["can_sign"]:
        raise ValueError(" ".join(st["blockers"]))
    project.cfg["signoff"] = {"by": who, "at": now(), "hash": design_hash(project), "commit": commit, "note": note.strip()[:400],
                              "counts": st["checks"]["counts"], "waivers": sum(1 for w in st["waivers"] if w["state"] == "applies")}
    project.save()
    return current(project)


def revoke(project):
    project.cfg.pop("signoff", None)
    project.save()


# ------------------------------------------------------------------ how the run went
def _intervals(project):
    """(stage id, start, end) for each stage that ran, from the stage log (else from each stage's last update)."""
    from .projects import STAGES
    ids = [s[0] for s in STAGES]
    log = project.cfg.get("stage_log") or []
    out = {}
    if log:
        for e in log:
            sid, st, t = e.get("s"), e.get("st"), e.get("t", 0)
            if sid not in ids:
                continue
            cur = out.setdefault(sid, [None, None])
            if st == "active" and cur[0] is None:
                cur[0] = t
            if st in ("done", "skipped", "blocked"):
                cur[1] = t
                if cur[0] is None:
                    cur[0] = t
    else:                                                  # older projects: consecutive stage updates
        prev = None
        for sid in ids:
            s = (project.cfg.get("stages") or {}).get(sid) or {}
            if s.get("status") == "done" and s.get("updated"):
                try:
                    end = datetime.datetime.fromisoformat(s["updated"]).timestamp()
                except ValueError:
                    continue
                out[sid] = [prev if prev is not None else end, end]
                prev = end
    return [(sid, a, b) for sid, (a, b) in out.items() if a is not None]


def run_report(project):
    """Per stage: when it ran, how long, Claude's working time, turns and cost; and the totals."""
    from .projects import STAGES
    titles = {s[0]: s[1] for s in STAGES}
    d = os.path.join(project.root, ".tracewright", "sessions")
    turns = []
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        names = []
    from . import costs
    for n in names:
        done = []
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                for line in f:
                    if '"kind": "done"' not in line:
                        continue
                    try:
                        done.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        for r in costs.per_turn(done)[0]:                  # each turn's own cost, not the CLI's running total
            turns.append((r.get("t", 0), (r.get("duration_ms") or 0) / 1000, r.get("cost") or 0.0, r.get("stage")))
    iv = _intervals(project)
    rows = []
    for sid, a, b in iv:
        end = b or time.time()
        mine = [x for x in turns if (x[3] == sid) or (x[3] is None and a - 1 <= x[0] <= end + 1)]
        rows.append({"stage": sid, "title": titles.get(sid, sid), "start": a, "end": b, "wall_s": round(end - a),
                     "claude_s": round(sum(x[1] for x in mine)), "turns": len(mine), "cost": round(sum(x[2] for x in mine), 2)})
    rows.sort(key=lambda r: r["start"])
    return {"stages": rows, "turns": len(turns), "claude_s": round(sum(x[1] for x in turns)), "cost": round(sum(x[2] for x in turns), 2),
            "first": min((x[0] for x in turns), default=None), "last": max((x[0] for x in turns), default=None)}
