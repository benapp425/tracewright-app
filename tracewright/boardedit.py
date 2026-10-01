"""In-app board editing: the board view sends an op list (the same ops as Claude's board tools, see
tw/pcb/kpy_ops.py), applied through the board worker -- or live in KiCad when it has the board open -- each batch one
undo step.

Undo keeps a copy of the board file from before each edit (the app's cache folder, not the project's, the last 50):
undo puts it back, redo the one from after. An edit from anywhere else since the last one (Claude, KiCad, a script) ends the history, since
undoing past it would undo that too.

What the user made by hand is remembered (.tracewright/handmade.json: footprints moved, tracks and vias drawn, pours
drawn or reshaped), so a change Claude later makes to it goes on the list for the user's OK."""
import json, os, shutil, threading, time

EDIT_OPS = {"move", "track", "tracks", "via", "vias", "delete", "track_set", "via_set", "zone", "zone_set", "rule_area",
            "fill", "lock"}
KEEP = 50


class History:
    def __init__(self, project):
        self.p = project
        self.undo, self.redo = [], []                    # [(snapshot file, label)]
        self.after = None                                # the board file's stamp after our last change to it
        self.n = 0
        self.lock = threading.Lock()

    @property
    def dir(self):
        from . import config
        return config.cache_dir("undo", str(self.p.id))

    def _stamp(self):
        st = os.stat(self.p.tw.pcb)
        return (st.st_mtime_ns, st.st_size)

    def _copy(self):
        self.n += 1
        f = os.path.join(self.dir, f"{int(time.time() * 1000)}-{self.n}.kicad_pcb")
        shutil.copyfile(self.p.tw.pcb, f)
        return f

    def valid(self):
        try:
            return self.after is not None and self._stamp() == self.after
        except OSError:
            return False

    def _drop(self):
        for f, _ in self.undo + self.redo:
            try:
                os.remove(f)
            except OSError:
                pass
        self.undo, self.redo = [], []

    def before(self):
        """A copy of the board as it is now, taken before an edit. A change made elsewhere since our last one ends
        the history first."""
        if not self.valid():
            self._drop()
        return self._copy()

    def done(self, snap, label):
        self.undo.append((snap, label))
        while len(self.undo) > KEEP:
            f, _ = self.undo.pop(0)
            try:
                os.remove(f)
            except OSError:
                pass
        for f, _ in self.redo:
            try:
                os.remove(f)
            except OSError:
                pass
        self.redo = []
        self.after = self._stamp()

    def failed(self, snap):
        try:
            os.remove(snap)
        except OSError:
            pass

    def step(self, back=True):
        """Undo (back) or redo one edit: the label, or None when there is nothing to undo or redo."""
        stack, other = (self.undo, self.redo) if back else (self.redo, self.undo)
        if not stack or not self.valid():
            return None
        snap, label = stack.pop()
        other.append((self._copy(), label))
        shutil.copyfile(snap, self.p.tw.pcb)             # a new write: the file's time moves on, and every reader sees it
        try:
            os.remove(snap)
        except OSError:
            pass
        self.after = self._stamp()
        return label

    def state(self):
        ok = self.valid()
        return {"undo": len(self.undo) if ok else 0, "redo": len(self.redo) if ok else 0,
                "undo_label": self.undo[-1][1] if ok and self.undo else "", "redo_label": self.redo[-1][1] if ok and self.redo else ""}


# ------------------------------------------------------------------ what the user made by hand
def handmade(project):
    try:
        with open(os.path.join(project.root, ".tracewright", "handmade.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    for k in ("footprints", "tracks", "vias", "zones"):
        d.setdefault(k, {} if k == "footprints" else [])
    return d


def record(project, ops, changes):
    """Remember what this edit made by hand: the footprints it moved (where to), the tracks, vias and pours it drew
    or reshaped; deletions forget theirs."""
    d = handmade(project)
    for c in changes or []:
        k = c.get("kind")
        if k == "move" and c.get("ref"):
            d["footprints"][c["ref"]] = c.get("to") or {}
        elif k == "track" and c.get("uuid"):
            d["tracks"].append(c["uuid"])
        elif k == "via" and c.get("uuid"):
            d["vias"].append(c["uuid"])
        elif k in ("zone", "zone_set") and c.get("uuid"):
            d["zones"].append(c["uuid"])
        elif k == "track_set" and c.get("uuid"):
            d["tracks"].append(c["uuid"])
        elif k == "via_set" and c.get("uuid"):
            d["vias"].append(c["uuid"])
    gone = set()
    for o in ops or []:
        if o.get("op") == "delete":
            gone.update(o.get("uuids") or [])
    for k in ("tracks", "vias", "zones"):
        d[k] = sorted({u for u in d[k] if u not in gone})[-2000:]
    os.makedirs(os.path.join(project.root, ".tracewright"), exist_ok=True)
    with open(os.path.join(project.root, ".tracewright", "handmade.json"), "w") as f:
        json.dump(d, f)
    return d


def describe(ops, changes):
    """One line for Claude's next turn: what the user did on the board."""
    parts = []
    moved = [c for c in changes or [] if c.get("kind") == "move"]
    if moved:
        parts.append("moved " + ", ".join(f"{c['ref']} to ({c['to']['x']:g}, {c['to']['y']:g}"
                                          f"{', ' + format(c['to']['rot'], 'g') + '°' if c['to'].get('rot') else ''}"
                                          f"{', bottom' if c['to'].get('side') == 'B' else ''})" for c in moved[:8] if c.get("to")))
    tracks = [c for c in changes or [] if c.get("kind") == "track"]
    if tracks:
        nets = sorted({c.get("net", "").rsplit("/", 1)[-1] for c in tracks if c.get("net")})
        layers = sorted({c.get("layer", "") for c in tracks})
        parts.append(f"routed {len(tracks)} segment{'s' if len(tracks) != 1 else ''} of {', '.join(nets[:6])} on {', '.join(layers)}")
    vias = [c for c in changes or [] if c.get("kind") == "via"]
    if vias:
        parts.append(f"added {len(vias)} via{'s' if len(vias) != 1 else ''}")
    dels = sum(1 for o in ops or [] if o.get("op") == "delete")
    if dels:
        parts.append("deleted copper")
    if any(c.get("kind") in ("zone", "zone_set") for c in changes or []) or any(o.get("op") in ("zone", "zone_set", "rule_area") for o in ops or []):
        parts.append("drew or reshaped a pour or keep-out")
    if any(c.get("kind") in ("track_set", "via_set") for c in changes or []):
        parts.append("moved or resized copper")
    return "; ".join(p for p in parts if p) or "edited the board"
