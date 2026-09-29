"""How tall each part stands, from its footprint's 3D model. KiCad's models are STEP files, whose geometry
is listed as CARTESIAN_POINTs in millimetres; the highest point after the footprint's model offset, scale
and rotation is the part's height above the board (an upper bound: spline control points can sit a
little outside the surface). No CAD kernel needed.

    height(fp, project)   -> (mm, model file) or (None, why)
"""
import os, re, math, functools

_PT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)")
_VARS = re.compile(r"\$\{([A-Z0-9_]+)\}|\$\(([A-Z0-9_]+)\)")


@functools.lru_cache(maxsize=512)
def _points(path, mtime):
    """Every CARTESIAN_POINT of a STEP file (thinned to at most 60000)."""
    try:
        with open(path, "r", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return None
    pts = [(float(a), float(b), float(c)) for a, b, c in _PT.findall(txt)]
    if len(pts) > 60000:
        step = len(pts) // 60000 + 1
        pts = pts[::step]
    return pts or None


def resolve(model, project=None):
    """A model path with KiCad's variables expanded; .wrl names tried as .step too. None if not found."""
    from . import env
    share = None
    try:
        share = env.kicad().get("share")
    except Exception:
        share = None
    models_dir = os.path.join(share, "3dmodels") if share else None
    prj = os.path.dirname(project.pcb) if project is not None and getattr(project, "pcb", None) else None

    def sub(m):
        name = m.group(1) or m.group(2)
        if name == "KIPRJMOD":
            return prj or ""
        if name.endswith("3DMODEL_DIR") or name == "KISYS3DMOD":
            return os.environ.get(name) or models_dir or ""
        return os.environ.get(name, "")
    p = _VARS.sub(sub, model or "").strip()
    if not p:
        return None
    if not os.path.isabs(p) and prj:
        p = os.path.join(prj, p)
    cands = [p]
    base, ext = os.path.splitext(p)
    if ext.lower() in (".wrl", ".wings"):
        cands = [base + ".step", base + ".stp", p]
    for c in cands:
        if os.path.exists(c) and c.lower().endswith((".step", ".stp")):
            return c
    return None


def _rot(pt, rx, ry, rz):
    """KiCad's model rotation: degrees about x, then y, then z (applied as the 3D viewer does)."""
    x, y, z = pt
    for ang, axis in ((rx, "x"), (ry, "y"), (rz, "z")):
        if not ang:
            continue
        a = math.radians(-ang)
        c, s = math.cos(a), math.sin(a)
        if axis == "x":
            y, z = y * c - z * s, y * s + z * c
        elif axis == "y":
            x, z = x * c + z * s, -x * s + z * c
        else:
            x, y = x * c - y * s, x * s + y * c
    return x, y, z


def height(fp, project=None):
    """(height above the board in mm, model file) for a footprint, or (None, why)."""
    if not getattr(fp, "models", None):
        return None, "no 3D model"
    places = getattr(fp, "model_places", None) or [{}] * len(fp.models)
    best, used, why = None, None, "model file not found"
    for model, place in zip(fp.models, places):
        path = resolve(model, project)
        if not path:
            continue
        pts = _points(path, os.path.getmtime(path))
        if not pts:
            why = "model has no geometry to read"
            continue
        sx, sy, sz = (place or {}).get("scale", (1.0, 1.0, 1.0))
        ox, oy, oz = (place or {}).get("offset", (0.0, 0.0, 0.0))
        rx, ry, rz = (place or {}).get("rotate", (0.0, 0.0, 0.0))
        top = max(_rot((x * sx, y * sy, z * sz), rx, ry, rz)[2] for x, y, z in pts) + oz
        if best is None or top > best:
            best, used = top, os.path.basename(path)
    if best is None:
        return None, why
    return round(max(best, 0.0), 2), used
