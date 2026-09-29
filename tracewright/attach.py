"""Files attached to a chat message -- dropped on the window, pasted into the message box or picked --
each put in its place in the project by what it is, and described to Claude:

  pictures         uploads/images/         sent with the message, so Claude sees them
  PDFs             docs/datasheets/        Claude reads the pages it needs
  symbol library   <hardware>/lib/         added to the project's symbol library table
  footprint lib    <hardware>/lib/         (a .pretty folder) added to the footprint library table
  footprint        <hardware>/lib/<project>.pretty/   the project's own footprint library
  3D model         <hardware>/lib/3d/
  KiCad design     uploads/kicad/          another design's files, for reference
  anything else    uploads/

A name already taken gets a number ("LM7805-2.pdf") rather than replacing the file there."""
import os, shutil, subprocess, sys

IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
MODEL = {".step", ".stp", ".wrl", ".glb"}
KICAD = {".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_prl", ".kicad_dru"}

WHAT = {"image": "a picture", "pdf": "a PDF", "symbols": "a KiCad symbol library", "footprints": "a KiCad footprint library",
        "footprint": "a KiCad footprint", "model": "a 3D model", "kicad": "a file from another KiCad design", "file": "a file"}


def kind_of(name, is_dir=False):
    n = name.lower().rstrip("/")
    ext = os.path.splitext(n)[1]
    if is_dir:
        return "footprints" if ext == ".pretty" else "file"
    if ext in IMAGE:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".kicad_sym":
        return "symbols"
    if ext == ".kicad_mod":
        return "footprint"
    if ext in MODEL:
        return "model"
    if ext in KICAD:
        return "kicad"
    return "file"


def _lib(project):
    return os.path.relpath(os.path.join(project.tw.hw, "lib"), project.root)


def own_footprints(project):
    """The project's own footprint library (the first .pretty in its lib folder, or <stem>.pretty)."""
    lib = os.path.join(project.tw.hw, "lib")
    try:
        have = sorted(d for d in os.listdir(lib) if d.endswith(".pretty") and os.path.isdir(os.path.join(lib, d)))
    except OSError:
        have = []
    name = have[0] if have else f"{project.tw.stem or 'Project'}.pretty"
    return os.path.relpath(os.path.join(lib, name), project.root)


def folder_for(project, kind):
    lib = _lib(project)
    return {"image": os.path.join("uploads", "images"), "pdf": os.path.join("docs", "datasheets"), "symbols": lib,
            "footprints": lib, "footprint": own_footprints(project), "model": os.path.join(lib, "3d"),
            "kicad": os.path.join("uploads", "kicad")}.get(kind, "uploads")


def unique(path):
    """path, or path with -2, -3 ... before its extension when the name is taken."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path.rstrip("/"))
    if base.endswith(".tar"):
        base, ext = base[:-4], ".tar" + ext
    i = 2
    while os.path.exists(f"{base}-{i}{ext}"):
        i += 1
    return f"{base}-{i}{ext}"


def clean_name(name):
    name = os.path.basename(str(name).replace("\\", "/").rstrip("/")) or "file"
    name = "".join(ch for ch in name if ch not in '<>:"|?*\x00').strip(" .") or "file"
    return name[:180]


def destination(project, name, is_dir=False):
    """(kind, absolute destination) for an attachment called name."""
    kind = kind_of(name, is_dir)
    folder = os.path.join(project.root, folder_for(project, kind))
    return kind, unique(os.path.join(folder, clean_name(name)))


def finish(project, kind, dest):
    """After the file is in place: HEIC pictures become JPEG (Claude takes PNG, JPEG, GIF, WebP), libraries go
    into the project's tables. Returns the attachment's record."""
    from tw import libtable
    rel = lambda p: os.path.relpath(p, project.root)
    rec = {"path": rel(dest), "name": os.path.basename(dest), "kind": kind, "made": [rel(dest)]}
    if kind == "image" and os.path.splitext(dest)[1].lower() in (".heic", ".heif", ".tif", ".tiff", ".bmp"):
        jpg = unique(os.path.splitext(dest)[0] + ".jpg")
        if _to_jpeg(dest, jpg):
            rec["path"], rec["name"] = rel(jpg), os.path.basename(jpg)
            rec["made"].append(rel(jpg))
    hw = project.tw.hw
    if kind == "symbols":
        name = os.path.splitext(os.path.basename(dest))[0]
        uri = "${KIPRJMOD}/" + os.path.relpath(dest, hw).replace(os.sep, "/")
        if libtable.add_lib(hw, "sym", name, uri, "attached in the chat"):
            rec["table"] = ["sym", name]
    elif kind in ("footprints", "footprint"):
        lib = dest if kind == "footprints" else os.path.dirname(dest)
        name = os.path.splitext(os.path.basename(lib))[0]
        uri = "${KIPRJMOD}/" + os.path.relpath(lib, hw).replace(os.sep, "/")
        if libtable.add_lib(hw, "fp", name, uri, "attached in the chat" if kind == "footprints" else "project footprints"):
            rec["table"] = ["fp", name]
        rec["library"] = name
    try:
        rec["size"] = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(dest) for f in fs) if os.path.isdir(dest) \
            else os.path.getsize(os.path.join(project.root, rec["path"]))
    except OSError:
        rec["size"] = 0
    rec["label"] = label(rec)
    return rec


def _to_jpeg(src, dest):
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dest, "JPEG", quality=90)
        return True
    except Exception:
        pass
    if sys.platform == "darwin" and shutil.which("sips"):                 # HEIC: macOS converts it
        r = subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dest], capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(dest)
    return False


def add_path(project, src):
    """A file or folder from this Mac, copied in."""
    src = os.path.expanduser(str(src))
    is_dir = os.path.isdir(src)
    kind, dest = destination(project, src, is_dir)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if is_dir:
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    return finish(project, kind, dest)


def label(rec):
    """How the attachment is described to Claude."""
    what, path = WHAT.get(rec["kind"], "a file"), rec["path"]
    if rec["kind"] == "image":
        return f"{what}, {path} (it is in this message)"
    if rec["kind"] == "pdf":
        return f"{what}, {path} (read the pages you need)"
    if rec["kind"] in ("symbols", "footprints"):
        t = rec.get("table")
        return f"{what}, {path}" + (f" (added to the project's {'symbol' if rec['kind'] == 'symbols' else 'footprint'} library table as \"{t[1]}\")"
                                    if t else " (a library of that name was already in the project's table)")
    if rec["kind"] == "footprint":
        return f"{what}, {path} (in the project's footprint library \"{rec.get('library', '')}\")"
    return f"{what}, {path}"


def remove(project, rec):
    """Undo an attachment the user took back before sending: its files, and its library table entry."""
    from tw import libtable
    for p in rec.get("made") or [rec["path"]]:
        full = os.path.join(project.root, p)
        if not os.path.abspath(full).startswith(project.root + os.sep):
            continue
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        elif os.path.exists(full):
            os.remove(full)
    t = rec.get("table")
    if t:
        libtable.remove_lib(project.tw.hw, t[0], t[1])


def image_for_claude(path, max_px=1568, max_bytes=3_750_000):
    """(media type, bytes) of a picture ready to send to Claude: PNG, JPEG, GIF or WebP as it is when small
    enough; anything else, or anything bigger, scaled down and saved again (PNG for drawings and screen
    shots, JPEG for photos). None when it is not a picture that can be read."""
    with open(path, "rb") as f:
        data = f.read()
    head = data[:16]
    kind = ("image/png" if head.startswith(b"\x89PNG") else "image/jpeg" if head.startswith(b"\xff\xd8") else
            "image/gif" if head[:6] in (b"GIF87a", b"GIF89a") else "image/webp" if head[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            if kind and len(data) <= max_bytes and max(im.size) <= max_px:
                return kind, data
            im.thumbnail((max_px, max_px))
            out = io.BytesIO()
            photo = kind == "image/jpeg" or (im.mode not in ("RGBA", "LA", "P") and kind != "image/png")
            if photo:
                im.convert("RGB").save(out, "JPEG", quality=86)
            else:
                im.save(out, "PNG", optimize=True)
                if out.tell() > max_bytes:
                    out = io.BytesIO()
                    im.convert("RGB").save(out, "JPEG", quality=84)
                    photo = True
            return ("image/jpeg" if photo else "image/png"), out.getvalue()
    except Exception:
        return (kind, data) if kind and len(data) <= max_bytes else None
