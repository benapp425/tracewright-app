"""What needs the user's OK after one of Claude's runs: a short list at the end of the run, never a question in the
middle of it. Read from the history checkpoints before and after the run (like turns.py).

Gone ahead, confirm later (in the design already; Keep, or Undo: Claude puts it back and redoes what hung on it):
  parts       a part's value, MPN, LCSC number or footprint changed once the parts were agreed (a stand-in too)
  floorplan   the outline, a mounting hole or a connector moved once the floorplan was agreed
  handmade    the user's own work changed: a part they placed or locked, a track, via or pour they drew
  rules       design rules made looser than before the run (a clearance, a track width, a via)
Asked, kept as agreed meanwhile (put back at the end of the run; Approve applies it):
  limits      the agreed limits (board size, layers, height ...) changed
After sign-off, every change (signed).

Each kind can be turned off in Settings (approve_parts, approve_floorplan ...). .tracewright/approvals.json:
  {"next": n, "items": [{id, kind, group: confirm | ask, title, detail, turn, base, head, at, status: pending | kept |
   undone | approved | declined, proposed?, agreed?}]}"""
import json, os, re, tempfile, threading, time

from . import history

KINDS = {"parts": "confirm", "floorplan": "confirm", "handmade": "confirm", "rules": "confirm", "limits": "ask", "signed": "confirm"}
DEFAULTS = {"parts": True, "floorplan": True, "handmade": True, "rules": True, "limits": True, "signed": True}
LABELS = {"parts": "Part swaps once the parts are agreed", "floorplan": "The outline, holes or connectors moved once the floorplan is agreed",
          "handmade": "Changes to what you made by hand (parts you placed or locked, copper you drew)",
          "rules": "Design rules made looser", "limits": "Changes to the agreed limits (kept as agreed until you approve)",
          "signed": "Every change after you sign off"}
CONNECTOR = re.compile(r"^(J|P|CN|CON|USB|X)\d", re.I)
HOLE = re.compile(r"^(H|MH|MK)\d", re.I)
_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def kinds_on(settings):
    """Which kinds go on the list: the app's settings (approve_parts, approve_rules ... true or false) over the
    defaults."""
    s = dict(DEFAULTS)
    for k in DEFAULTS:
        v = (settings or {}).get(f"approve_{k}")
        if v is not None:
            s[k] = bool(v)
    return s


class Approvals:
    def __init__(self, project):
        self.p = project
        self.path = os.path.join(project.state_dir(), "approvals.json")

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            d.setdefault("items", [])
            d.setdefault("next", 1 + len(d["items"]))
            return d
        except (OSError, ValueError):
            return {"next": 1, "items": []}

    def _save(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def items(self, status=None):
        it = self._load()["items"]
        return [x for x in it if not status or x["status"] == status]

    def pending(self):
        return self.items("pending")

    def get(self, aid):
        for x in self._load()["items"]:
            if x["id"] == aid:
                return x
        raise KeyError(aid)

    def add(self, items):
        if not items:
            return []
        with _lock:
            d = self._load()
            out = []
            for it in items:
                it = dict(it, id=f"A{d['next']}", status="pending", at=_now(), group=KINDS.get(it["kind"], "confirm"))
                d["next"] += 1
                d["items"].append(it)
                out.append(it)
            d["items"] = d["items"][-300:]
            self._save(d)
        return out

    def set(self, aid, status):
        with _lock:
            d = self._load()
            for x in d["items"]:
                if x["id"] == aid:
                    x["status"] = status
                    x["decided"] = _now()
                    self._save(d)
                    return x
        raise KeyError(aid)


# ------------------------------------------------------------------ reading a run
def _cfg_at(root, rev):
    t = history.show_file(root, rev, "tracewright.json")
    try:
        return json.loads(t) if t else {}
    except ValueError:
        return {}


def _board_at(root, rev, rel):
    from tw.board import Board
    t = history.show_file(root, rev, rel)
    if not t:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False) as f:
        f.write(t)
    try:
        return Board.load(f.name)
    except Exception:
        return None
    finally:
        os.remove(f.name)


_FIELDS = ("Value", "MPN", "LCSC", "Footprint")


def _parts_at(root, rev, sheets):
    """{ref: {Value, MPN, LCSC, Footprint}} of the placed symbols in the given sheets at a checkpoint."""
    from tw.sexp import parse, findall
    out = {}
    for p in sheets:
        t = history.show_file(root, rev, p)
        if not t:
            continue
        try:
            root_node = parse(t)
        except Exception:
            continue
        for sym in findall(root_node, "symbol"):
            props = {str(q[1]): str(q[2]) for q in findall(sym, "property") if len(q) >= 3}
            ref = props.get("Reference", "")
            if ref and not ref.startswith("#"):
                out[ref] = {k: props.get(k, "") for k in _FIELDS}
    return out


def _pro_at(root, rev, rel):
    from tw.pro import ProjectSettings
    t = history.show_file(root, rev, rel)
    if not t:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".kicad_pro", delete=False) as f:
        f.write(t)
    try:
        return ProjectSettings.load(f.name)
    except Exception:
        return None
    finally:
        os.remove(f.name)


def _moved(a, b):
    return abs(a.x - b.x) > 0.005 or abs(a.y - b.y) > 0.005 or abs((a.angle - b.angle) % 360) > 0.01 or a.side != b.side


def _outline(b):
    return [tuple(round(v, 3) for p in l for v in p) for l in (b.outline or [])]


def review_run(project, base, head, summary=None, on=None, handmade=None):
    """The items one run calls for (not saved): [{kind, title, detail, ...}]."""
    on = on or dict(DEFAULTS)
    root = project.root
    if not base or not head or base == head:
        return []
    r = history._git(root, "diff", "--name-status", "--no-renames", base, head)
    files = [l.split("\t", 1)[1] for l in r.stdout.splitlines() if "\t" in l]
    if not files:
        return []
    cfg_a = _cfg_at(root, base)
    cfg_b = project.cfg
    items = []
    stages = cfg_a.get("stages") or {}
    parts_agreed = (stages.get("parts") or {}).get("status") == "done"
    floor_agreed = (cfg_a.get("start") or {}).get("phase") == "done" or (stages.get("placement") or {}).get("status") == "done"
    pcb = os.path.relpath(project.tw.pcb, root) if project.tw.pcb else None
    # parts swapped once agreed
    sheets = [p for p in files if p.endswith(".kicad_sch")]
    if on.get("parts") and parts_agreed and sheets:
        pa, pb = _parts_at(root, base, sheets), _parts_at(root, head, sheets)
        for ref in sorted(set(pa) & set(pb), key=_natural):
            ch = [(k, pa[ref][k], pb[ref][k]) for k in _FIELDS if pa[ref][k] != pb[ref][k]]
            if not ch:
                continue
            title = f"{ref}: " + "; ".join(f"{k.lower() if k != 'MPN' and k != 'LCSC' else k} {a or '(none)'} -> {b or '(none)'}" for k, a, b in ch)
            items.append({"kind": "parts", "title": title, "ref": ref, "before": pa[ref], "after": pb[ref],
                          "detail": "A part you had agreed was changed."})
    # the board
    if pcb and pcb in files:
        ba, bb = _board_at(root, base, pcb), _board_at(root, head, pcb)
        if ba is not None and bb is not None:
            fa = {f.ref: f for f in ba.fp_list}
            fb = {f.ref: f for f in bb.fp_list}
            if on.get("floorplan") and floor_agreed:
                if _outline(ba) and _outline(ba) != _outline(bb):
                    wa, ha = ba.size()
                    wb, hb = bb.size()
                    items.append({"kind": "floorplan", "title": f"The board outline changed ({wa:.1f} × {ha:.1f} -> {wb:.1f} × {hb:.1f} mm)",
                                  "detail": "The outline was agreed with the floorplan."})
                for ref in sorted(set(fa) & set(fb), key=_natural):
                    if (CONNECTOR.match(ref) or HOLE.match(ref)) and _moved(fa[ref], fb[ref]):
                        what = "connector" if CONNECTOR.match(ref) else "mounting hole"
                        items.append({"kind": "floorplan", "title": f"{ref} ({what}) moved", "ref": ref,
                                      "from": [round(fa[ref].x, 3), round(fa[ref].y, 3), round(fa[ref].angle, 1), fa[ref].side],
                                      "to": [round(fb[ref].x, 3), round(fb[ref].y, 3), round(fb[ref].angle, 1), fb[ref].side],
                                      "detail": f"Its place was agreed with the floorplan: ({fa[ref].x:.2f}, {fa[ref].y:.2f}) -> ({fb[ref].x:.2f}, {fb[ref].y:.2f})."})
            if on.get("handmade"):
                hm = handmade or {}
                mine = set((hm.get("footprints") or {})) | {r for r, f in fa.items() if f.locked}
                for ref in sorted(mine & set(fa) & set(fb), key=_natural):
                    if _moved(fa[ref], fb[ref]) and not any(x.get("ref") == ref for x in items):
                        how = "you locked it" if fa[ref].locked else "you placed it"
                        items.append({"kind": "handmade", "title": f"{ref} moved ({how})", "ref": ref,
                                      "from": [round(fa[ref].x, 3), round(fa[ref].y, 3), round(fa[ref].angle, 1), fa[ref].side],
                                      "detail": f"({fa[ref].x:.2f}, {fa[ref].y:.2f}) -> ({fb[ref].x:.2f}, {fb[ref].y:.2f})."})
                ta = {t.uuid: t for t in ba.tracks}
                tb = {t.uuid: t for t in bb.tracks}
                gone = [ta[u] for u in hm.get("tracks") or [] if u in ta and u not in tb]
                changed = [ta[u] for u in hm.get("tracks") or [] if u in ta and u in tb and
                           (tuple(ta[u].a) != tuple(tb[u].a) or tuple(ta[u].b) != tuple(tb[u].b) or ta[u].w != tb[u].w or ta[u].layer != tb[u].layer)]
                if gone or changed:
                    nets = sorted({t.net.rsplit("/", 1)[-1] for t in gone + changed if t.net})
                    items.append({"kind": "handmade", "title": f"Tracks you drew ({', '.join(nets[:5])}) were {'removed' if gone and not changed else 'changed'}",
                                  "detail": f"{len(gone)} removed, {len(changed)} changed."})
                za = {z.uuid: z for z in ba.zones}
                zb = {z.uuid: z for z in bb.zones}
                zg = [za[u] for u in hm.get("zones") or [] if u in za and (u not in zb or za[u].outline != zb[u].outline or za[u].net != zb[u].net)]
                if zg:
                    items.append({"kind": "handmade", "title": f"{'A pour' if len(zg) == 1 else str(len(zg)) + ' pours'} you drew changed",
                                  "detail": ", ".join(sorted({(z.net or 'keep-out').rsplit('/', 1)[-1] for z in zg}))})
    # rules made looser
    pro = os.path.relpath(project.tw.pro, root) if project.tw.pro else None
    if on.get("rules") and pro and pro in files:
        ra, rb = _pro_at(root, base, pro), _pro_at(root, head, pro)
        if ra is not None and rb is not None:
            for name in sorted(set(ra.classes) & set(rb.classes) | {"Default"}):
                ca, cb = ra.cls(name), rb.cls(name)
                for k, label in (("clearance", "clearance"), ("track_width", "track width"), ("via_diameter", "via diameter"), ("via_drill", "via drill")):
                    a, b = ca.get(k), cb.get(k)
                    if a and b and float(b) < float(a) - 1e-6:
                        items.append({"kind": "rules", "title": f"{name} class {label} {float(a):g} -> {float(b):g} mm", "class": name, "key": k,
                                      "before": float(a), "after": float(b), "detail": "A looser rule than before this run."})
            for k, label in (("min_clearance", "minimum clearance"), ("min_track_width", "minimum track width"), ("min_via_diameter", "minimum via"),
                             ("min_copper_edge_clearance", "copper to edge"), ("min_hole_clearance", "hole clearance")):
                a, b = (ra.rules or {}).get(k), (rb.rules or {}).get(k)
                if a and b is not None and float(b) < float(a) - 1e-6:
                    items.append({"kind": "rules", "title": f"Board {label} {float(a):g} -> {float(b):g} mm", "key": k, "before": float(a), "after": float(b),
                                  "detail": "A looser rule than before this run."})
    # limits: asked, kept as agreed
    la, lb = cfg_a.get("constraints") or {}, cfg_b.get("constraints") or {}
    if on.get("limits") and la and la != lb:
        from tw import constraints as cons
        diff = sorted(set(la) | set(lb))
        changed = [k for k in diff if la.get(k) != lb.get(k)]
        if changed:
            def show(v):
                return "not set" if v is None else " × ".join(f"{x:g}" for x in v) if isinstance(v, list) else f"{v:g}" if isinstance(v, (int, float)) else str(v)
            names = {k: (cons.OPTIONS.get(k) or (k,))[0].lower() for k in changed}
            items.append({"kind": "limits", "title": "Change the agreed limits: " + "; ".join(f"{names[k]} {show(la.get(k))} -> {show(lb.get(k))}" for k in changed),
                          "agreed": la, "proposed": lb, "detail": "Kept as agreed until you approve."})
    # signed off before the run: everything it changed
    if on.get("signed") and cfg_a.get("signoff") and not any(i["kind"] != "limits" for i in items):
        what = []
        if summary:
            bd = summary.get("board") or {}
            if bd.get("moved"):
                what.append(f"moved {', '.join(bd['moved'][:6])}")
            if bd.get("tracks"):
                what.append(f"{abs(bd['tracks'])} track segments {'added' if bd['tracks'] > 0 else 'removed'}")
            sch = summary.get("schematic") or {}
            if sch.get("values"):
                what.append("values " + ", ".join(f"{r} {a}->{b}" for r, a, b in sch["values"][:4]))
            if summary.get("docs"):
                what.append(f"{len(summary['docs'])} documents")
        items.append({"kind": "signed", "title": "The design changed after you signed off",
                      "detail": ("; ".join(what) or f"{len(files)} files") + ". Sign again once you have checked it."})
    return items


def hold(project, items):
    """What is asked, not done: the agreed limits put back while the user decides."""
    for it in items:
        if it["kind"] == "limits" and it.get("agreed") is not None:
            project.reload()
            project.cfg["constraints"] = it["agreed"]
            project.save()


def _natural(ref):
    m = re.match(r"([A-Za-z#_]*)(\d*)", ref)
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, ref)


# ------------------------------------------------------------------ deciding
def undo_text(it):
    """The message that asks Claude to put an item back (the follow-up run)."""
    k = it["kind"]
    if k == "parts":
        b = it.get("before") or {}
        return (f"Undo: {it['title']}. Put {it.get('ref')} back to the agreed part ({', '.join(f'{x} {b.get(x)}' for x in _FIELDS if b.get(x))}) "
                "and redo whatever depended on the change. Keep the rest of your work.")
    if k in ("floorplan", "handmade") and it.get("ref") and it.get("from"):
        x, y, rot, side = it["from"]
        return (f"Undo: {it['title']}. Put {it['ref']} back at ({x}, {y}), {rot}°, {'bottom' if side == 'B' else 'top'} -- "
                "where the user had it -- and fix what that affects (tracks, clearances). Keep the rest of your work.")
    if k == "rules":
        return (f"Undo: {it['title']}. Put the rule back to {it.get('before')} mm and make the board meet it (reroute or move "
                "what needs it). Keep the rest of your work.")
    if k == "signed":
        return "Undo the changes you made after the user signed off, putting the design back as it was signed. Then stop."
    return f"Undo: {it['title']}. {it.get('detail', '')} Put it back as it was agreed and keep the rest of your work."


def decided_line(it, status):
    """One line for Claude's next turn: what the user decided."""
    word = {"kept": "kept", "approved": "approved", "declined": "declined (keep to the agreed one)", "undone": "asked to undo"}[status]
    return f"the user {word}: {it['title']}"
