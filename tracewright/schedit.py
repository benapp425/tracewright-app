"""Schematic edits made in the app: a part's fields (value, footprint, part number, maker, LCSC code, data sheet,
description), its do-not-populate / in-BOM / on-board flags, and a net's name (the labels that name it, with KiCad's
netlist as the proof that the connections hold). Each edit is one undo step: the sheet files are copied before it
(in the cache, like the board's undo), and undo puts them back.

    ops: [{"op": "fields", "ref": "R4", "fields": {"Value": "4.7k", "LCSC": "C25900"}},
          {"op": "flags", "ref": "R9", "dnp": true},
          {"op": "rename", "from": "SDA", "to": "I2C_SDA", "kind": "global_label", "sheet": "/"}]

What the user sets by hand is remembered (.tracewright/handmade.json "fields") so a later change by Claude is
listed for their OK (approvals.py).
"""
import json, os, shutil, threading, time

FIELDS = ("Value", "Footprint", "MPN", "Manufacturer", "LCSC", "Datasheet", "Description")
OPS = {"fields", "flags", "rename"}
KEEP = 50


class SchHistory:
    """Undo and redo for the app's schematic edits. A change made elsewhere since the last one (Claude, KiCad)
    ends the history: undo never puts back sheets someone else has changed since."""

    def __init__(self, project):
        self.p = project
        self.undo, self.redo = [], []                     # [(snapshot dir, label)]
        self.after = None
        self.n = 0
        self.lock = threading.Lock()

    @property
    def dir(self):
        from . import config
        return config.cache_dir("undo", str(self.p.id), "sch")

    def _files(self):
        return sorted(os.path.abspath(f) for f in self.p.tw.sheets())

    def _stamp(self):
        out = []
        for f in self._files():
            try:
                st = os.stat(f)
                out.append((f, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((f, 0, 0))
        return tuple(out)

    def _copy(self):
        self.n += 1
        d = os.path.join(self.dir, f"{int(time.time() * 1000)}-{self.n}")
        os.makedirs(d, exist_ok=True)
        files = self._files()
        for i, f in enumerate(files):
            shutil.copyfile(f, os.path.join(d, f"{i}.kicad_sch"))
        with open(os.path.join(d, "files.json"), "w") as fh:
            json.dump(files, fh)
        return d

    def _restore(self, d):
        """Put the sheets back as the snapshot has them; only the files that differ are written."""
        with open(os.path.join(d, "files.json")) as fh:
            files = json.load(fh)
        n = 0
        for i, f in enumerate(files):
            src = os.path.join(d, f"{i}.kicad_sch")
            try:
                with open(src, "rb") as a, open(f, "rb") as b:
                    if a.read() == b.read():
                        continue
            except OSError:
                pass
            shutil.copyfile(src, f)
            n += 1
        return n

    def abandon(self, snap):
        """An edit that failed: whatever it wrote is put back, and the history goes on as before it."""
        was = self.valid()
        self._restore(snap)
        self.failed(snap)
        if was:
            self.after = self._stamp()

    def valid(self):
        return self.after is not None and self._stamp() == self.after

    def _drop(self):
        for d, _ in self.undo + self.redo:
            shutil.rmtree(d, ignore_errors=True)
        self.undo, self.redo = [], []

    def before(self):
        if not self.valid():
            self._drop()
        return self._copy()

    def failed(self, snap):
        shutil.rmtree(snap, ignore_errors=True)

    def done(self, snap, label):
        self.undo.append((snap, label))
        while len(self.undo) > KEEP:
            shutil.rmtree(self.undo.pop(0)[0], ignore_errors=True)
        for d, _ in self.redo:
            shutil.rmtree(d, ignore_errors=True)
        self.redo = []
        self.after = self._stamp()

    def step(self, back):
        """Undo (back) or redo one edit; returns its label, or None when there is nothing to do."""
        if not self.valid():
            self._drop()
            return None
        src, dst = (self.undo, self.redo) if back else (self.redo, self.undo)
        if not src:
            return None
        snap, label = src.pop()
        here = self._copy()
        self._restore(snap)
        shutil.rmtree(snap, ignore_errors=True)
        dst.append((here, label))
        self.after = self._stamp()
        return label

    def state(self):
        ok = self.valid()
        return {"undo": len(self.undo) if ok else 0, "redo": len(self.redo) if ok else 0,
                "undo_label": self.undo[-1][1] if ok and self.undo else None, "redo_label": self.redo[-1][1] if ok and self.redo else None}


def apply(project, ops):
    """Make the edits; returns [one line per change]. Raises ValueError for an edit that cannot be made (nothing
    written for it)."""
    from tw.sch import edit
    lines = []
    for o in ops:
        k = o.get("op")
        ref = str(o.get("ref") or "")
        if k == "fields":
            f = {str(a): str(b) for a, b in (o.get("fields") or {}).items() if a in FIELDS and b is not None}
            if not ref or not f:
                raise ValueError("fields: a part and at least one of " + ", ".join(FIELDS))
            if "Value" in f and not f["Value"].strip():
                raise ValueError("a part's value cannot be empty")
            rep = edit.set_fields(project.tw, {ref: f})
            if not rep and not _has(project, ref):
                raise ValueError(f"no {ref} in the schematic")
            lines += [x for v in rep.values() for x in v]
        elif k == "flags":
            f = {a: bool(o[a]) for a in edit.FLAGS if a in o}
            if not ref or not f:
                raise ValueError("flags: a part and dnp, in_bom or on_board")
            rep = edit.set_flags(project.tw, {ref: f})
            if not rep and not _has(project, ref):
                raise ValueError(f"no {ref} in the schematic")
            lines += [x for v in rep.values() for x in v]
        elif k == "rename":
            r = edit.rename_net(project.tw, o.get("from") or "", o.get("to") or "", kind=o.get("kind") or None, sheet=o.get("sheet") or None)
            lines.append(f"Renamed {o.get('from')} to {o.get('to')} ({r['labels']} label{'s' if r['labels'] != 1 else ''}"
                         + (f", {r['pins']} sheet pin{'s' if r['pins'] != 1 else ''}" if r["pins"] else "") + ")")
        else:
            raise ValueError(f"not a schematic edit: {k}")
    return lines


def _has(project, ref):
    from tw.schematic import Hierarchy
    return any(s.ref == ref for sh in Hierarchy.load(project.tw.sch).sheets for s in sh.symbols)


def record(project, ops):
    """Remember the fields the user set by hand ({ref: {field: value}}), for the approvals."""
    from .boardedit import handmade
    d = handmade(project)
    mine = d.setdefault("fields", {})
    for o in ops:
        if o.get("op") == "fields" and o.get("ref"):
            mine.setdefault(o["ref"], {}).update({a: str(b) for a, b in (o.get("fields") or {}).items() if a in FIELDS})
        elif o.get("op") == "flags" and o.get("ref"):
            mine.setdefault(o["ref"], {}).update({("DNP" if a == "dnp" else a): bool(o[a]) for a in ("dnp", "in_bom", "on_board") if a in o})
    os.makedirs(os.path.join(project.root, ".tracewright"), exist_ok=True)
    with open(os.path.join(project.root, ".tracewright", "handmade.json"), "w") as f:
        json.dump(d, f, indent=1)


def describe(lines):
    """One line for Claude's next turn: what the user changed on the schematic."""
    return "; ".join(lines[:10]) + (" ..." if len(lines) > 10 else "") if lines else "edited the schematic"
