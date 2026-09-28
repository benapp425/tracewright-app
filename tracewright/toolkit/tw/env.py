"""Where KiCad lives on this machine, and where this project's files are.

KiCad discovery (macOS, Linux, Windows) can be overridden with environment variables:
  TW_KICAD_CLI      path to kicad-cli
  TW_KICAD_PYTHON   a Python that can `import pcbnew` (KiCad's bundled one on macOS / Windows)
  TW_KICAD_SHARE    KiCad's shared-support folder (symbols/, footprints/, 3dmodels/, template/)

A project is the folder holding tracewright.json (see `Project`). Without one, the nearest folder
with a single .kicad_pro is used, so the toolkit also works on a plain KiCad project.
"""
import os, sys, json, glob, shutil, subprocess, functools

CONFIG = "tracewright.json"


# ----------------------------------------------------------------------------- KiCad install
def _first(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _mac_apps():
    out = []
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        out += sorted(glob.glob(os.path.join(base, "KiCad*", "KiCad.app")), reverse=True)
        out.append(os.path.join(base, "KiCad.app"))
    return out


@functools.lru_cache(maxsize=1)
def kicad():
    """{'cli', 'python', 'share', 'version', 'major', 'app'}; missing parts are None."""
    cli = os.environ.get("TW_KICAD_CLI")
    py = os.environ.get("TW_KICAD_PYTHON")
    share = os.environ.get("TW_KICAD_SHARE")
    app = None
    if sys.platform == "darwin":
        for a in _mac_apps():
            c = os.path.join(a, "Contents", "MacOS", "kicad-cli")
            if os.path.exists(c):
                app = a
                cli = cli or c
                py = py or _first([os.path.join(a, "Contents", "Frameworks", "Python.framework", "Versions",
                                                "Current", "bin", "python3")])
                share = share or _first([os.path.join(a, "Contents", "SharedSupport")])
                break
    elif os.name == "nt":
        roots = sorted(glob.glob(r"C:\Program Files\KiCad\*"), reverse=True)
        for r in roots:
            c = os.path.join(r, "bin", "kicad-cli.exe")
            if os.path.exists(c):
                app = r
                cli = cli or c
                py = py or _first([os.path.join(r, "bin", "python.exe")])
                share = share or _first([os.path.join(r, "share", "kicad")])
                break
    else:
        cli = cli or shutil.which("kicad-cli")
        share = share or _first(["/usr/share/kicad", "/usr/local/share/kicad",
                                 "/app/share/kicad"])            # flatpak
        if not py:
            for cand in ("/usr/bin/python3", shutil.which("python3")):
                if cand and _imports_pcbnew(cand):
                    py = cand
                    break
    cli = cli or shutil.which("kicad-cli")
    version = None
    if cli:
        try:
            version = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=30,
                                     cwd=os.path.expanduser("~")).stdout.strip()
        except Exception:
            version = None
    major = None
    if version:
        try:
            major = int(version.split(".")[0])
        except ValueError:
            major = None
    return {"cli": cli, "python": py, "share": share, "version": version, "major": major, "app": app}


def _imports_pcbnew(py):
    try:
        r = subprocess.run([py, "-c", "import pcbnew"], capture_output=True, timeout=30, cwd=os.path.expanduser("~"))
        return r.returncode == 0
    except Exception:
        return False


def share_dir(kind):
    """symbols | footprints | 3dmodels | template folder of the KiCad install."""
    s = kicad()["share"]
    if not s:
        return None
    p = os.path.join(s, kind)
    return p if os.path.isdir(p) else None


def require(what="cli"):
    k = kicad()
    if not k.get(what):
        hint = {"cli": "Install KiCad 9 or 10, or set TW_KICAD_CLI to kicad-cli.",
                "python": "Set TW_KICAD_PYTHON to a Python that can `import pcbnew` (KiCad's bundled Python)."}
        raise SystemExit(f"KiCad {what} not found. {hint.get(what, '')}")
    return k[what]


# ----------------------------------------------------------------------------- the project
class Project:
    """Paths of one design project.

    root         the project folder (holds tracewright.json)
    pro          the KiCad project file (.kicad_pro)
    hw           the folder of the KiCad project
    sch, pcb     root schematic and board (same stem as the .kicad_pro)
    build        generated outputs (reports, renders, fab files)
    cfg          tracewright.json contents (dict)
    """

    def __init__(self, root, cfg=None):
        self.root = os.path.abspath(root)
        self.cfg = cfg if cfg is not None else _read_json(os.path.join(self.root, CONFIG)) or {}
        pro = self.cfg.get("kicad_project")
        if pro:
            pro = os.path.join(self.root, pro)
        else:
            pro = _find_pro(self.root)
        self.pro = pro
        self.hw = os.path.dirname(pro) if pro else self.root
        stem = os.path.splitext(os.path.basename(pro))[0] if pro else None
        self.name = self.cfg.get("name") or stem or os.path.basename(self.root)
        self.stem = stem
        self.sch = os.path.join(self.hw, stem + ".kicad_sch") if stem else None
        self.pcb = os.path.join(self.hw, stem + ".kicad_pcb") if stem else None
        self.build = os.path.join(self.root, self.cfg.get("build_dir", "build"))

    # convenience
    def path(self, *p):
        return os.path.join(self.root, *p)

    def out(self, *p):
        """A path under build/, creating its folder."""
        f = os.path.join(self.build, *p)
        os.makedirs(os.path.dirname(f) if os.path.splitext(f)[1] else f, exist_ok=True)
        return f

    def has_sch(self):
        return bool(self.sch and os.path.exists(self.sch))

    def has_pcb(self):
        return bool(self.pcb and os.path.exists(self.pcb))

    def setting(self, dotted, default=None):
        """tracewright.json lookup: setting('fab.lcsc_field', 'LCSC')."""
        d = self.cfg
        for k in dotted.split("."):
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d

    def save_cfg(self):
        with open(os.path.join(self.root, CONFIG), "w") as f:
            json.dump(self.cfg, f, indent=2)
            f.write("\n")

    def sheets(self):
        """All .kicad_sch files of the project folder."""
        return sorted(glob.glob(os.path.join(self.hw, "*.kicad_sch")))

    def __repr__(self):
        return f"Project({self.name!r}, {self.root!r})"


def _read_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _find_pro(root):
    """The .kicad_pro in root, else in hardware/*/ or any subfolder (shallowest wins)."""
    cands = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("build", "tools", "node_modules",
                                                                          "backups", "__pycache__")]
        depth = dirpath[len(root):].count(os.sep)
        if depth > 3:
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".kicad_pro"):
                cands.append((depth, os.path.join(dirpath, f)))
    return sorted(cands)[0][1] if cands else None


def find_root(start=None):
    """The project folder: nearest ancestor with tracewright.json; else the ancestor of tools/tw;
    else the nearest ancestor with a .kicad_pro."""
    here = os.path.abspath(start or os.getcwd())
    d = here
    while True:
        if os.path.exists(os.path.join(d, CONFIG)):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    tw_dir = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.dirname(os.path.dirname(tw_dir))            # <root>/tools/tw -> <root>
    if os.path.basename(os.path.dirname(tw_dir)) == "tools" and (
            os.path.exists(os.path.join(cand, CONFIG)) or _find_pro(cand)):
        return cand
    d = here
    while True:
        if glob.glob(os.path.join(d, "*.kicad_pro")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            return here
        d = nd


def project(start=None):
    return Project(find_root(start))
