"""Review flags: what the user marks on the board, the schematic or the 3D view ("move C3 closer to
U1", "this trace is too thin"), or picks from the check findings. They collect in the project
(.tracewright/review.json, snapshots in .tracewright/review/), go to Claude together in one message
with a snapshot of each, and Claude marks each one fixed -- or won't fix, with the reason -- with its
review tool. Kept out of the checkpoints: restoring an old version never brings old flags back."""
import os, json, time, base64, threading

STATUSES = ("open", "sent", "fixed", "wontfix")
VIEWS = ("board", "schematic", "3d", "check")
WHERE_KEYS = ("x", "y", "region", "sheet", "refs", "nets", "layer", "side", "p3", "check", "camera")
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
        return [f for f in fl if not status or status == "all" or f["status"] == status]

    def get(self, fid):
        for f in self._load()["flags"]:
            if f["id"] == fid or str(f["n"]) == str(fid).lstrip("F#"):
                return f
        raise KeyError(fid)

    # ------------------------------------------------------------------ changing
    def add(self, view, text, where=None, snapshot=None, source="user"):
        text = (text or "").strip()
        if not text:
            raise ValueError("say what should change")
        with _lock:
            d = self._load()
            n = d["next"]
            d["next"] = n + 1
            f = {"id": f"F{n}", "n": n, "view": view if view in VIEWS else "board", "status": "open", "text": text[:4000],
                 "where": _clean_where(where), "source": source, "created": _now(), "updated": _now(), "sent": None,
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

    def update(self, fid, **changes):
        with _lock:
            d = self._load()
            for f in d["flags"]:
                if f["id"] != fid:
                    continue
                if "text" in changes and (changes["text"] or "").strip():
                    f["text"] = changes["text"].strip()[:4000]
                if changes.get("status") in STATUSES:
                    f["status"] = changes["status"]
                    if changes["status"] == "open":                       # reopened: the old answer no longer holds
                        f["resolution"] = f["resolved_by"] = None
                if "where" in changes:
                    f["where"] = _clean_where({**f.get("where", {}), **(changes["where"] or {})})
                if "resolution" in changes:
                    f["resolution"] = changes["resolution"]
                if changes.get("snapshot"):
                    f["snapshot"] = self._write_snapshot(fid, changes["snapshot"])
                f["updated"] = _now()
                self._save(d)
                return f
        raise KeyError(fid)

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
            gone = [f["id"] for f in d["flags"] if f["status"] in ("fixed", "wontfix")]
            d["flags"] = [f for f in d["flags"] if f["status"] not in ("fixed", "wontfix")]
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
        if status not in ("fixed", "wontfix"):
            raise ValueError("status is fixed or wontfix")
        with _lock:
            d = self._load()
            for f in d["flags"]:
                if f["id"] == fid or str(f["n"]) == str(fid).lstrip("F#"):
                    f["status"] = status
                    f["resolution"] = (note or "").strip()[:2000] or None
                    f["resolved_by"] = by
                    f["updated"] = _now()
                    self._save(d)
                    return f
        raise KeyError(fid)

    # ------------------------------------------------------------------ the message to Claude
    @staticmethod
    def describe(f):
        """One flag as Claude reads it: where, what is there, what the user said."""
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
        head = f"{f['id']} ({view})" + (": " + "; ".join(bits) if bits else "")
        text = "\n".join("  " + line for line in f["text"].splitlines())
        return f"{head}\n  \"{text.strip()}\""

    def message(self, flags, note=""):
        n = len(flags)
        with_img = [f for f in flags if f.get("snapshot")]
        lines = [f"Review flags from the user: {n} to work through."
                 + (f" A snapshot of each marked spot is attached, in this order: {', '.join(f['id'] for f in with_img)}." if with_img else ""),
                 "For each one: find it (the coordinates and parts are below; `show` it to the user), make the change, check it "
                 "(render the spot or run the relevant check), then mark it with the review tool: action \"resolve\", its id, "
                 "status \"fixed\" and one line on what you changed. If a flag should not be done as asked, resolve it with status "
                 "\"wontfix\" and the reason. Work through them all before stopping; ask only if a flag is ambiguous."]
        if note.strip():
            lines += ["", "The user adds: " + note.strip()]
        lines.append("")
        lines += [self.describe(f) for f in flags]
        return "\n".join(lines)
