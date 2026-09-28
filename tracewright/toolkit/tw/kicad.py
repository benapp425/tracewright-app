"""kicad-cli and KiCad-Python wrappers.

Every function raises KiCadError with KiCad's own message when a command fails, except the checks
(ERC / DRC), whose non-zero exit only means "violations found".
"""
import os, sys, json, subprocess, tempfile, shutil
from . import env

TW_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(TW_DIR)


class KiCadError(RuntimeError):
    pass


def workdir(args=(), data=None):
    """Where to run a KiCad program: the folder of the design file it works on, else home. KiCad
    records its working folder in the user's settings (kicad_common.json system.working_dir) when it
    exits; an app started from the Finder runs in / and would leave that there."""
    cands = [str(a) for a in args]
    if isinstance(data, dict):
        cands += [str(v) for v in data.values() if isinstance(v, str)]
    for a in reversed(cands):
        if a.endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro")) and os.path.exists(a):
            return os.path.dirname(os.path.abspath(a))
    return os.path.expanduser("~")


def cli(*args, ok=(0,), timeout=900, cwd=None):
    exe = env.require("cli")
    r = subprocess.run([exe, *[str(a) for a in args]], capture_output=True, text=True, timeout=timeout,
                       cwd=cwd or workdir(args))
    if r.returncode not in ok:
        msg = (r.stderr or r.stdout or "").strip()
        raise KiCadError(f"kicad-cli {' '.join(str(a) for a in args[:3])} failed ({r.returncode}): {msg[-1500:]}")
    return r


# ----------------------------------------------------------------------------- schematic
def erc(sch, out_json):
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    cli("sch", "erc", "--format", "json", "--severity-all", "--units", "mm", "-o", out_json, sch, ok=(0, 5, 6))
    with open(out_json) as f:
        return json.load(f)


def netlist(sch, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cli("sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, sch)
    return out


def sch_svg(sch, outdir, drawing_sheet=True, theme=None):
    """One SVG per sheet into outdir (cleared first). Returns the file list."""
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    args = ["sch", "export", "svg", "-o", outdir]
    if not drawing_sheet:
        args.append("--exclude-drawing-sheet")
    if theme:
        args += ["--theme", theme]
    cli(*args, sch)
    return sorted(os.path.join(outdir, f) for f in os.listdir(outdir) if f.endswith(".svg"))


def sch_pdf(sch, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cli("sch", "export", "pdf", "-o", out, sch)
    return out


def sch_bom(sch, out, fields="Reference,Value,Footprint,${QUANTITY},MPN,Manufacturer,LCSC"):
    cli("sch", "export", "bom", "--fields", fields, "--group-by", "Value,Footprint", "-o", out, sch)
    return out


def sch_upgrade(path):
    cli("sch", "upgrade", "--force", path)


# ----------------------------------------------------------------------------- board
def drc(pcb, out_json, parity=True):
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    args = ["pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", out_json]
    if parity:
        args.append("--schematic-parity")
    cli(*args, pcb, ok=(0, 5, 6))
    with open(out_json) as f:
        return json.load(f)


def pcb_upgrade(path):
    cli("pcb", "upgrade", "--force", path)


def pcb_render(pcb, out, side="top", width=1600, height=1000, quality="basic", extra=()):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cli("pcb", "render", "--width", width, "--height", height, "--side", side, "--background", "opaque",
        "--quality", quality, *extra, "-o", out, pcb)
    return out


def pcb_glb(pcb, out, components=True):
    """The board as binary glTF (metres, Y up; board x -> glTF x, board y -> glTF z): the body, copper,
    pads, silkscreen and mask, and each part's 3D model as a node named by its reference."""
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    args = ["pcb", "export", "glb", "--force", "-o", out, "--subst-models", "--include-tracks", "--include-pads",
            "--include-zones", "--include-silkscreen", "--include-soldermask"]
    if not components:
        args.append("--no-components")
    cli(*args, pcb, timeout=900)
    return out


def pcb_svg(pcb, out, layers, mirror=False, mode_single=True, extra=()):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    args = ["pcb", "export", "svg", "--layers", ",".join(layers), "--exclude-drawing-sheet", "--fit-page-to-board"]
    if mode_single:
        args.append("--mode-single")
    if mirror:
        args.append("--mirror")
    cli(*args, *extra, "-o", out, pcb)
    return out


def pcb_import(src, out, fmt="auto"):
    """Altium / Eagle / PADS / CADSTAR / Fabmaster / P-CAD / SolidWorks board -> .kicad_pcb."""
    cli("pcb", "import", "--format", fmt, "-o", out, src)
    return out


# ----------------------------------------------------------------------------- KiCad's python
def kpy(script, *args, timeout=1800, check=True, input_json=None):
    """Run a toolkit script under KiCad's Python (pcbnew available). `script` is a path or a module
    file name inside tw/pcb. Returns (returncode, stdout, stderr); stdout lines starting with
    'TWJSON ' are the script's structured result (see last_json)."""
    py = env.require("python")
    path = script if os.path.isabs(script) else os.path.join(TW_DIR, "pcb", script)
    e = dict(os.environ)
    e["PYTHONPATH"] = TOOLS_DIR + os.pathsep + e.get("PYTHONPATH", "")
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    inp = json.dumps(input_json) if input_json is not None else None
    r = subprocess.run([py, path, *[str(a) for a in args]], capture_output=True, text=True, timeout=timeout,
                       env=e, input=inp, cwd=workdir(args, input_json))
    err = "\n".join(l for l in (r.stderr or "").splitlines() if not _wx_noise(l))
    if check and r.returncode != 0:
        raise KiCadError(f"{os.path.basename(path)} failed ({r.returncode}): {(err or r.stdout)[-2500:]}")
    return r.returncode, r.stdout, err


def _wx_noise(line):
    return any(s in line for s in ("wxApp", "assert", "memory leak", "Debug: ", "OnInit", "CFBundle"))


def last_json(stdout):
    """The last 'TWJSON {...}' line a kpy script printed, parsed (or None)."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("TWJSON "):
            return json.loads(line[7:])
    return None
