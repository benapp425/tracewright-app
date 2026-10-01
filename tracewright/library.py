"""My parts: the parts you have checked once and want again -- each one's symbol, footprint, 3D model, pin table and
what made it right (its JLC pad map, notes) -- kept with the app, not in a project, and offered to every project.
Saved from a project's part card; Claude's library tool finds them and copies one into a project's own libraries, so
the next board starts from what is known to work.

    data dir/library/index.json   [{id, name, mpn, lcsc, value, manufacturer, description, datasheet, symbol, footprint,
                                    model, pins, pad_map, notes, from: {project, ref}, saved}]
    data dir/library/<id>/        symbol.kicad_sym, footprint.kicad_mod, the 3D model, pins.json
"""
import json, os, re, shutil, threading, time

from . import config

_lock = threading.Lock()
FIELDS = ("MPN", "LCSC", "Manufacturer", "Datasheet", "Description")


def _dir():
    return os.path.join(config.data_dir(), "library")


def _index_path():
    return os.path.join(_dir(), "index.json")


def items():
    try:
        with open(_index_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_index(lst):
    os.makedirs(_dir(), exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lst, f, indent=1)
    os.replace(tmp, _index_path())


def get(pid):
    for it in items():
        if it["id"] == pid:
            return it
    raise KeyError(pid)


def search(q=""):
    """The saved parts that match every word of q (in the part number, value, maker, description, footprint)."""
    words = [w.lower() for w in re.split(r"\s+", q or "") if w]
    out = []
    for it in items():
        hay = " ".join(str(it.get(k) or "") for k in ("name", "mpn", "lcsc", "value", "manufacturer", "description", "footprint", "notes")).lower()
        if all(w in hay for w in words):
            out.append(it)
    return out


def _slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:60] or "part"


def remove(pid):
    with _lock:
        lst = items()
        keep = [x for x in lst if x["id"] != pid]
        if len(keep) == len(lst):
            raise KeyError(pid)
        _save_index(keep)
    shutil.rmtree(os.path.join(_dir(), pid), ignore_errors=True)


# ------------------------------------------------------------------ saving a part from a project
def _instance(tw, ref):
    """(sheet file, the symbol's lib_id, its fields) for a placed symbol, or None."""
    from tw.schematic import Hierarchy
    h = Hierarchy.load(tw.sch)
    for sh in h.sheets:
        for s in sh.symbols:
            if s.ref == ref:
                return sh.file, s.lib_id, dict(s.fields, Value=s.value)
    return None


def _lib_symbol_text(sheet_file, lib_id):
    """The symbol's definition as the sheet embeds it (lib_symbols), renamed for a library of its own."""
    from tw.sexp import parse, find, findall
    with open(sheet_file, encoding="utf-8") as f:
        text = f.read()
    t = parse(text, spans=True)
    ls = find(t, "lib_symbols")
    for s in findall(ls, "symbol") if ls else []:
        if len(s) > 1 and str(s[1]) == lib_id:
            body = text[s.span[0]:s.span[1]]
            name = lib_id.split(":")[-1]
            return body.replace(f'(symbol "{lib_id}"', f'(symbol "{name}"', 1), name
    return None, None


def _model_of(fp_text, project_dir):
    """The 3D model a footprint names, as a file on this Mac (or None)."""
    m = re.search(r'\(model\s+"([^"]+)"', fp_text) or re.search(r"\(model\s+(\S+)", fp_text)
    if not m:
        return None
    from tw.libtable import expand
    path = expand(m.group(1), project_dir)
    for cand in (path, re.sub(r"\.wrl$", ".step", path), re.sub(r"\.step$", ".wrl", path)):
        if os.path.isfile(cand):
            return cand
    return None


def save(project, ref, notes=""):
    """Save a project's part to the library (project: tracewright.projects.Project). Returns the entry."""
    tw = project.tw
    inst = _instance(tw, ref)
    if not inst:
        raise ValueError(f"no {ref} in the schematic")
    sheet, lib_id, fields = inst
    sym_text, sym_name = _lib_symbol_text(sheet, lib_id)
    if not sym_text:
        raise ValueError(f"{ref}'s symbol is not in its sheet")
    fp_id = fields.get("Footprint") or ""
    from tw.libtable import LibTables
    fp_file = LibTables(tw.hw).footprint_file(fp_id) if fp_id else None
    mpn = fields.get("MPN") or ""
    pid = _slug(mpn or f"{fields.get('Value', ref)}-{fp_id.split(':')[-1]}")
    d = os.path.join(_dir(), pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "symbol.kicad_sym"), "w", encoding="utf-8") as f:
        f.write('(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "tracewright")\n\t(generator_version "10.0")\n\t' + sym_text + "\n)\n")
    entry = {"id": pid, "name": mpn or fields.get("Value") or ref, "mpn": mpn, "lcsc": fields.get("LCSC") or "",
             "value": fields.get("Value") or "", "manufacturer": fields.get("Manufacturer") or "",
             "description": fields.get("Description") or "", "datasheet": fields.get("Datasheet") or "",
             "symbol": sym_name, "footprint": None, "footprint_id": fp_id, "model": None, "pins": None,
             "pad_map": (tw.setting("fab.cpl_pad_map", {}) or {}).get(ref) if hasattr(tw, "setting") else None,
             "notes": (notes or "").strip()[:1000], "from": {"project": project.name, "ref": ref}, "saved": time.strftime("%Y-%m-%d")}
    if fp_file:
        fp_text = open(fp_file, encoding="utf-8").read()
        shutil.copyfile(fp_file, os.path.join(d, "footprint.kicad_mod"))
        entry["footprint"] = os.path.splitext(os.path.basename(fp_file))[0]
        model = _model_of(fp_text, tw.hw)
        if model:
            shutil.copyfile(model, os.path.join(d, os.path.basename(model)))
            entry["model"] = os.path.basename(model)
    from tw import datasheets
    pins, source, _ = datasheets.pins_for(tw, lcsc=entry["lcsc"], mpn=mpn, value=entry["value"])
    if pins:
        with open(os.path.join(d, "pins.json"), "w") as f:
            json.dump({"pins": pins, "source": source, "mpn": mpn, "lcsc": entry["lcsc"]}, f, indent=1)
        entry["pins"] = len(pins)
    with _lock:
        lst = [x for x in items() if x["id"] != pid] + [entry]
        _save_index(sorted(lst, key=lambda x: x["name"].lower()))
    return entry


# ------------------------------------------------------------------ using one in a project
def use(project, pid):
    """Copy a saved part into the project's own libraries (hardware/<project>/lib: its symbol library, its .pretty,
    lib/3d for the model). Returns {symbol, footprint (lib ids), fields} to put on the schematic symbol."""
    it = get(pid)
    tw = project.tw
    d = os.path.join(_dir(), pid)
    stem = tw.stem
    lib = os.path.join(tw.hw, "lib")
    os.makedirs(os.path.join(lib, f"{stem}.pretty"), exist_ok=True)
    sym_lib = os.path.join(lib, f"{stem}.kicad_sym")
    if not os.path.exists(sym_lib):
        with open(sym_lib, "w", encoding="utf-8") as f:
            f.write('(kicad_symbol_lib\n\t(version 20241209)\n\t(generator "tracewright")\n\t(generator_version "10.0")\n)\n')
    from tw.sexp import parse, find, findall
    src = open(os.path.join(d, "symbol.kicad_sym"), encoding="utf-8").read()
    t = parse(src, spans=True)
    node = findall(t, "symbol")[0]
    body = src[node.span[0]:node.span[1]]
    cur = open(sym_lib, encoding="utf-8").read()
    if f'(symbol "{it["symbol"]}"' not in cur:                 # the symbol, once
        end = cur.rstrip().rfind(")")
        cur = cur[:end].rstrip("\n") + "\n\t" + body + "\n" + cur[end:]
        with open(sym_lib, "w", encoding="utf-8") as f:
            f.write(cur)
    fp_lid = it.get("footprint_id") or ""
    if it.get("footprint"):
        text = open(os.path.join(d, "footprint.kicad_mod"), encoding="utf-8").read()
        if it.get("model"):
            os.makedirs(os.path.join(lib, "3d"), exist_ok=True)
            shutil.copyfile(os.path.join(d, it["model"]), os.path.join(lib, "3d", it["model"]))
            text = re.sub(r'\(model\s+("[^"]+"|\S+)', f'(model "${{KIPRJMOD}}/lib/3d/{it["model"]}"', text, count=1)
        with open(os.path.join(lib, f"{stem}.pretty", it["footprint"] + ".kicad_mod"), "w", encoding="utf-8") as f:
            f.write(text)
        fp_lid = f"{stem}:{it['footprint']}"
    from tw.libtable import add_lib
    add_lib(tw.hw, "sym", stem, f"${{KIPRJMOD}}/lib/{stem}.kicad_sym", "this project's own parts")
    add_lib(tw.hw, "fp", stem, f"${{KIPRJMOD}}/lib/{stem}.pretty", "this project's own footprints")
    if os.path.exists(os.path.join(d, "pins.json")):
        from tw import datasheets
        p = json.load(open(os.path.join(d, "pins.json")))
        datasheets.save_pins(tw, p["pins"], p.get("source", "my parts library"), mpn=p.get("mpn", ""), lcsc=p.get("lcsc", ""))
    fields = {k: it.get(k.lower()) for k in ("MPN", "LCSC", "Manufacturer", "Datasheet", "Description") if it.get(k.lower())}
    return {"symbol": f"{stem}:{it['symbol']}", "footprint": fp_lid, "fields": fields, "value": it.get("value"),
            "pad_map": it.get("pad_map"), "notes": it.get("notes")}
