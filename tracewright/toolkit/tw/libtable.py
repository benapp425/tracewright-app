"""Resolve KiCad library nicknames ('Device:R', 'Resistor_SMD:R_0402_1005Metric') to files.

Reads the project's sym-lib-table / fp-lib-table, then the user's global tables (KiCad's config
folder), and expands ${KIPRJMOD}, ${KICADn_*_DIR} and other environment variables.
"""
import os, re, sys, glob
from .sexp import parse, findall, value
from . import env


def config_dir(major=None):
    major = major or env.kicad().get("major") or 10
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Preferences/kicad")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kicad")
    for v in (f"{major}.0", f"{major}.99"):
        d = os.path.join(base, v)
        if os.path.isdir(d):
            return d
    cands = sorted(glob.glob(os.path.join(base, "*.*")), reverse=True)
    return cands[0] if cands else os.path.join(base, f"{major}.0")


def _vars(project_dir):
    k = env.kicad()
    major = k.get("major") or 10
    v = dict(os.environ)
    v["KIPRJMOD"] = project_dir or ""
    share = k.get("share") or ""
    for n in range(6, major + 1):
        v.setdefault(f"KICAD{n}_SYMBOL_DIR", os.path.join(share, "symbols"))
        v.setdefault(f"KICAD{n}_FOOTPRINT_DIR", os.path.join(share, "footprints"))
        v.setdefault(f"KICAD{n}_3DMODEL_DIR", os.path.join(share, "3dmodels"))
        v.setdefault(f"KICAD{n}_TEMPLATE_DIR", os.path.join(share, "template"))
    v.setdefault("KICAD_USER_TEMPLATE_DIR", "")
    return v


def expand(uri, project_dir):
    vs = _vars(project_dir)
    return re.sub(r"\$\{([^}]+)\}", lambda m: vs.get(m.group(1), m.group(0)), uri)


def read_table(path, project_dir):
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            t = parse(f.read())
    except (OSError, ValueError):
        return out
    for lib in findall(t, "lib"):
        name, uri = value(lib, "name"), value(lib, "uri")
        if name and uri:
            out[str(name)] = expand(str(uri), project_dir)
    return out


class LibTables:
    def __init__(self, project_dir):
        self.dir = project_dir
        cfg = config_dir()
        self.fp = read_table(os.path.join(cfg, "fp-lib-table"), project_dir)
        self.fp.update(read_table(os.path.join(project_dir, "fp-lib-table"), project_dir))
        self.sym = read_table(os.path.join(cfg, "sym-lib-table"), project_dir)
        self.sym.update(read_table(os.path.join(project_dir, "sym-lib-table"), project_dir))
        share_fp = env.share_dir("footprints")
        share_sym = env.share_dir("symbols")
        # KiCad ships every stock library; fall back to them when the global table is missing
        if share_fp:
            for d in glob.glob(os.path.join(share_fp, "*.pretty")):
                self.fp.setdefault(os.path.splitext(os.path.basename(d))[0], d)
        if share_sym:
            for f in glob.glob(os.path.join(share_sym, "*.kicad_sym")):
                self.sym.setdefault(os.path.splitext(os.path.basename(f))[0], f)

    def footprint_dir(self, lib_id):
        lib = lib_id.split(":")[0] if ":" in lib_id else ""
        return self.fp.get(lib)

    def footprint_file(self, lib_id):
        d = self.footprint_dir(lib_id)
        if not d:
            return None
        f = os.path.join(d, lib_id.split(":")[-1] + ".kicad_mod")
        return f if os.path.exists(f) else None

    def symbol_file(self, lib_id):
        lib = lib_id.split(":")[0] if ":" in lib_id else ""
        return self.sym.get(lib)
