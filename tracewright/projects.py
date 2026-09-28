"""Projects: one folder each, with the KiCad files, the toolkit, the agent's design scripts and docs.

    store = ProjectStore()
    p = store.create("Sensor node", brief="ESP32-C3 board with ...")
    p = store.import_copy("/path/to/kicad/project")     # copied into the workspace
    p = store.open_in_place("/path/to/kicad/project")   # worked on where it is
"""
import os, re, json, time, shutil, datetime, zipfile, glob, threading
from . import config, history
from tw import env as twenv

STAGES = [
    ("brief", "Brief", "Requirements, constraints, interfaces, budget -- written down and agreed"),
    ("architecture", "Architecture", "Block diagram, power tree, key part choices and the reasons"),
    ("parts", "Parts", "Every part chosen with MPN / LCSC, stock checked, symbol and footprint verified"),
    ("schematic", "Schematic", "Captured, ERC clean, readable, reviewed"),
    ("board_setup", "Board setup", "Outline, stack-up, net classes, fab rules, mounting, connector positions"),
    ("placement", "Placement", "Every part placed with intent: connectors, power stages, decoupling, keepouts"),
    ("routing", "Routing", "All nets routed, pours, DRC clean, power widths and pairs checked"),
    ("verification", "Verification", "Every check passes or is waived with a reason; review and bring-up plan written"),
    ("release", "Release", "Fab outputs, BOM/CPL against JLC, release zip"),
]
STAGE_IDS = [s[0] for s in STAGES]
# how Claude works with the user: "autonomous" asks every question up front (the intake), then carries
# the design through without waiting, recording each decision it takes; "check_in" stops at each gate
RUN_MODES = ("autonomous", "check_in")
PKG = os.path.dirname(os.path.abspath(__file__))
TOOLKIT_SRC = os.path.join(PKG, "toolkit", "tw")
TEMPLATES = os.path.join(PKG, "templates", "project")
IGNORE_COPY = shutil.ignore_patterns("__pycache__", "*.pyc", "libastar.so", ".DS_Store")


def slugify(name):
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").lower()
    return s[:48] or "project"


def kicad_stem(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")
    return s[:40] or "board"


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class Project:
    """A project folder as the app sees it."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._lock = threading.RLock()
        self.reload()

    def reload(self):
        path = os.path.join(self.root, twenv.CONFIG)
        try:
            with open(path) as f:
                self.cfg = json.load(f)
        except (OSError, ValueError):
            self.cfg = {}
        self.tw = twenv.Project(self.root, self.cfg)
        return self

    @property
    def id(self):
        return self.cfg.get("id") or os.path.basename(self.root)

    @property
    def name(self):
        return self.cfg.get("name") or self.tw.name

    def save(self):
        with self._lock:
            path = os.path.join(self.root, twenv.CONFIG)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.cfg, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
            self.tw = twenv.Project(self.root, self.cfg)

    def state_dir(self, *p):
        d = os.path.join(self.root, ".tracewright", *p)
        os.makedirs(d, exist_ok=True)
        return d

    # ------------------------------------------------------------------ stages
    def stages(self):
        st = self.cfg.setdefault("stages", {})
        out = []
        for sid, title, desc in STAGES:
            s = st.get(sid, {})
            out.append({"id": sid, "title": title, "description": desc, "status": s.get("status", "todo"),
                        "note": s.get("note", ""), "updated": s.get("updated", "")})
        return out

    def run_mode(self):
        m = self.cfg.get("run_mode")
        return m if m in RUN_MODES else "autonomous"

    def intake_done(self):
        """The requirements are agreed: stage brief is done, or the work has moved past it (an imported
        design gets its stages from its files). A guided start is in the intake until the user presses
        Start."""
        from . import canvas
        if canvas.phase(self.cfg):
            return False
        st = self.cfg.get("stages", {})
        if st.get("brief", {}).get("status") in ("done", "skipped"):
            return True
        return any(v.get("status") in ("active", "done") for k, v in st.items() if k in STAGE_IDS and k != "brief")

    def unattended(self):
        """Is Claude to work without waiting on the user now (autonomous mode, past the intake)?"""
        return self.run_mode() == "autonomous" and self.intake_done()

    def set_stage(self, sid, status, note=""):
        if sid not in STAGE_IDS:
            raise ValueError(f"unknown stage {sid}; stages: {', '.join(STAGE_IDS)}")
        if status not in ("todo", "active", "done", "blocked", "skipped"):
            raise ValueError("status must be todo, active, done, blocked or skipped")
        with self._lock:
            st = self.cfg.setdefault("stages", {})
            st[sid] = {"status": status, "note": note, "updated": now()}
            if status == "active":                      # one active stage at a time: moving on finishes the
                i = STAGE_IDS.index(sid)                # earlier stage, going back reopens the later one
                for k, v in st.items():
                    if k != sid and v.get("status") == "active":
                        v["status"] = "done" if k in STAGE_IDS and STAGE_IDS.index(k) < i else "todo"
            self.save()
        return self.stages()

    # ------------------------------------------------------------------ files
    def brief(self):
        p = os.path.join(self.root, "BRIEF.md")
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def updated(self):
        ts = [os.path.getmtime(f) for f in (self.tw.sch, self.tw.pcb, os.path.join(self.root, twenv.CONFIG))
              if f and os.path.exists(f)]
        return datetime.datetime.fromtimestamp(max(ts)).isoformat(timespec="seconds") if ts else self.cfg.get("created", "")

    def checks_summary(self):
        f = os.path.join(self.tw.build, "checks.json")
        if not os.path.exists(f):
            return None
        try:
            with open(f) as fh:
                d = json.load(fh)
            from tw.checks.runner import verdict
            return {"verdict": verdict(d), "counts": d["counts"], "generated": d["generated"]}
        except (OSError, ValueError, KeyError):
            return None

    def thumbnail(self):
        for f in (os.path.join(self.root, ".tracewright", "thumb.png"),
                  os.path.join(self.tw.build, "images", f"{self.tw.stem}-top.png")):
            if f and os.path.exists(f):
                return f
        return None

    def summary(self):
        return {"id": self.id, "name": self.name, "root": self.root, "kind": self.cfg.get("kind", "new"),
                "created": self.cfg.get("created", ""), "updated": self.updated(),
                "has_sch": self.tw.has_sch(), "has_pcb": self.tw.has_pcb(),
                "kicad_project": os.path.relpath(self.tw.pro, self.root) if self.tw.pro else None,
                "stages": self.stages(), "checks": self.checks_summary(), "thumbnail": bool(self.thumbnail()),
                "description": self.cfg.get("description", ""), "archived": self.cfg.get("archived", False),
                "toolkit": self.cfg.get("toolkit"), "fab": self.cfg.get("fab", {}), "run_mode": self.run_mode(),
                "unattended": self.unattended(), "start_phase": self.start_phase()}

    def start_phase(self):
        """'intake' | 'ready' while a guided start waits for the user's Start; None otherwise."""
        from . import canvas
        return canvas.phase(self.cfg)

    def set_start_phase(self, phase):
        """Move a guided start on: 'ready' (the intake is done, the Start card shows) or 'done'."""
        from . import canvas
        if not canvas.start_of(self.cfg) or phase not in ("intake", "ready", "done"):
            return False
        with self._lock:
            self.cfg["start"] = {**self.cfg["start"], "phase": phase, "updated": now()}
        self.save()
        return True


def infer_stages(root, kicad_project, drc_out=None):
    """A first reading of where an existing design is, from its files: what the files show is marked
    done ("in the imported files"), the next step is active. The import review corrects it."""
    pro = os.path.join(root, kicad_project)
    stem, d = os.path.splitext(pro)[0], os.path.dirname(pro)

    def read(f):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""
    parts = sum(read(f).count("(lib_id ") for f in glob.glob(os.path.join(d, "*.kicad_sch")))
    board = read(stem + ".kicad_pcb")
    fps = board.count("(footprint ")
    tracks = board.count("(segment") + board.count("(arc")          # board / footprint arcs are gr_arc / fp_arc
    unrouted = None
    if tracks and drc_out:
        try:
            from tw import kicad
            unrouted = len(kicad.drc(stem + ".kicad_pcb", drc_out, parity=False).get("unconnected_items", []))
        except Exception:
            pass
    note = ""
    if tracks and unrouted == 0:
        active, note = "verification", "routed board imported: run every check"
    elif tracks:
        active = "routing"
        note = f"{unrouted} connections left to route" if unrouted else "partly routed"
    elif fps:
        active = "placement"
    elif parts:
        active = "schematic"
    else:
        active = "brief"
    out, t = {}, now()
    for sid in STAGE_IDS:
        if sid == active:
            out[sid] = {"status": "active", "note": note, "updated": t}
            break
        if sid == "schematic" and not parts:
            out[sid] = {"status": "skipped", "note": "no schematic in the import", "updated": t}
        else:
            out[sid] = {"status": "done", "note": "in the imported files", "updated": t}
    return out


class ProjectStore:
    def __init__(self):
        self.reg_path = os.path.join(config.data_dir(), "projects.json")
        self._cache = {}

    # ------------------------------------------------------------------ registry of in-place projects
    def _registry(self):
        try:
            with open(self.reg_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"in_place": {}}

    def _save_registry(self, reg):
        with open(self.reg_path, "w") as f:
            json.dump(reg, f, indent=2)

    def roots(self):
        ws = config.workspace()
        out = []
        for d in sorted(os.listdir(ws)):
            p = os.path.join(ws, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, twenv.CONFIG)):
                out.append(p)
        for pid, path in self._registry().get("in_place", {}).items():
            if os.path.exists(os.path.join(path, twenv.CONFIG)):
                out.append(path)
        return out

    def list(self, include_archived=True):
        out = []
        for r in self.roots():
            try:
                p = self.get_by_root(r)
                s = p.summary()
                if include_archived or not s["archived"]:
                    out.append(s)
            except Exception as e:
                out.append({"id": os.path.basename(r), "name": os.path.basename(r), "root": r, "error": str(e)})
        out.sort(key=lambda s: s.get("updated", ""), reverse=True)
        return out

    def get_by_root(self, root):
        root = os.path.abspath(root)
        p = self._cache.get(root)
        if p is None:
            p = self._cache[root] = Project(root)
        else:
            p.reload()
        return p

    def get(self, pid):
        for r in self.roots():
            p = self.get_by_root(r)
            if p.id == pid:
                return p
        raise KeyError(f"no project {pid}")

    def _unique_dir(self, slug):
        ws = config.workspace()
        root = os.path.join(ws, slug)
        n = 2
        while os.path.exists(root):
            root = os.path.join(ws, f"{slug}-{n}")
            n += 1
        return root

    # ------------------------------------------------------------------ create / import
    def create(self, name, brief="", options=None):
        """A new project from a prompt: folders, an empty KiCad project, the toolkit, the agent's
        instructions and skills, and a first checkpoint."""
        from . import scaffold
        options = options or {}
        root = self._unique_dir(slugify(name))
        os.makedirs(root)
        stem = kicad_stem(options.get("kicad_name") or name)
        cfg = {"schema": 1, "id": os.path.basename(root), "name": name, "created": now(), "kind": "new",
               "kicad_project": f"hardware/{stem}/{stem}.kicad_pro",
               "description": (brief.strip().splitlines() or [""])[0][:160],
               "fab": {"house": options.get("fab_house", config.settings().get("fab_house", "jlcpcb")),
                       "layers": int(options.get("layers", 2)), "assembly": bool(options.get("assembly", True))},
               "checks": {}, "stages": {"brief": {"status": "active", "note": "", "updated": now()}},
               "run_mode": options.get("run_mode") if options.get("run_mode") in RUN_MODES else "autonomous"}
        if options.get("workflow") == "guided":
            cfg["start"] = {"mode": "guided", "phase": "intake", "since": now()}
        scaffold.new_project(root, cfg, brief)
        return self.get_by_root(root)

    def import_copy(self, src, name=None, options=None):
        """Copy an existing KiCad project (folder, .kicad_pro, .zip, or an Altium/Eagle/... board) into
        the workspace and add the toolkit around it. The original is not touched."""
        from . import scaffold
        src = os.path.abspath(os.path.expanduser(src))
        if not os.path.exists(src):
            raise FileNotFoundError(src)
        tmp_extract = None
        if src.lower().endswith(".zip"):
            tmp_extract = os.path.join(config.data_dir(), "tmp", f"import-{int(time.time())}")
            os.makedirs(tmp_extract, exist_ok=True)
            with zipfile.ZipFile(src) as z:
                z.extractall(tmp_extract)
            src = tmp_extract
        foreign = None
        whole_folder = os.path.isdir(src)            # a folder: bring all of it (docs, scripts, sourcing ...)
        if os.path.isfile(src):
            ext = os.path.splitext(src)[1].lower()
            if ext in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
                src = os.path.dirname(src)
            else:
                foreign = src                              # Altium / Eagle / PADS / ... board
        pro = twenv._find_pro(src) if not foreign else None
        if not foreign and not pro:
            raise ValueError("no .kicad_pro found in that folder (for other tools, choose the board file itself)")
        base = name or (os.path.basename(src.rstrip(os.sep)) if whole_folder else
                        os.path.splitext(os.path.basename(pro))[0] if pro else os.path.splitext(os.path.basename(foreign))[0])
        root = self._unique_dir(slugify(base))
        skip = shutil.ignore_patterns(".git", "*-backups", "__pycache__", "*.lck", "fp-info-cache", ".DS_Store", "node_modules",
                                      ".tracewright", "*.pyc")
        if foreign:
            os.makedirs(root)
            stem = kicad_stem(base)
            hw = os.path.join(root, "hardware", stem)
            os.makedirs(hw)
            from tw import kicad
            kicad.pcb_import(foreign, os.path.join(hw, stem + ".kicad_pcb"))
            scaffold.write_kicad_pro(os.path.join(hw, stem + ".kicad_pro"), stem)
            rel = f"hardware/{stem}/{stem}.kicad_pro"
            srcnote = foreign
        elif whole_folder:
            def ignore(d, names):
                out = set(skip(d, names))
                if os.path.abspath(d) == os.path.abspath(src) and "build" in names:
                    out.add("build")                     # regenerable outputs stay behind
                return out
            shutil.copytree(src, root, ignore=ignore)
            rel = os.path.relpath(pro, src)
            srcnote = src
        else:
            os.makedirs(root)
            pdir = os.path.dirname(pro)
            dest = os.path.join(root, "hardware", os.path.basename(pdir) or os.path.splitext(os.path.basename(pro))[0])
            shutil.copytree(pdir, dest, ignore=skip)
            rel = os.path.relpath(os.path.join(dest, os.path.basename(pro)), root)
            srcnote = pdir
        cfg = {"schema": 1, "id": os.path.basename(root), "name": name or base, "created": now(), "kind": "imported",
               "source": srcnote, "kicad_project": rel, "fab": {"house": config.settings().get("fab_house", "jlcpcb"),
                                                               "assembly": True},
               "checks": {}, "stages": {}}
        existing = os.path.join(root, twenv.CONFIG)
        if os.path.exists(existing):                 # a Tracewright project from elsewhere: keep its settings,
            try:                                     # give the copy its own identity
                with open(existing) as f:
                    old = json.load(f)
                for k in ("fab", "checks", "stages", "description", "hardware_checks", "routing", "highspeed", "revision"):
                    if k in old:
                        cfg[k] = old[k]
            except (OSError, ValueError):
                pass
            os.remove(existing)
        cfg.update((options or {}).get("cfg", {}))
        if not cfg.get("stages"):
            cfg["stages"] = infer_stages(root, rel, os.path.join(root, "build", "drc.json"))
        scaffold.existing_project(root, cfg, shadow=False)
        if tmp_extract:
            shutil.rmtree(tmp_extract, ignore_errors=True)
        return self.get_by_root(root)

    def import_git(self, url, name=None):
        """Clone a KiCad project from GitHub (or any git URL) into the workspace, keeping its history
        and its link to the repository, and add the toolkit around it."""
        from . import scaffold, github
        base = name or url.rstrip("/").split("/")[-1].removesuffix(".git") or "project"
        root = self._unique_dir(slugify(base))
        github.clone(url, root)
        pro = twenv._find_pro(root)
        if not pro:
            shutil.rmtree(root, ignore_errors=True)
            raise ValueError("no .kicad_pro in that repository")
        rel = os.path.relpath(pro, root)
        cfg = {"schema": 1, "id": os.path.basename(root), "name": name or base, "created": now(), "kind": "imported",
               "source": url, "kicad_project": rel, "fab": {"house": config.settings().get("fab_house", "jlcpcb"),
                                                            "assembly": True}, "checks": {}, "stages": {}}
        existing = os.path.join(root, twenv.CONFIG)
        if os.path.exists(existing):                 # a Tracewright project from elsewhere: keep its design settings
            try:
                with open(existing) as f:
                    old = json.load(f)
                for k in ("fab", "checks", "stages", "description", "hardware_checks", "routing", "highspeed",
                          "revision", "run_mode"):
                    if k in old:
                        cfg[k] = old[k]
            except (OSError, ValueError):
                pass
            os.remove(existing)
        if not cfg.get("stages"):
            cfg["stages"] = infer_stages(root, rel, os.path.join(root, "build", "drc.json"))
        cfg["github"] = {"repo": "/".join(url.rstrip("/").replace(":", "/").split("/")[-2:]).removesuffix(".git"),
                         "url": url.removesuffix(".git") if url.startswith("https://") else url, "remote": url,
                         "auto_push": True}
        scaffold.existing_project(root, cfg, shadow=False)
        return self.get_by_root(root)

    def open_in_place(self, src, name=None):
        """Work on a KiCad project where it is. Adds tracewright.json, tw, tools/tw, CLAUDE.md, .claude/
        and .tracewright/ next to it; the user's own git is left alone (checkpoints go to a shadow repo)."""
        from . import scaffold
        src = os.path.abspath(os.path.expanduser(src))
        if os.path.isfile(src):
            src = os.path.dirname(src)
        pro = twenv._find_pro(src)
        if not pro:
            raise ValueError("no .kicad_pro found in that folder")
        root = src
        if os.path.exists(os.path.join(root, twenv.CONFIG)):
            p = self.get_by_root(root)
        else:
            stem = os.path.splitext(os.path.basename(pro))[0]
            cfg = {"schema": 1, "id": slugify(name or stem) + "-" + format(abs(hash(root)) % 10000, "04d"),
                   "name": name or stem, "created": now(), "kind": "in_place", "kicad_project": os.path.relpath(pro, root),
                   "fab": {"house": config.settings().get("fab_house", "jlcpcb"), "assembly": True},
                   "checks": {}, "stages": {}}
            tmp = os.path.join(config.data_dir(), "tmp", f"drc-{os.getpid()}-{int(time.time())}.json")
            cfg["stages"] = infer_stages(root, cfg["kicad_project"], tmp)
            if os.path.exists(tmp):
                os.remove(tmp)
            scaffold.existing_project(root, cfg, shadow=True)
            p = self.get_by_root(root)
        reg = self._registry()
        reg.setdefault("in_place", {})[p.id] = root
        self._save_registry(reg)
        return p

    def remove(self, pid, delete_files=False):
        """Forget a project; with delete_files, move its folder to the data folder's trash."""
        p = self.get(pid)
        reg = self._registry()
        reg.get("in_place", {}).pop(pid, None)
        self._save_registry(reg)
        self._cache.pop(p.root, None)
        if delete_files and p.cfg.get("kind") != "in_place":
            trash = os.path.join(config.data_dir(), "trash")
            os.makedirs(trash, exist_ok=True)
            dest = os.path.join(trash, f"{os.path.basename(p.root)}-{int(time.time())}")
            shutil.move(p.root, dest)
            return dest
        if not delete_files and p.cfg.get("kind") != "in_place":
            p.cfg["archived"] = True
            p.save()
        return None
