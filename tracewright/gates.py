"""Stage gates: what has to be true before a design stage counts as done. Claude's `stage` tool asks here when it
marks a stage done (or moves on past it); a gate that fails keeps the stage open and says what is missing.
The user can still mark a stage done by hand from the stage list.

  brief         docs/requirements.md written (more than the template's headings)
  architecture  docs/architecture.md written
  schematic     the schematic exists; the last checks ran after its last change with no schematic errors
  board_setup   the board exists with a closed outline
  placement     every part on the board, no courtyards overlapping (read from the board itself)
  routing       the last DRC is newer than the board and leaves nothing unconnected
  verification  the checks ran after the last design change, with no errors left (an error waived only counts
                once the user approved the waiver)
  release       the user signed the design off, and it has not changed since
"""
import json, os, re

SCH_GROUPS = ("Schematic",)


def _text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _written(path, min_words=60):
    """A doc with real content: more words than its headings and template placeholders."""
    t = _text(path)
    body = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    body = re.sub(r"<[^>]*>|\(to be written\)|TBD|TODO", " ", body, flags=re.I)
    return len(re.findall(r"[A-Za-z0-9]{2,}", body)) >= min_words


def _checks(project):
    try:
        with open(os.path.join(project.tw.build, "checks.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def design_mtime(project):
    """When the design last changed: its schematic sheets, board and KiCad project."""
    ts = []
    for d, _, files in os.walk(project.tw.hw):
        if "/." in d or os.sep + "backups" in d:
            continue
        for f in files:
            if f.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro")):
                try:
                    ts.append(os.path.getmtime(os.path.join(d, f)))
                except OSError:
                    pass
    return max(ts) if ts else 0


def checks_fresh(project, res=None):
    """The last checks' results, and whether they ran after the design last changed."""
    res = res if res is not None else _checks(project)
    if not res:
        return None, False
    try:
        import datetime
        gen = datetime.datetime.fromisoformat(str(res.get("generated"))).timestamp()
    except (TypeError, ValueError):
        gen = 0
    return res, gen + 2 >= design_mtime(project)


def gate(project, stage):
    """(ok, [what is missing]) for marking the stage done."""
    p, miss = project, []
    root = p.root
    if stage == "brief":
        if not _written(os.path.join(root, "docs", "requirements.md")):
            miss.append("docs/requirements.md is not written yet (the agreed requirements and the assumptions)")
    elif stage == "architecture":
        if not _written(os.path.join(root, "docs", "architecture.md"), 40):
            miss.append("docs/architecture.md is not written yet (blocks, power tree with currents, interfaces, key parts)")
    elif stage == "schematic":
        if not p.tw.has_sch():
            miss.append("there is no schematic yet")
        else:
            res, fresh = checks_fresh(p)
            if not res or not fresh:
                miss.append("run the checks (run_checks): the schematic changed after the last run")
            else:
                bad = [c for c in res.get("checks", []) if c.get("group") in SCH_GROUPS and c.get("status") in ("fail", "error")]
                if bad:
                    miss.append("schematic checks with errors: " + ", ".join(c.get("id", "?") for c in bad[:6]))
    elif stage == "board_setup":
        if not p.tw.has_pcb():
            miss.append("there is no board yet (sync_board)")
        else:
            from tw.board import Board
            if not Board.load(p.tw.pcb).outline:
                miss.append("the board has no closed outline on Edge.Cuts")
    elif stage == "placement":
        if not p.tw.has_pcb():
            miss.append("there is no board yet")
        else:
            from tw.checks import runner
            res = runner.run_all(p.tw, only=["pcb.placement"], offline=True, write=False)
            for c in res.get("checks", []):
                errs = [f["message"] for f in c.get("findings", []) if f.get("severity") == "error"]
                if errs:
                    miss.append(f"{len(errs)} placement error{'s' if len(errs) != 1 else ''}: " + "; ".join(errs[:4]))
    elif stage == "routing":
        drc = os.path.join(p.tw.build, "drc.json")
        try:
            stale = os.path.getmtime(drc) < os.path.getmtime(p.tw.pcb) - 2
            d = json.loads(_text(drc) or "{}")
        except OSError:
            stale, d = True, {}
        if stale:
            miss.append("run the checks (or ./tw drc): the board changed after the last DRC")
        else:
            n = len(d.get("unconnected_items", []))
            if n:
                miss.append(f"{n} connection{'s' if n != 1 else ''} still unrouted")
    elif stage == "verification":
        res, fresh = checks_fresh(p)
        if not res:
            miss.append("the checks have not run")
        elif not fresh:
            miss.append("run the checks again: the design changed after the last run")
        else:
            n = (res.get("counts") or {}).get("error", 0)
            if n:
                proposed = sum(1 for c in res.get("checks", []) for f in c.get("findings", []) if f.get("waiver"))
                miss.append(f"{n} error{'s' if n != 1 else ''} left" + (f" ({proposed} with a waiver waiting for the user's approval)" if proposed else ""))
    elif stage == "release":
        from . import signoff
        s = signoff.current(p)
        if not s:
            miss.append("the user has not signed the design off (Checks > Sign-off)")
        elif not s.get("valid"):
            miss.append("the design changed after the user signed it off: they need to sign it off again")
    return (not miss), miss
