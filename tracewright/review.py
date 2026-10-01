"""Review flags: comments the user leaves on the board, the schematic or the 3D view ("move C3 closer to U1",
"is this trace wide enough?"), or picks from the check findings -- each a request (do this) or a question (what do
you think?), with marks drawn on the view if the user drew any: a pen line, an arrow, a box, a sketch of where a
track should run, an area. They collect in the project (.tracewright/review.json, snapshots in .tracewright/review/)
and go to Claude together in one message with a snapshot of each.

Each flag is a thread. Claude replies to every one with its review tool: it made the change (done: green), it
disagrees and changed nothing (declined: red, with why), or it answered a question that needed no change
(answered). The user can reply, ask again, or say do it anyway; the reply goes to Claude with the next send. Kept
out of the checkpoints: restoring an old version never brings old flags back."""
import os, json, time, base64, threading

STATUSES = ("open", "sent", "done", "declined", "answered")
OUTCOMES = ("done", "declined", "answered")
OLD = {"fixed": "done", "wontfix": "declined"}          # 0.x flags
ASKS = ("request", "question")
VIEWS = ("board", "schematic", "3d", "check")
WHERE_KEYS = ("x", "y", "region", "sheet", "refs", "nets", "layer", "side", "p3", "check", "camera")
MARKS = ("pen", "arrow", "box", "route", "area", "text")
MAX_SNAPSHOT = 3 * 1024 * 1024
_lock = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_where(w):
    w = {k: v for k, v in (w or {}).items() if k in WHERE_KEYS and v not in (None, "", [])}
    for k in ("x", "y"):
        if k in w:
            w[k] = round(float(w[k]), 3)
    if "region" in w:
        r = [round(float(v), 3) for v in w["region"]][:4]
        w["region"] = [min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])] if len(r) == 4 else None
    for k in ("refs", "nets"):
        if k in w:
            w[k] = [str(v) for v in w[k]][:24]
    return {k: v for k, v in w.items() if v is not None}


def _clean_marks(marks):
    """What the user drew: [{t: pen | arrow | box | route | area | text, p: [[x, y], ...], s?: text, layer?}], in the
    view's millimetres."""
    out = []
    for m in (marks or [])[:20]:
        if not isinstance(m, dict) or m.get("t") not in MARKS:
            continue
        pts = []
        for q in (m.get("p") or [])[:400]:
            try:
                pts.append([round(float(q[0]), 3), round(float(q[1]), 3)])
            except (TypeError, ValueError, IndexError):
                continue
        if len(pts) < (1 if m["t"] == "text" else 2):
            continue
        mk = {"t": m["t"], "p": pts}
        if m.get("s"):
            mk["s"] = str(m["s"])[:200]
        if m.get("layer"):
            mk["layer"] = str(m["layer"])[:16]
        out.append(mk)
    return out


def _migrate(f):
    f["status"] = OLD.get(f.get("status"), f.get("status") or "open")
    f.setdefault("ask", "request")
    f.setdefault("marks", [])
    if "thread" not in f:                                 # the answer a 0.x flag had becomes the thread's reply
        f["thread"] = []
        if f.get("resolution"):
            f["thread"].append({"who": "claude" if f.get("resolved_by") == "claude" else "you", "text": f["resolution"],
                                "at": f.get("updated") or _now(), "outcome": f["status"] if f["status"] in OUTCOMES else None})
    return f


class Review:
    def __init__(self, project):
        self.p = project
        self.path = os.path.join(project.state_dir(), "review.json")

    # ------------------------------------------------------------------ storage
    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            d.setdefault("flags", [])
            d.setdefault("next", 1 + max([f.get("n", 0) for f in d["flags"]] or [0]))
            for f in d["flags"]:
                _migrate(f)
            return d
        except (OSError, ValueError):
            return {"next": 1, "flags": []}

    def _save(self, d):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.path)

    def snapshot_path(self, fid):
        return os.path.join(self.p.state_dir("review"), f"{fid}.png")

    # ------------------------------------------------------------------ reading
    def flags(self, status=None):
        fl = self._load()["flags"]
        if status == "active":
            return [f for f in fl if f["status"] in ("open", "sent")]
        if status == "replied":
            return [f for f in fl if f["status"] in OUTCOMES]
        status = OLD.get(status, status)
        return [f for f in fl if not status or status == "all" or f["status"] == status]

    def get(self, fid):
        for f in self._load()["flags"]:
            if f["id"] == fid or str(f["n"]) == str(fid).lstrip("F#"):
                return f
        raise KeyError(fid)

    # ------------------------------------------------------------------ changing
    def add(self, view, text, where=None, snapshot=None, source="user", ask="request", marks=None):
        text = (text or "").strip()
        if not text:
            raise ValueError("say what should change, or what you want to know")
        with _lock:
            d = self._load()
            n = d["next"]
            d["next"] = n + 1
            f = {"id": f"F{n}", "n": n, "view": view if view in VIEWS else "board", "status": "open", "text": text[:4000],
                 "ask": ask if ask in ASKS else "request", "where": _clean_where(where), "marks": _clean_marks(marks),
                 "thread": [], "source": source, "created": _now(), "updated": _now(), "sent": None,
                 "resolution": None, "resolved_by": None, "snapshot": False}
            if snapshot:
                f["snapshot"] = self._write_snapshot(f["id"], snapshot)
            d["flags"].append(f)
            self._save(d)
        return f

    def _write_snapshot(self, fid, data):
        if isinstance(data, str):
            data = base64.b64decode(data.split(",", 1)[-1])
        if not data.startswith(b"\x89PNG") or len(data) > MAX_SNAPSHOT:
            return False
        with open(self.snapshot_path(fid), "wb") as fh:
            fh.write(data)
        return True

    def _edit(self, fid, fn):
        with _lock:
            d = self._load()
            for f in d["flags"]:
                if f["id"] == fid or str(f["n"]) == str(fid).lstrip("F#"):
                    fn(f)
                    f["updated"] = _now()
                    self._save(d)
                    return f
        raise KeyError(fid)

    def update(self, fid, **changes):
        def go(f):
            if "text" in changes and (changes["text"] or "").strip():
                f["text"] = changes["text"].strip()[:4000]
            st = OLD.get(changes.get("status"), changes.get("status"))
            if st in STATUSES:
                f["status"] = st
                if st == "open":                                   # reopened: the old answer no longer holds
                    f["resolution"] = f["resolved_by"] = None
            if changes.get("ask") in ASKS:
                f["ask"] = changes["ask"]
            if "where" in changes:
                f["where"] = _clean_where({**f.get("where", {}), **(changes["where"] or {})})
            if "marks" in changes:
                f["marks"] = _clean_marks(changes["marks"])
            if "resolution" in changes:
                f["resolution"] = changes["resolution"]
            if changes.get("snapshot"):
                f["snapshot"] = self._write_snapshot(f["id"], changes["snapshot"])
        return self._edit(fid, go)

    def reply(self, fid, text, who="you", outcome=None, anyway=False):
        """A message in the flag's thread. The user's goes to Claude with the next send (the flag is open again);
        Claude's carries its outcome: done (it changed it), declined (it disagrees: nothing changed), answered."""
        text = (text or "").strip()
        if not text and not anyway:
            raise ValueError("say something")
        if who == "claude" and outcome not in OUTCOMES:
            raise ValueError("outcome is done, declined or answered")

        def go(f):
            m = {"who": who, "text": text[:4000] or "Do it anyway.", "at": _now()}
            if who == "claude":
                m["outcome"] = outcome
                f["status"] = outcome
                f["resolution"] = text[:2000]
                f["resolved_by"] = "claude"
            else:
                if anyway:
                    m["anyway"] = True
                f["status"] = "open"
                f["resolution"] = f["resolved_by"] = None
            f["thread"].append(m)
        return self._edit(fid, go)

    def delete(self, fid):
        with _lock:
            d = self._load()
            before = len(d["flags"])
            d["flags"] = [f for f in d["flags"] if f["id"] != fid]
            if len(d["flags"]) == before:
                raise KeyError(fid)
            self._save(d)
        try:
            os.remove(self.snapshot_path(fid))
        except OSError:
            pass

    def clear_resolved(self):
        with _lock:
            d = self._load()
            gone = [f["id"] for f in d["flags"] if f["status"] in OUTCOMES]
            d["flags"] = [f for f in d["flags"] if f["status"] not in OUTCOMES]
            self._save(d)
        for fid in gone:
            try:
                os.remove(self.snapshot_path(fid))
            except OSError:
                pass
        return len(gone)

    def mark_sent(self, ids):
        with _lock:
            d = self._load()
            for f in d["flags"]:
                if f["id"] in ids and f["status"] in ("open", "sent"):
                    f["status"] = "sent"
                    f["sent"] = f["updated"] = _now()
            self._save(d)

    def resolve(self, fid, status, note, by="claude"):
        """An answer the 0.x way: fixed (done) or wontfix (declined)."""
        status = OLD.get(status, status)
        if status not in OUTCOMES:
            raise ValueError("status is done, declined or answered")
        if by == "claude":
            return self.reply(fid, note or ("Done." if status == "done" else "Not done."), who="claude", outcome=status)
        return self.update(fid, status=status, resolution=note)

    # ------------------------------------------------------------------ the message to Claude
    @staticmethod
    def describe_marks(f):
        unit = "mm on the page" if f["view"] == "schematic" else "mm on the board"
        out = []
        for m in f.get("marks") or []:
            pts = m["p"]
            path = " -> ".join(f"({x:.2f}, {y:.2f})" for x, y in pts[:24]) + (" ..." if len(pts) > 24 else "")
            t = m["t"]
            if t == "route":
                out.append(f"a sketch of where the track should run{' on ' + m['layer'] if m.get('layer') else ''}: {path} "
                           f"({unit}; to route along it: the route tool with along \"{f['id']}\")")
            elif t == "area":
                out.append(f"an area: {path} ({unit})")
            elif t == "box":
                (x0, y0), (x1, y1) = pts[0], pts[-1]
                out.append(f"a box ({min(x0, x1):.2f}, {min(y0, y1):.2f}) to ({max(x0, x1):.2f}, {max(y0, y1):.2f}) {unit}")
            elif t == "arrow":
                out.append(f"an arrow from ({pts[0][0]:.2f}, {pts[0][1]:.2f}) to ({pts[-1][0]:.2f}, {pts[-1][1]:.2f}) {unit}")
            elif t == "text":
                out.append(f"a note \"{m.get('s', '')}\" at ({pts[0][0]:.2f}, {pts[0][1]:.2f}) {unit}")
            else:
                out.append(f"a pen line {path} ({unit})")
        return out

    @staticmethod
    def describe(f):
        """One flag as Claude reads it: what kind, where, what is there, what the user said, the thread so far."""
        w = f.get("where") or {}
        view = {"board": "board", "schematic": "schematic", "3d": "3D view", "check": "from the checks"}[f["view"]]
        bits = []
        if w.get("check"):
            bits.append(f"check {w['check']}")
        if w.get("sheet"):
            bits.append(f"sheet {w['sheet']}")
        unit = "mm on the page" if f["view"] == "schematic" else "mm on the board"
        if w.get("region"):
            r = w["region"]
            bits.append(f"area ({r[0]:.2f}, {r[1]:.2f}) to ({r[2]:.2f}, {r[3]:.2f}) {unit}")
        elif "x" in w:
            bits.append(f"at ({w['x']:.2f}, {w['y']:.2f}) {unit}")
        if w.get("side"):
            bits.append("bottom side" if w["side"] == "B" else "top side")
        if w.get("layer"):
            bits.append(f"on {w['layer']}")
        if w.get("refs"):
            bits.append(("symbols " if f["view"] == "schematic" else "parts ") + ", ".join(w["refs"]))
        if w.get("nets"):
            bits.append("nets " + ", ".join(n.rsplit("/", 1)[-1] for n in w["nets"]))
        kind = "a question" if f.get("ask") == "question" else "a request"
        head = f"{f['id']} ({view}, {kind})" + (": " + "; ".join(bits) if bits else "")
        text = "\n".join("  " + line for line in f["text"].splitlines())
        lines = [head, f"  \"{text.strip()}\""]
        for m in Review.describe_marks(f):
            lines.append("  drawn: " + m)
        for m in f.get("thread") or []:
            who = "You (Claude)" if m["who"] == "claude" else "The user"
            tag = {"done": " [done]", "declined": " [declined]", "answered": " [answered]"}.get(m.get("outcome"), "")
            if m.get("anyway"):
                tag = " [do it anyway]"
            lines.append(f"  {who}{tag}: \"{m['text'].strip()}\"")
        return "\n".join(lines)

    def message(self, flags, note=""):
        n = len(flags)
        with_img = [f for f in flags if f.get("snapshot")]
        lines = [f"Review flags from the user: {n} to work through."
                 + (f" A snapshot of each marked spot is attached, in this order: {', '.join(f['id'] for f in with_img)}." if with_img else ""),
                 "Each is a request (do it) or a question (what do you think?), some with marks the user drew, some with the "
                 "thread so far. Find each one (the coordinates and parts are below; `show` it to the user), then:",
                 "- a request: make the change, check it (render the spot or run the relevant check), and reply with the review "
                 "tool: action \"reply\", its id, outcome \"done\" and a line on what you changed. If it should not be done as "
                 "asked, reply with outcome \"declined\" and why, and change nothing.",
                 "- a question: answer it. If you agree and it means a change, make it and reply \"done\"; if you disagree, reply "
                 "\"declined\" with your reasons and change nothing; if no change is needed, reply \"answered\".",
                 "- \"do it anyway\": the user heard you out and wants it done: do it and reply \"done\".",
                 "Reply to every flag before stopping; ask only if one is ambiguous."]
        if note.strip():
            lines += ["", "The user adds: " + note.strip()]
        lines.append("")
        lines += [self.describe(f) for f in flags]
        return "\n".join(lines)
