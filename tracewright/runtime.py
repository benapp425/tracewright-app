"""What the app keeps for an open project: its event hub, the geometry caches for the viewers, the
file watcher (who changed what), the KiCad live-link poller, selections and annotations.

The watcher tells the user's edits from Tracewright's own: writes made by the agent's tools open a
short "self" window; any other change to the board or schematic is the user's (a save in KiCad),
and is summarised for the agent's next turn ("you moved U3 by 4 mm", "power.kicad_sch was edited").
"""
import os, json, time, asyncio, threading, glob, math, traceback
from tw.board import Board
from tw.schematic import Hierarchy
from tw import kicad as twkicad, live as twlive
from tw.checks.context import map_svgs, sheet_svg_names


def _mtimes(paths):
    out = {}
    for p in paths:
        try:
            out[p] = os.path.getmtime(p)
        except OSError:
            pass
    return out


class ProjectRuntime:
    def __init__(self, app, project):
        self.app = app
        self.p = project
        self.hub = app.hubs.get(project.id)
        self.self_until = 0.0
        self._board = None              # (key, Board, json, version)
        self._sch = None
        self._svg_lock = threading.Lock()
        self._glb_lock = threading.Lock()
        self.version = {"board": 0, "schematic": 0}
        self.user_changes = []          # summaries for the agent's next turn
        self.selection = {"app": [], "kicad": []}
        self.attached = {}               # files attached to the message being written: path -> record (attach.py)
        self.live = {"running": False, "board_open": False, "sch_open": False, "api": twlive.HAVE_KIPY}
        self._live_place = None
        self._live_sel = None
        self._tasks = []
        self._last = {}
        self._fp_state = None
        self.viewers = 0
        from .timelapse import Timelapse
        self.timelapse = Timelapse(project.root)
        self.agenda = None                    # the agenda Claude keeps for the current request (agenda tool)
        self.hub.listeners.append(self._tap)
        self.checks_lock = asyncio.Lock()      # one check run at a time (Checks tab or the agent)
        self.checks_stop = None                # threading.Event of the run in progress
        self.bom_stop = None                   # threading.Event of the parts lookup in progress
        self.edit_lock = asyncio.Lock()        # one board edit from the app at a time
        self.app_edit_until = 0.0              # the board view's own edits: the user's, already described
        self._edits = None
        self._sch_edits = None

    # ------------------------------------------------------------------ lifecycle
    def _tap(self, ev):
        """The router's live stream goes into the timelapse net by net."""
        if ev.get("type") == "route.progress" and ev.get("status") in ("routed", "escape", "stitch") and ev.get("net"):
            fr = self.timelapse.record_route(ev["net"], ev.get("tracks"), ev.get("vias"), ev.get("done"), ev.get("total"))
            if fr:
                self.hub.emit("timelapse.frame", **fr)

    def record_frame(self, board, src):
        try:
            fr = self.timelapse.record(board, src)
        except Exception:
            traceback.print_exc()
            return
        if fr:
            self.hub.emit("timelapse.frame", **fr)

    def start(self):
        if not self._tasks:
            if self.p.tw.has_pcb():                   # the timelapse's first picture
                def first():
                    try:
                        if self.timelapse.count() == 0:
                            b = self.board()
                            if b is not None:
                                self.record_frame(b, "start")
                    except Exception:
                        traceback.print_exc()
                threading.Thread(target=first, daemon=True).start()
            self._last = self._file_state()
            self._tasks = [asyncio.ensure_future(self._watch())]
            if not getattr(self.app, "server_mode", False):         # a server has no KiCad window to link to
                self._tasks.append(asyncio.ensure_future(self._poll_live()))

    def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    def mark_self(self, seconds=3.0):
        """Tracewright is about to write the design files (agent tools, router, checks)."""
        self.self_until = max(self.self_until, time.time() + seconds)

    def mark_app_edit(self, seconds=3.0):
        """The user is editing the board in the app: their change, described by the edit itself."""
        self.app_edit_until = max(self.app_edit_until, time.time() + seconds)

    @property
    def edits(self):
        """The board view's undo history (boardedit.History)."""
        if self._edits is None:
            from .boardedit import History
            self._edits = History(self.p)
        return self._edits

    @property
    def sch_edits(self):
        """The schematic view's undo history (schedit.SchHistory)."""
        if self._sch_edits is None:
            from .schedit import SchHistory
            self._sch_edits = SchHistory(self.p)
        return self._sch_edits

    # ------------------------------------------------------------------ board geometry
    def board_path(self):
        return self.p.tw.pcb if self.p.tw.has_pcb() else None

    def board(self):
        path = self.board_path()
        if not path:
            return None
        key = (os.path.getmtime(path), os.path.getsize(path))
        if self._board is None or self._board[0] != key:
            for tries in range(25):                       # caught mid-save (KiCad writes in place): read it once it is whole
                try:
                    b = Board.load(path)
                except ValueError:
                    if tries == 24:
                        raise
                    time.sleep(0.12)
                    key = (os.path.getmtime(path), os.path.getsize(path))
                    continue
                now = (os.path.getmtime(path), os.path.getsize(path))
                if now == key:
                    break
                key = now                                 # it changed while it was read: again
            self.version["board"] += 1
            self._board = (key, b, None, self.version["board"])
        return self._board[1]

    def board_json(self):
        b = self.board()
        if b is None:
            return None
        key, _, js, ver = self._board
        if js is None:
            js = b.to_json()
            js["summary"] = b.summary()
            js["version"] = ver
            js["unrouted"] = self._unrouted()
            from tw import ratsnest
            try:
                js["ratsnest"] = ratsnest.ratsnest(b)          # from the copper itself: right after every edit
            except Exception:
                traceback.print_exc()
                js["ratsnest"] = None
            js["rules"] = self.board_rules()
            from tw import stackup
            plan = stackup.get(self.p.cfg)
            js["stackup"] = {"roles": stackup.board_roles(b, plan), "preset": (plan or {}).get("preset") if plan and len(b.copper) == plan["layers"] else None}
            self._board = (key, b, js, ver)
        return js

    def board_rules(self):
        """What the editor holds new copper to: each net class's clearance, width and via, which class each net is in,
        and the board's minimums (the .kicad_pro)."""
        from tw.pro import ProjectSettings
        ps = ProjectSettings.load(self.p.tw.pro) if self.p.tw.pro else ProjectSettings({})
        classes = {}
        for name in set(ps.classes) | {"Default"}:
            c = ps.cls(name)
            classes[name] = {k: c.get(k) for k in ("clearance", "track_width", "via_diameter", "via_drill", "diff_pair_width", "diff_pair_gap")}
        b = self.board()
        net_class = {}
        for n in (b.nets if b else []):
            c = ps.class_of(n)
            if c != "Default":
                net_class[n] = c
        r = ps.rules or {}
        return {"classes": classes, "net_class": net_class,
                "min_clearance": r.get("min_clearance", 0.0) or 0.0, "min_track": r.get("min_track_width", 0.0) or 0.0,
                "min_via": r.get("min_via_diameter", 0.0) or 0.0, "edge_clearance": r.get("min_copper_edge_clearance", 0.0) or 0.0,
                "hole_clearance": r.get("min_hole_clearance", 0.0) or 0.0}

    def _unrouted(self):
        """Unrouted connections from the last DRC, if it is not older than the board: [[x1, y1, x2, y2]]."""
        f = os.path.join(self.p.tw.build, "drc.json")
        try:
            if os.path.getmtime(f) < os.path.getmtime(self.p.tw.pcb) - 2:
                return None
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        out = []
        for u in d.get("unconnected_items", []):
            its = [i.get("pos") for i in u.get("items", []) if i.get("pos")]
            if len(its) >= 2:
                out.append([its[0]["x"], its[0]["y"], its[1]["x"], its[1]["y"]])
        return out

    def glb(self, components=True):
        """The board as a 3D model for the 3D view (KiCad's GLB export, its primitives merged per mesh and
        material so the browser draws a few hundred instead of tens of thousands), kept until the board
        changes -- in the app's cache folder, not the project's: a project in iCloud would upload every one."""
        from tw import glbopt
        from . import config
        pcb = self.board_path()
        if not pcb:
            return None
        kind = "parts" if components else "bare"
        key = f"{int(os.path.getmtime(pcb) * 1000)}-{os.path.getsize(pcb)}"
        d = config.cache_dir("glb", str(self.p.id))
        for old in glob.glob(os.path.join(self.p.root, ".tracewright", "cache", "board-*.glb*")):     # where 0.x kept them
            try:
                os.remove(old)
            except OSError:
                pass
        out = os.path.join(d, f"board-{kind}-m1-{key}.glb")
        with self._glb_lock:
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
            for old in glob.glob(os.path.join(d, f"board-{kind}-*.glb")):
                try:
                    os.remove(old)
                except OSError:
                    pass
            raw = out + ".kicad.glb"
            twkicad.pcb_glb(pcb, raw, components=components)
            try:
                glbopt.optimize(raw, out)
            except Exception:                      # never lose the model over the optimisation
                traceback.print_exc()
                os.replace(raw, out)
            finally:
                if os.path.exists(raw):
                    os.remove(raw)
        return out

    def footprint_state(self, b=None):
        b = b or self.board()
        if b is None:
            return {}
        return {fp.ref: (round(fp.x, 3), round(fp.y, 3), round(fp.angle % 360, 2), fp.side) for fp in b.fp_list}

    # ------------------------------------------------------------------ schematic
    def schematic_json(self):
        tw = self.p.tw
        if not tw.has_sch():
            return None
        files = tw.sheets()
        key = tuple(sorted(_mtimes(files).items()))
        if self._sch is None or self._sch[0] != key:
            h = Hierarchy.load(tw.sch)
            self.version["schematic"] += 1
            names = sheet_svg_names(h)
            js = {"version": self.version["schematic"], "project": h.project,
                  "sheets": [dict(sh.to_json(), svg=names.get(sh.name_path)) for sh in h.sheets]}
            self._sch = (key, h, js)
        return self._sch[2]

    def svg_dir(self):
        return os.path.join(self.p.tw.build, "sch_svg")

    def ensure_svgs(self):
        """Plot the sheets if the schematic is newer than the plots (kicad-cli, ~1 s a sheet)."""
        tw = self.p.tw
        d = self.svg_dir()
        with self._svg_lock:
            files = sorted(glob.glob(os.path.join(d, "*.svg")))
            newest = max(_mtimes(tw.sheets()).values() or [0])
            if not files or min(os.path.getmtime(f) for f in files) < newest:
                self.mark_self(5)
                files = twkicad.sch_svg(tw.sch, d, drawing_sheet=False, theme="_builtin_default")
        return files

    def svg_for(self, name_path):
        files = self.ensure_svgs()
        js = self.schematic_json()
        stem = None
        for sh in js["sheets"]:
            if sh["name_path"] == name_path:
                stem = sh.get("svg")
        by = {os.path.splitext(os.path.basename(f))[0]: f for f in files}
        if stem in by:
            return by[stem]
        h = self._sch[1]
        from tw.svg import SvgDoc
        m = map_svgs(h, files, SvgDoc.load)
        if name_path in m:
            return m[name_path][1]
        return files[0] if files else None

    # ------------------------------------------------------------------ watching the files
    def _files(self):
        tw = self.p.tw
        fs = list(tw.sheets())
        if tw.pcb:
            fs.append(tw.pcb)
        fs.append(os.path.join(self.p.root, "tracewright.json"))
        fs.append(os.path.join(tw.build, "checks.json"))
        return fs

    def _file_state(self):
        return _mtimes(self._files())

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(1.0)
                cur = await asyncio.get_running_loop().run_in_executor(None, self._file_state)
                changed = [f for f in set(cur) | set(self._last) if cur.get(f) != self._last.get(f)]
                self._last = cur
                if changed:
                    await self._on_change(changed)
            except asyncio.CancelledError:
                return
            except Exception:
                traceback.print_exc()

    async def _on_change(self, changed):
        tw = self.p.tw
        now = time.time()
        source = "tracewright" if now < self.self_until else "user"
        loop = asyncio.get_running_loop()
        if tw.pcb in changed and tw.has_pcb():
            app_edit = source == "user" and now < self.app_edit_until      # the board view's edit, told already
            before = self._fp_state
            b = await loop.run_in_executor(None, self.board)
            after = self.footprint_state(b)
            self._fp_state = after
            diff = _placement_diff(before, after) if before is not None else []
            if source == "user" and not app_edit and diff:
                self.user_changes.append("board: " + "; ".join(diff[:12]) + (" ..." if len(diff) > 12 else ""))
            elif source == "user" and not app_edit:
                self.user_changes.append("board saved (copper or other edits)")
            self.hub.emit("board.changed", version=self.version["board"], source="app" if app_edit else source, moved=diff[:40])
            await loop.run_in_executor(None, self.record_frame, b, "claude" if source == "tracewright" else "you")
        sch_changed = [f for f in changed if f.endswith(".kicad_sch")]
        if sch_changed:
            if source == "user":
                self.user_changes.append("schematic edited: " + ", ".join(os.path.basename(f) for f in sch_changed))
            self.hub.emit("schematic.changed", source=source, files=[os.path.basename(f) for f in sch_changed])
        if os.path.join(self.p.root, "tracewright.json") in changed:
            self.p.reload()
            self.hub.emit("project.changed", summary=self.p.summary())
        if os.path.join(tw.build, "checks.json") in changed:
            self.hub.emit("checks.updated")

    def take_user_changes(self):
        out, self.user_changes = self.user_changes, []
        return out

    # ------------------------------------------------------------------ the KiCad live link
    async def _poll_live(self):
        loop = asyncio.get_running_loop()
        idle = 0
        while True:
            try:
                await asyncio.sleep(2.0 if self.viewers or self.app.agent_busy(self.p.id) else 6.0)
                st = await loop.run_in_executor(None, self._live_once)
                if st is not None and st != self.live:
                    self.live = st
                    self.hub.emit("live.status", **st)
            except asyncio.CancelledError:
                return
            except Exception:
                traceback.print_exc()

    def _live_once(self):
        tw = self.p.tw
        st = twlive.status()
        out = {"api": st["api"], "running": st["running"], "process": st.get("process"), "version": st.get("version"),
               "reason": st.get("reason"), "board_open": False, "sch_open": False}
        if tw.pcb:
            out["board_open"] = any(_same(b, tw.pcb) for b in st["boards"])
        if st["schematics"]:
            out["sch_open"] = any(_same(s, tw.hw) or _same(s, self.p.root) for s in st["schematics"])
        if out["board_open"]:
            link = twlive.link_for(tw.pcb)
            if link is not None:
                try:
                    place = link.placement()
                    if self._live_place is not None:
                        moved = [r for r, v in place.items() if r in self._live_place and
                                 (abs(v["x"] - self._live_place[r]["x"]) > 1e-3 or abs(v["y"] - self._live_place[r]["y"]) > 1e-3
                                  or abs(v["rot"] - self._live_place[r]["rot"]) > 0.01 or v["side"] != self._live_place[r]["side"])]
                        if moved and time.time() >= self.self_until:
                            self.hub.emit("board.live", moves={r: place[r] for r in moved})
                            self.user_changes.append("in KiCad (not saved yet): moved " + ", ".join(moved[:10]))
                    self._live_place = place
                    sel = link.selection()
                    if sel != self._live_sel:
                        self._live_sel = sel
                        self.selection["kicad"] = sel
                        self.hub.emit("live.selection", items=sel)
                except Exception as e:
                    out["reason"] = f"reading the open board failed: {e}"
        return out


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _placement_diff(before, after):
    out = []
    for ref, a in sorted(after.items()):
        b = before.get(ref)
        if b is None:
            out.append(f"added {ref}")
            continue
        if b == a:
            continue
        d = math.hypot(a[0] - b[0], a[1] - b[1])
        bits = []
        if d > 0.005:
            bits.append(f"moved {d:.2f} mm to ({a[0]:.2f}, {a[1]:.2f})")
        if abs((a[2] - b[2] + 180) % 360 - 180) > 0.01:
            bits.append(f"rotated to {a[2]:g}")
        if a[3] != b[3]:
            bits.append(f"flipped to {'bottom' if a[3] == 'B' else 'top'}")
        if bits:
            out.append(f"{ref} " + ", ".join(bits))
    for ref in sorted(set(before) - set(after)):
        out.append(f"removed {ref}")
    return out
