"""What one of Claude's turns changed in the project, read from the history checkpoints taken around it (the
commit before the turn and the one after it), and undoing a turn: the files put back as they were before it,
as a new commit, so nothing is lost.

The summary, for the chat's card after each turn:
  {"base", "head", "files": [[status, path]], "board": {"added", "removed", "moved", "tracks", "vias"},
   "schematic": {"added", "removed", "values": [[ref, before, after]]}, "docs": [path], "other": n}"""
import os, re, tempfile

from . import history

DOC_EXT = (".md", ".txt", ".csv")


def head(root):
    """The current commit, or None without history."""
    if not history.available() or not history.has_repo(root):
        return None
    r = history._git(root, "rev-parse", "--short", "HEAD")
    return r.stdout.strip() or None if r.returncode == 0 else None


def _show(root, rev, path):
    return history.show_file(root, rev, path)


def _board_diff(root, rev_a, rev_b, pcb_rel):
    from tw.board import Board
    texts = [_show(root, r, pcb_rel) for r in (rev_a, rev_b)]
    if texts[1] is None:
        return None
    boards = []
    for t in texts:
        if t is None:
            boards.append(None)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False) as f:
            f.write(t)
        try:
            boards.append(Board.load(f.name))
        finally:
            os.remove(f.name)
    a, b = boards
    fa = {f.ref: f for f in a.fp_list} if a else {}
    fb = {f.ref: f for f in b.fp_list}
    moved = [r for r, f in fb.items() if r in fa and (abs(f.x - fa[r].x) > 0.005 or abs(f.y - fa[r].y) > 0.005 or
                                                        abs((f.angle - fa[r].angle) % 360) > 0.01 or f.side != fa[r].side)]
    out = {"added": sorted(set(fb) - set(fa), key=_natural), "removed": sorted(set(fa) - set(fb), key=_natural),
           "moved": sorted(moved, key=_natural),
           "tracks": len(b.tracks) - (len(a.tracks) if a else 0), "vias": len(b.vias) - (len(a.vias) if a else 0)}
    na = {t.net for t in a.tracks} if a else set()
    nb = {t.net for t in b.tracks}
    out["routed"] = sorted(n for n in nb - na if n)
    out["unrouted"] = sorted(n for n in na - nb if n)
    out["outline"] = bool(a) and _outline(a) != _outline(b)
    return out


def _outline(b):
    return [tuple(round(v, 3) for p in l for v in p) for l in (b.outline or [])]


def _natural(ref):
    m = re.match(r"([A-Za-z#_]*)(\d*)", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, ref)


_SYM = re.compile(r'\(property\s+"(Reference|Value)"\s+"((?:[^"\\]|\\.)*)"')


def _symbols(text):
    """{ref: value} of a sheet's placed symbols (power symbols left out)."""
    from tw.sexp import parse, findall, value
    out = {}
    try:
        root = parse(text)
    except Exception:
        return out
    for sym in findall(root, "symbol"):
        props = {}
        for p in findall(sym, "property"):
            if len(p) >= 3:
                props[str(p[1])] = str(p[2])
        ref = props.get("Reference", "")
        if not ref or ref.startswith("#"):
            continue
        out[ref] = props.get("Value", "")
    return out


def _schematic_diff(root, rev_a, rev_b, files):
    sheets = [p for s, p in files if p.endswith(".kicad_sch")]
    if not sheets:
        return None
    before, after = {}, {}
    for p in sheets:
        a, b = _show(root, rev_a, p), _show(root, rev_b, p)
        if a:
            before.update(_symbols(a))
        if b:
            after.update(_symbols(b))
    # a part that moved to another sheet in this turn is neither added nor removed
    return {"added": sorted(set(after) - set(before), key=_natural), "removed": sorted(set(before) - set(after), key=_natural),
            "values": [[r, before[r], after[r]] for r in sorted(set(before) & set(after), key=_natural) if before[r] != after[r]]}


def summary(project, base, after):
    """What changed between the checkpoint before a turn and the one after it (None when nothing did)."""
    root = project.root
    if not base or not after or base == after:
        return None
    r = history._git(root, "diff", "--name-status", "--no-renames", base, after)
    files = [l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l]
    if not files:
        return None
    out = {"base": base, "head": after, "files": files[:200], "nfiles": len(files)}
    pcb = os.path.relpath(project.tw.pcb, root) if project.tw.pcb else None
    if pcb and any(p == pcb for _, p in files):
        try:
            out["board"] = _board_diff(root, base, after, pcb)
        except Exception as e:                            # a board mid-edit that does not parse: say so, do not fail
            out["board"] = {"error": str(e)[:200]}
    try:
        sch = _schematic_diff(root, base, after, files)
        if sch:
            out["schematic"] = sch
    except Exception as e:
        out["schematic"] = {"error": str(e)[:200]}
    out["docs"] = [p for _, p in files if p.startswith("docs/") and p.endswith(DOC_EXT)]
    shown = {pcb} | {p for _, p in files if p.endswith(".kicad_sch")} | set(out["docs"])
    out["other"] = [p for _, p in files if p not in shown][:40]
    return out


def undo(project, base, label=""):
    """The project's files back as they were at base (before the turn), as new commits; returns the new head."""
    return history.restore(project.root, base)
