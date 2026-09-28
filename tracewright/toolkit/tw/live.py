"""Live link to a running KiCad through its IPC API (KiCad 9+, Python package `kicad-python`).

With the board open in KiCad's PCB editor, edits made here appear in that window immediately, as
one undo step each ("Tracewright: place U3"), and the user's own selection can be read back. The
schematic editor is reached the same way for selection, save and reload.

KiCad must have the API server on: Preferences > Plugins > "Enable KiCad API" (the setting is
`api.enable_server` in kicad_common.json). Everything degrades to file edits when it is off.
"""
import os, time, math, json, shutil

try:
    from kipy import KiCad
    from kipy.board_types import Track, Via, FootprintInstance
    from kipy.geometry import Vector2, Angle
    from kipy.proto.common.types import base_types_pb2
    from kipy.errors import ApiError, ConnectionError as KiConnError
    HAVE_KIPY = True
except Exception:                                   # not installed, or a version without these names
    HAVE_KIPY = False

NM = 1_000_000


def _nm(v):
    return int(round(float(v) * NM))


def _mm(v):
    return v / NM


def socket_candidates():
    """KiCad's default socket and any per-instance sockets (a second KiCad gets api-<pid>.sock)."""
    if os.name == "nt":
        return [None]
    base = "/tmp/kicad"
    out = []
    if os.path.exists(os.path.join(base, "api.sock")):
        out.append("ipc://" + os.path.join(base, "api.sock"))
    if os.path.isdir(base):
        for f in sorted(os.listdir(base)):
            if f.startswith("api") and f.endswith(".sock") and f != "api.sock":
                out.append("ipc://" + os.path.join(base, f))
    return out or [None]


def connect(timeout_ms=1500):
    """(KiCad client, None) or (None, reason)."""
    if not HAVE_KIPY:
        return None, "the kicad-python package is not installed"
    last = "KiCad is not running with the API server enabled"
    for sock in socket_candidates():
        try:
            k = KiCad(socket_path=sock, client_name="tracewright", timeout_ms=timeout_ms) if sock else \
                KiCad(client_name="tracewright", timeout_ms=timeout_ms)
            k.ping()
            return k, None
        except Exception as e:                      # refused / timed out / not running
            last = str(e) or type(e).__name__
    return None, last


def _doc_path(doc, kind):
    try:
        proj = doc.project.path
        if kind == "pcb":
            fn = doc.board_filename
            return os.path.abspath(fn if os.path.isabs(fn) else os.path.join(proj, fn))
        return os.path.abspath(proj)
    except Exception:
        return None


def kicad_process_running():
    """Is any KiCad program running (whether or not its API is on)?"""
    import subprocess, sys
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5)
            return any(n in r.stdout.lower() for n in ("kicad.exe", "pcbnew.exe", "eeschema.exe"))
        r = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True, timeout=5)
        names = {os.path.basename(l.strip()).lower() for l in r.stdout.splitlines()}
        return bool(names & {"kicad", "pcbnew", "eeschema", "pcb editor", "schematic editor"})
    except Exception:
        return False


def api_setting_path():
    from .libtable import config_dir
    return os.path.join(config_dir(), "kicad_common.json")


def api_enabled():
    """KiCad's own "Enable KiCad API" setting: True / False, or None when its settings file is missing."""
    try:
        with open(api_setting_path()) as f:
            return bool(json.load(f).get("api", {}).get("enable_server"))
    except (OSError, ValueError):
        return None


def enable_api():
    """Turn the setting on (kicad_common.json api.enable_server), backing the file up first.
    Returns (path, already_on). KiCad reads it when it starts -- and a running KiCad may write its
    settings back when it quits, so re-apply it after KiCad has exited."""
    path = api_setting_path()
    with open(path) as f:
        d = json.load(f)
    if d.get("api", {}).get("enable_server"):
        return path, True
    shutil.copy(path, path + ".tracewright-backup")
    d.setdefault("api", {})["enable_server"] = True
    tmp = path + ".tracewright-tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
    return path, False


def status():
    """What the live link can see: {'api', 'running', 'process', 'version', 'boards', 'schematics', 'reason',
    'enabled' (KiCad's API setting)}."""
    out = {"api": HAVE_KIPY, "running": False, "process": False, "version": None, "boards": [], "schematics": [], "reason": None,
           "enabled": api_enabled()}
    k, why = connect()
    if k is None:
        out["process"] = kicad_process_running()
        if out["process"] and out["enabled"]:
            out["reason"] = "KiCad's API is turned on but this KiCad started before that: quit and reopen KiCad"
        elif out["process"]:
            out["reason"] = "KiCad is open but its API server is off (Preferences > Plugins > Enable KiCad API, then restart KiCad)"
        else:
            out["reason"] = why
        return out
    out["process"] = True
    out["running"] = True
    try:
        out["version"] = str(k.get_version())
    except Exception:
        pass
    try:
        for d in k.get_open_documents(base_types_pb2.DocumentType.DOCTYPE_PCB):
            p = _doc_path(d, "pcb")
            if p:
                out["boards"].append(p)
    except Exception as e:
        out["reason"] = f"could not list boards: {e}"
    try:
        for d in k.get_open_documents(base_types_pb2.DocumentType.DOCTYPE_SCHEMATIC):
            p = _doc_path(d, "sch")
            if p:
                out["schematics"].append(p)
    except Exception:
        pass
    return out


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def link_for(pcb_path):
    """A BoardLink when KiCad has this board open, else None."""
    if not HAVE_KIPY or not pcb_path:
        return None
    k, _ = connect()
    if k is None:
        return None
    try:
        from kipy.board import Board
        for d in k.get_open_documents(base_types_pb2.DocumentType.DOCTYPE_PCB):
            p = _doc_path(d, "pcb")
            if p and _same(p, pcb_path):
                return BoardLink(k, Board(k._client, d))
    except Exception:
        return None
    return None


def sch_link_for(project_dir):
    if not HAVE_KIPY:
        return None
    k, _ = connect()
    if k is None:
        return None
    try:
        from kipy.schematic import Schematic
        for d in k.get_open_documents(base_types_pb2.DocumentType.DOCTYPE_SCHEMATIC):
            p = _doc_path(d, "sch")
            if p and _same(p, project_dir):
                return SchLink(k, Schematic(k._client, d))
    except Exception:
        return None
    return None


class BoardLink:
    def __init__(self, kicad, board):
        self.k, self.b = kicad, board
        self._nets = None
        self._fps = None

    # ------------------------------------------------------------------ reading
    def nets(self):
        if self._nets is None:
            self._nets = {}
            for n in self.b.get_nets():
                self._nets[n.name] = n
                self._nets.setdefault(n.name.rsplit("/", 1)[-1], n)
        return self._nets

    def footprints(self, refresh=False):
        if self._fps is None or refresh:
            self._fps = {}
            for fp in self.b.get_footprints():
                try:
                    self._fps[fp.reference_field.text.value] = fp
                except Exception:
                    continue
        return self._fps

    def placement(self):
        """{ref: {x, y, rot, side, locked}} as the editor has it now (unsaved edits included)."""
        out = {}
        for ref, fp in self.footprints(refresh=True).items():
            out[ref] = {"x": round(_mm(fp.position.x), 4), "y": round(_mm(fp.position.y), 4),
                        "rot": round(fp.orientation.degrees % 360, 3),
                        "side": "B" if "B_Cu" in _layer_name(fp.layer) else "F", "locked": bool(fp.locked)}
        return out

    def selection(self):
        """What the user has selected in KiCad: [{'kind', 'ref'|'net', 'x', 'y'}]."""
        out = []
        for it in self.b.get_selection():
            cls = type(it).__name__
            d = {"kind": cls}
            try:
                if isinstance(it, FootprintInstance):
                    d.update(ref=it.reference_field.text.value, x=_mm(it.position.x), y=_mm(it.position.y))
                elif hasattr(it, "net") and hasattr(it, "start"):
                    d.update(net=it.net.name, x=_mm(it.start.x), y=_mm(it.start.y))
                elif hasattr(it, "net") and hasattr(it, "position"):
                    d.update(net=it.net.name, x=_mm(it.position.x), y=_mm(it.position.y))
                elif hasattr(it, "number") and hasattr(it, "position"):
                    d.update(pad=it.number, x=_mm(it.position.x), y=_mm(it.position.y))
            except Exception:
                pass
            out.append(d)
        return out

    # ------------------------------------------------------------------ pointing
    def highlight(self, refs=(), nets=()):
        """Select the footprints / the copper of the nets in KiCad (what the agent is talking about)."""
        items = []
        fps = self.footprints(refresh=True)
        items += [fps[r] for r in refs if r in fps]
        if nets:
            ns = [self.nets()[n] for n in nets if n in self.nets()]
            if ns:
                try:
                    items += list(self.b.get_items_by_net(ns))
                except Exception:
                    pass
        self.b.clear_selection()
        if items:
            self.b.add_to_selection(items)
        return len(items)

    # ------------------------------------------------------------------ editing
    def apply(self, ops, message=None, pace=0.0):
        """The kpy_ops op list, as one undo step in the open editor. pace: seconds between
        footprint moves, so a placement is seen happening."""
        results, changes = [], []
        commit = self.b.begin_commit()
        ok = True
        try:
            for op in ops:
                kind = op.get("op")
                try:
                    if kind == "move":
                        changes.append(self._move(op))
                        if pace:
                            time.sleep(pace)
                    elif kind in ("track", "tracks"):
                        items = op.get("items") if kind == "tracks" else [op]
                        self.b.create_items([self._track(t) for t in items])
                        changes += [{"kind": "track", **{k: t.get(k) for k in ("net", "layer", "a", "b", "w")}} for t in items]
                    elif kind in ("via", "vias"):
                        items = op.get("items") if kind == "vias" else [op]
                        self.b.create_items([self._via(v) for v in items])
                        changes += [{"kind": "via", **{k: v.get(k) for k in ("net", "x", "y", "d")}} for v in items]
                    elif kind == "delete":
                        n = self._delete(op)
                        changes.append({"kind": "delete", "count": n})
                    elif kind == "fill":
                        pass                                   # done after the commit
                    elif kind == "lock":
                        fps = self.footprints()
                        sel = [fps[r] for r in op.get("refs", []) if r in fps]
                        for fp in sel:
                            fp.locked = bool(op.get("locked", True))
                        if sel:
                            self.b.update_items(sel)
                    else:
                        raise NotImplementedError(f"'{kind}' is done on the file (live link: move, track, via, delete, lock)")
                    results.append({"op": kind, "ok": True})
                except Exception as e:
                    ok = False
                    results.append({"op": kind, "ok": False, "error": f"{type(e).__name__}: {e}"})
                    break
        finally:
            if ok:
                self.b.push_commit(commit, message or _describe(ops))
            else:
                self.b.drop_commit(commit)
        if ok and any(o.get("op") == "fill" for o in ops):
            self.b.refill_zones()
        return {"ok": ok, "results": results, "changes": changes}

    def _move(self, op):
        fps = self.footprints()
        fp = fps.get(op["ref"])
        if fp is None:
            fp = self.footprints(refresh=True).get(op["ref"])
        if fp is None:
            raise ValueError(f"no footprint {op['ref']} in the open board")
        before = {"x": _mm(fp.position.x), "y": _mm(fp.position.y), "rot": fp.orientation.degrees}
        side = op.get("side")
        cur_side = "B" if "B_Cu" in _layer_name(fp.layer) else "F"
        if side and side != cur_side:
            self.b.flip_items([fp])
            fp = self.footprints(refresh=True)[op["ref"]]
        if "x" in op and "y" in op:
            fp.position = Vector2.from_xy(_nm(op["x"]), _nm(op["y"]))
        if op.get("rot") is not None:
            fp.orientation = Angle.from_degrees(float(op["rot"]))
        if "locked" in op:
            fp.locked = bool(op["locked"])
        self.b.update_items([fp])
        after = {"x": op.get("x", before["x"]), "y": op.get("y", before["y"]), "rot": op.get("rot", before["rot"])}
        return {"kind": "move", "ref": op["ref"], "from": before, "to": after}

    def _layer(self, name):
        return self.b.get_layer_by_name(name)

    def _track(self, t):
        tr = Track()
        tr.start = Vector2.from_xy(_nm(t["a"][0]), _nm(t["a"][1]))
        tr.end = Vector2.from_xy(_nm(t["b"][0]), _nm(t["b"][1]))
        tr.width = _nm(t.get("w", 0.25))
        tr.layer = self._layer(t.get("layer", "F.Cu"))
        n = self.nets().get(t.get("net", ""))
        if n is not None:
            tr.net = n
        return tr

    def _via(self, v):
        via = Via()
        via.position = Vector2.from_xy(_nm(v["x"]), _nm(v["y"]))
        via.diameter = _nm(v.get("d", 0.6))
        via.drill_diameter = _nm(v.get("drill", 0.3))
        n = self.nets().get(v.get("net", ""))
        if n is not None:
            via.net = n
        return via

    def _delete(self, op):
        kinds = set(op.get("kinds") or ["track", "via"])
        nets = set(op.get("nets") or [])
        region = op.get("region")
        everything = bool(op.get("all"))
        victims = []
        pool = []
        if "track" in kinds:
            pool += list(self.b.get_tracks())
        if "via" in kinds:
            pool += list(self.b.get_vias())
        for it in pool:
            name = it.net.name if hasattr(it, "net") else ""
            hit = everything or (nets and (name in nets or name.rsplit("/", 1)[-1] in nets))
            if region:
                x0, y0, x1, y1 = region
                pts = [it.position] if hasattr(it, "position") and not hasattr(it, "start") else [it.start, it.end]
                inside = all(x0 <= _mm(p.x) <= x1 and y0 <= _mm(p.y) <= y1 for p in pts)
                hit = (hit and inside) if nets else inside
            if hit:
                victims.append(it)
        if victims:
            self.b.remove_items(victims)
        return len(victims)

    def save(self):
        self.b.save()

    def revert(self):
        self.b.revert()

    def refill(self):
        self.b.refill_zones()


class SchLink:
    def __init__(self, kicad, sch):
        self.k, self.s = kicad, sch

    def selection(self):
        out = []
        for it in self.s.get_selection():
            d = {"kind": type(it).__name__}
            for attr in ("reference", "text", "name"):
                try:
                    v = getattr(it, attr)
                    d[attr] = v if isinstance(v, str) else str(v)
                except Exception:
                    pass
            out.append(d)
        return out

    def save(self):
        self.s.save()

    def revert(self):
        self.s.revert()


def _layer_name(layer):
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer
        return BoardLayer.Name(layer)
    except Exception:
        return str(layer)


def _describe(ops):
    moves = [o["ref"] for o in ops if o.get("op") == "move"]
    if moves:
        return "Tracewright: place " + ", ".join(moves[:6]) + (" ..." if len(moves) > 6 else "")
    kinds = sorted({o.get("op") for o in ops})
    return "Tracewright: " + ", ".join(kinds)
