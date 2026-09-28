"""Lay out a project folder: KiCad skeleton, toolkit, agent instructions, skills, knowledge, docs."""
import os, filecmp, re, sys, json, shutil, uuid, datetime, glob, hashlib, functools
from . import history, knowledge
from tw import env as twenv, kicad, dfm, __version__ as TW_VERSION

PKG = os.path.dirname(os.path.abspath(__file__))
TOOLKIT_SRC = os.path.join(PKG, "toolkit", "tw")
TPL = os.path.join(PKG, "templates", "project")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "libastar.so", ".DS_Store", "fixtures_out")

GITIGNORE = """# Tracewright
build/
sourcing/cache/
.tracewright/sessions/
.tracewright/tmp/
.tracewright/cache/
.tracewright/timelapse/
.tracewright/python
.tracewright/history.git/
# KiCad
*-backups/
*.kicad_prl
fp-info-cache
~*.lck
*.bak
# Python
__pycache__/
*.pyc
tools/tw/route/libastar.so
"""

LAUNCHER_SH = """#!/bin/sh
# Tracewright toolkit for this project. Run ./tw with no arguments for the command list.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${TW_PYTHON:-}"
if [ -z "$PY" ] && [ -f "$DIR/.tracewright/python" ]; then PY="$(cat "$DIR/.tracewright/python")"; fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi
# the app's Python runs in the CPU architecture its native packages were installed for (macOS)
if [ -z "$TW_PYTHON" ] && [ -f "$DIR/.tracewright/arch" ] && [ -x /usr/bin/arch ] && \
   [ "$PY" = "$(cat "$DIR/.tracewright/python" 2>/dev/null)" ]; then
  exec /usr/bin/arch -"$(cat "$DIR/.tracewright/arch")" "$PY" "$DIR/tools/tw/cli.py" "$@"
fi
exec "$PY" "$DIR/tools/tw/cli.py" "$@"
"""

LAUNCHER_CMD = """@echo off
rem Tracewright toolkit for this project. Run tw with no arguments for the command list.
set DIR=%~dp0
set PY=python
if exist "%DIR%.tracewright\\python" set /p PY=<"%DIR%.tracewright\\python"
"%PY%" "%DIR%tools\\tw\\cli.py" %*
"""


def _write(path, text, overwrite=True):
    if os.path.exists(path) and not overwrite:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def _render(tpl, **vars_):
    with open(os.path.join(TPL, tpl), encoding="utf-8") as f:
        t = f.read()
    for k, v in vars_.items():
        t = t.replace("{{" + k + "}}", str(v))
    return t


@functools.lru_cache(maxsize=1)
def toolkit_fingerprint():
    """A hash of what install_toolkit puts in a project -- the toolkit files, the launchers, and the
    Python they run (path and architecture): a project with a different one is out of date (the
    version number alone does not change between builds)."""
    h = hashlib.sha1()
    h.update(f"{LAUNCHER_SH}{LAUNCHER_CMD}{sys.executable}{wheel_arch()}".encode())
    for top in (TOOLKIT_SRC, TPL, os.path.dirname(knowledge.__file__)):   # + the agent files it writes
        for dp, dn, fn in os.walk(top):
            dn[:] = sorted(d for d in dn if d not in ("__pycache__", "fixtures_out"))
            for f in sorted(fn):
                if f.endswith((".pyc", ".so")) or f == ".DS_Store":
                    continue
                full = os.path.join(dp, f)
                h.update(os.path.relpath(full, top).encode())
                with open(full, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()[:16]


def toolkit_outdated(root):
    """Does this project's tools/tw differ from the app's toolkit?"""
    try:
        with open(os.path.join(root, ".tracewright", "toolkit.sha")) as f:
            return f.read().strip() != toolkit_fingerprint()
    except OSError:
        return os.path.isdir(os.path.join(root, "tools", "tw"))    # installed before fingerprints existed


def wheel_arch():
    """macOS: the CPU architecture this Python's native packages were built for ('arm64' / 'x86_64'),
    read from a compiled module's Mach-O header -- right even when this process runs under Rosetta.
    None elsewhere, or for universal builds."""
    if sys.platform != "darwin":
        return None
    import importlib.util, struct
    for pkg in ("PIL", "numpy"):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            continue
        if not spec or not spec.submodule_search_locations:
            continue
        base = list(spec.submodule_search_locations)[0]
        for so in sorted(glob.glob(os.path.join(base, "*.so")) + glob.glob(os.path.join(base, "_core", "*.so"))):
            try:
                with open(so, "rb") as f:
                    magic, cpu = struct.unpack("<II", f.read(8))
            except (OSError, struct.error):
                continue
            if magic == 0xFEEDFACF:
                return {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(cpu)
            return None                                        # a universal ("fat") build runs either way
    return None


# ----------------------------------------------------------------------------- copies that sync well
# A folder deleted and written again at once makes iCloud Drive (a Documents folder synced to iCloud)
# keep both, as "checks 3", "routing 3", "INDEX 2.md": found in every project on 2026-09-27, including
# duplicated Claude skills. So the app's copies are updated in place, and such conflict copies of the
# app's own files are removed.
CONFLICT = re.compile(r"^(?P<base>.+?) \d{1,2}(?P<ext>\.[^./ ]+)?$")


def _same_file(a, b):
    try:
        return os.path.getsize(a) == os.path.getsize(b) and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def mirror(src, dst, ignore=None):
    """Make dst match src without removing and recreating it: changed files rewritten in place, new
    ones added, anything src does not have removed (what `ignore` matches in dst -- build products,
    caches -- is left alone)."""
    os.makedirs(dst, exist_ok=True)
    names = os.listdir(src)
    skip = set(ignore(src, names)) if ignore else set()
    want = [n for n in names if n not in skip]
    for n in want:
        s, d = os.path.join(src, n), os.path.join(dst, n)
        if os.path.isdir(s):
            if os.path.lexists(d) and not os.path.isdir(d):
                os.remove(d)
            mirror(s, d, ignore)
        else:
            if os.path.isdir(d) and not os.path.islink(d):
                shutil.rmtree(d)
            if not os.path.exists(d) or not _same_file(s, d):
                shutil.copy2(s, d)
    have = os.listdir(dst)
    keep = set(ignore(dst, have)) if ignore else set()
    for n in have:
        if n in want or n in keep:
            continue
        p = os.path.join(dst, n)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass


def write_tree(dst, files):
    """dst holds exactly {name: text}: files rewritten only when they differ, the rest removed."""
    os.makedirs(dst, exist_ok=True)
    for name, text in files.items():
        p = os.path.join(dst, name)
        try:
            with open(p, encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    for n in os.listdir(dst):
        if n not in files and not os.path.isdir(os.path.join(dst, n)):
            try:
                os.remove(os.path.join(dst, n))
            except OSError:
                pass


def clean_conflicts(root):
    """Remove iCloud conflict copies ("pcb 3", "routing 3", "INDEX 2.md") inside the folders the app
    writes (tools/tw, .claude/skills, .claude/knowledge): only where the original is next to it.
    Returns what was removed (relative paths)."""
    gone = []
    for base in (os.path.join(root, "tools", "tw"), os.path.join(root, ".claude", "skills"), os.path.join(root, ".claude", "knowledge")):
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            for n in list(dns) + list(fns):
                m = CONFLICT.match(n)
                if not m or not os.path.exists(os.path.join(dp, m.group("base") + (m.group("ext") or ""))):
                    continue
                p = os.path.join(dp, n)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    dns.remove(n)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        continue
                gone.append(os.path.relpath(p, root))
    return gone


def install_toolkit(root):
    dest = os.path.join(root, "tools", "tw")
    if os.path.islink(dest):
        os.unlink(dest)
    mirror(TOOLKIT_SRC, dest, ignore=IGNORE)
    _write(os.path.join(root, "tw"), LAUNCHER_SH)
    os.chmod(os.path.join(root, "tw"), 0o755)
    _write(os.path.join(root, "tw.cmd"), LAUNCHER_CMD)
    st = os.path.join(root, ".tracewright")
    os.makedirs(st, exist_ok=True)
    with open(os.path.join(st, "python"), "w") as f:
        f.write(sys.executable)
    arch, archf = wheel_arch(), os.path.join(st, "arch")
    if arch:
        with open(archf, "w") as f:
            f.write(arch)
    elif os.path.exists(archf):
        os.remove(archf)
    with open(os.path.join(st, "toolkit.sha"), "w") as f:
        f.write(toolkit_fingerprint())
    return TW_VERSION


def install_agent_files(root, cfg, overwrite_claude=True):
    """CLAUDE.md (+ .claude/tracewright.md, skills, knowledge): the agent's instructions travel with the
    project, so Claude Code run directly in the folder works the same as inside the app."""
    name = cfg.get("name", os.path.basename(root))
    pb = knowledge.playbook_text()
    tref = _render("toolkit_reference.md")
    _write(os.path.join(root, ".claude", "tracewright.md"), pb + "\n\n" + tref)
    claude = os.path.join(root, "CLAUDE.md")
    if overwrite_claude or not os.path.exists(claude):
        _write(claude, _render("CLAUDE.md", name=name, kicad_project=cfg.get("kicad_project", "")))
    skills_src = os.path.join(TPL, "skills")
    skills_dst = os.path.join(root, ".claude", "skills")
    if os.path.isdir(skills_src):
        mirror(skills_src, skills_dst, ignore=IGNORE)
    files = {les["file"]: les["raw"] for les in knowledge.all_lessons()}
    files["INDEX.md"] = knowledge.index_text()
    write_tree(os.path.join(root, ".claude", "knowledge"), files)
    # Claude Code's own permissions when run directly in the folder (the app sets its own)
    settings = os.path.join(root, ".claude", "settings.json")
    if not os.path.exists(settings):
        _write(settings, json.dumps({"permissions": {"allow": ["Bash(./tw:*)", "Bash(python3 tools/tw/cli.py:*)",
                                                              "Read", "Edit", "Write", "Glob", "Grep"]}}, indent=2) + "\n")


def docs_skeleton(root, cfg, brief):
    d = os.path.join(root, "docs")
    own = os.path.isdir(d) and any(f.lower().endswith(".md") for f in os.listdir(d))
    if not own:                                      # a project with its own docs keeps just those
        for name in ("requirements.md", "architecture.md", "decisions.md", "parts.md", "bring-up.md", "review.md"):
            _write(os.path.join(d, name), _render(os.path.join("docs", name), name=cfg.get("name", "")), overwrite=False)
    _write(os.path.join(root, "design", "README.md"), _render("design_README.md"), overwrite=False)
    _write(os.path.join(root, "design", "checks", "README.md"), _render("design_checks_README.md"), overwrite=False)


def write_kicad_pro(path, stem, fab=None):
    """A .kicad_pro from KiCad's default template, with the fab's limits as the board minimums."""
    tpl = None
    tdir = twenv.share_dir("template")
    if tdir and os.path.exists(os.path.join(tdir, "kicad.kicad_pro")):
        tpl = os.path.join(tdir, "kicad.kicad_pro")
    data = {}
    if tpl:
        with open(tpl) as f:
            data = json.load(f)
    data.setdefault("meta", {})["filename"] = os.path.basename(path)
    fab = fab or {}
    prof = dfm.profile(fab.get("house", "jlcpcb"), int(fab.get("layers", 2)))
    board = data.setdefault("board", {})
    ds = board.setdefault("design_settings", {})
    rules = ds.setdefault("rules", {})
    rules.update({"min_clearance": prof["rec_space"], "min_track_width": prof["rec_track"],
                  "min_via_diameter": prof["min_via_diameter"], "min_through_hole_diameter": prof["min_via_drill"],
                  "min_via_annular_width": prof["min_annular"], "min_hole_to_hole": prof["min_hole_to_hole"],
                  "min_copper_edge_clearance": prof["min_edge_clearance"], "min_text_height": prof["silk_text_height"],
                  "min_text_thickness": prof["silk_line_width"], "min_silk_clearance": 0.05})
    ds["track_widths"] = [0.0, 0.2, 0.25, 0.3, 0.4, 0.5, 0.8, 1.0, 1.5, 2.0]
    ds["via_dimensions"] = [{"diameter": 0.0, "drill": 0.0}, {"diameter": 0.6, "drill": 0.3}, {"diameter": 0.8, "drill": 0.4}]
    two = int(fab.get("layers", 2)) <= 2
    ns = data.setdefault("net_settings", {})
    base = {"bus_width": 12, "diff_pair_via_gap": 0.25, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)", "wire_width": 6}
    ns["classes"] = [dict(base, name="Default", clearance=0.2, track_width=0.25, via_diameter=0.6 if two else 0.5,
                          via_drill=0.3 if two else 0.25, diff_pair_width=0.2, diff_pair_gap=0.2, priority=2147483647),
                     dict(base, name="Power", clearance=0.2, track_width=0.5, via_diameter=0.8, via_drill=0.4,
                          diff_pair_width=0.2, diff_pair_gap=0.2, priority=1, pcb_color="rgba(220, 60, 40, 1.000)")]
    ns.setdefault("netclass_patterns", [])
    ns.setdefault("netclass_assignments", None)
    data.setdefault("sheets", [])
    data.setdefault("text_variables", {})
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


EMPTY_SCH = """(kicad_sch
	(version 20250114)
	(generator "tracewright")
	(generator_version "10.0")
	(uuid "{uuid}")
	(paper "A4")
	(title_block
		(title "{title}")
		(date "{date}")
		(rev "A")
	)
	(lib_symbols)
	(sheet_instances
		(path "/"
			(page "1")
		)
	)
	(embedded_fonts no)
)
"""


def kicad_skeleton(root, cfg):
    pro = os.path.join(root, cfg["kicad_project"])
    hw = os.path.dirname(pro)
    stem = os.path.splitext(os.path.basename(pro))[0]
    os.makedirs(os.path.join(hw, "lib", f"{stem}.pretty"), exist_ok=True)
    write_kicad_pro(pro, stem, cfg.get("fab"))
    sch = os.path.join(hw, stem + ".kicad_sch")
    if not os.path.exists(sch):
        _write(sch, EMPTY_SCH.format(uuid=str(uuid.uuid4()), title=cfg["name"].replace('"', "'"),
                                     date=datetime.date.today().isoformat()))
    _write(os.path.join(hw, "lib", f"{stem}.kicad_sym"),
           '(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "tracewright")\n\t(generator_version "10.0")\n)\n',
           overwrite=False)
    _write(os.path.join(hw, "sym-lib-table"),
           f'(sym_lib_table\n\t(version 7)\n\t(lib (name "{stem}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{stem}.kicad_sym")'
           f'(options "")(descr "{cfg["name"]} project symbols"))\n)\n', overwrite=False)
    _write(os.path.join(hw, "fp-lib-table"),
           f'(fp_lib_table\n\t(version 7)\n\t(lib (name "{stem}")(type "KiCad")(uri "${{KIPRJMOD}}/lib/{stem}.pretty")'
           f'(options "")(descr "{cfg["name"]} project footprints"))\n)\n', overwrite=False)
    try:
        kicad.sch_upgrade(sch)
    except Exception:
        pass
    return pro


def gitignore(root, append=False):
    p = os.path.join(root, ".gitignore")
    if os.path.exists(p):
        if not append:
            return
        cur = open(p).read()
        if "# Tracewright" in cur:
            return
        with open(p, "a") as f:
            f.write("\n" + GITIGNORE)
    else:
        _write(p, GITIGNORE)


def new_project(root, cfg, brief):
    cfg["toolkit"] = TW_VERSION
    with open(os.path.join(root, twenv.CONFIG), "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    _write(os.path.join(root, "BRIEF.md"), f"# {cfg['name']}: brief\n\n_The request this project started from "
                                           f"({cfg['created'][:10]}). The agent keeps docs/requirements.md as the "
                                           f"agreed version._\n\n{brief.strip()}\n")
    kicad_skeleton(root, cfg)
    install_toolkit(root)
    install_agent_files(root, cfg)
    docs_skeleton(root, cfg, brief)
    gitignore(root)
    history.init(root)
    history.snapshot(root, "Project created")


def existing_project(root, cfg, shadow):
    cfg["toolkit"] = TW_VERSION
    cfgp = os.path.join(root, twenv.CONFIG)
    if not os.path.exists(cfgp):
        with open(cfgp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
    install_toolkit(root)
    install_agent_files(root, cfg, overwrite_claude=not os.path.exists(os.path.join(root, "CLAUDE.md")))
    docs_skeleton(root, cfg, "")
    if shadow:
        history.init(root, shadow=True)
    else:
        gitignore(root)
        history.init(root)
    history.snapshot(root, "Imported into Tracewright")


def refresh(root, cfg):
    """Update a project's toolkit and agent files to this app's version (after a checkpoint)."""
    history.snapshot(root, "Before updating the toolkit")
    install_toolkit(root)
    install_agent_files(root, cfg, overwrite_claude=False)
    cfg["toolkit"] = TW_VERSION
    return TW_VERSION
