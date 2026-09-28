#!/usr/bin/env python3
"""Update the board from the schematic netlist (KiCad's "Update PCB from Schematic", headless).
Run under KiCad's Python. Request on stdin:

  {"board": "x.kicad_pcb", "netlist": "x.net", "fp_dirs": {"Device": "/path/Device.pretty", ...},
   "remove_extra": false, "save": true}

Footprints are matched to symbols by the symbol path (so a renumbered reference is a rename, not a
new part), then by reference. Existing footprints keep their position, side, rotation and lock;
a changed footprint is swapped in place. New footprints are parked in a grid right of the board for
placement. Every pad gets the net the netlist gives its pin. Prints "TWJSON {...}".
"""
import sys, os, json, traceback
import pcbnew

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tw.netlist import Netlist          # noqa: E402

MM = pcbnew.FromMM
TO = pcbnew.ToMM
GRAVE = []        # removed items stay referenced until exit (a destroyed wrapper corrupts SWIG's types)


def load_fp(dirs, lib_id):
    lib, name = lib_id.split(":", 1) if ":" in lib_id else ("", lib_id)
    d = dirs.get(lib)
    if not d:
        return None, f"footprint library '{lib}' is not in the library tables"
    fp = pcbnew.FootprintLoad(d, name)
    if fp is None:
        return None, f"footprint {lib_id} not found in {d}"
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp, None


def set_fields(fp, comp):
    fp.SetReference(comp["ref"])
    fp.SetValue(comp["value"])
    for k, v in comp["fields"].items():
        if k in ("Footprint", "Reference", "Value"):
            continue
        try:
            existing = fp.GetField(k) if fp.HasField(k) else None
        except Exception:
            existing = None
        fp.SetField(k, v)
        if existing is None and k not in ("Datasheet", "Description"):
            try:
                f = fp.GetField(k)
                f.SetVisible(False)
                f.SetLayer(pcbnew.F_Fab if not fp.IsFlipped() else pcbnew.B_Fab)
                f.SetPosition(fp.GetPosition())
            except Exception:
                pass
    try:
        fp.SetDNP(bool(comp["dnp"]))
        fp.SetExcludedFromBOM(not comp["in_bom"])
    except Exception:
        pass


def main():
    req = json.loads(sys.stdin.read())
    path = req["board"]
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    keep = open(pro, "rb").read() if os.path.exists(pro) else None
    b = pcbnew.LoadBoard(path)
    nl = Netlist.load(req["netlist"])
    comps = {}
    for ref, p in nl.parts.items():
        if ref.startswith("#") or not p["footprint"] or not p.get("on_board", True):
            continue
        comps[ref] = dict(p, ref=ref)
    # netlist sheet paths: tw.netlist keeps the component tstamp; read sheet tstamps from the raw file
    sheet_ts = _sheet_tstamps(req["netlist"])
    by_path, by_ref = {}, {}
    for fp in list(b.GetFootprints()):
        by_ref[fp.GetReference()] = fp
        try:
            by_path[fp.GetPath().AsString()] = fp
        except Exception:
            pass
    created, updated, replaced, renamed, errors, moves = [], [], [], [], [], []
    bb = b.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0:
        px0, py0 = TO(bb.GetRight()) + 8.0, TO(bb.GetTop())
    else:
        px0, py0 = 150.0, 50.0
    park = {"x": 0.0, "y": 0.0, "row_h": 0.0}
    dirs = req.get("fp_dirs", {})
    for ref in sorted(comps, key=lambda r: (r.rstrip("0123456789"), int("0" + "".join(c for c in r if c.isdigit())))):
        c = comps[ref]
        kpath = sheet_ts.get(ref, "") + c.get("tstamp", "")
        fp = by_path.get(kpath) or by_ref.get(ref)
        want = c["footprint"]
        if fp is None:
            fp, err = load_fp(dirs, want)
            if err:
                errors.append(f"{ref}: {err}")
                continue
            set_fields(fp, c)
            try:
                fp.SetPath(pcbnew.KIID_PATH(kpath))
            except Exception:
                pass
            try:
                fp.SetSheetname(c.get("sheet", ""))
            except Exception:
                pass
            b.Add(fp)
            bbox = fp.GetBoundingBox(False, False) if hasattr(fp, "GetBoundingBox") else None
            w = TO(bbox.GetWidth()) if bbox else 5.0
            h = TO(bbox.GetHeight()) if bbox else 5.0
            if park["x"] + w > 60.0:
                park["x"], park["y"] = 0.0, park["y"] + park["row_h"] + 2.0
                park["row_h"] = 0.0
            fp.SetPosition(pcbnew.VECTOR2I(MM(px0 + park["x"] + w / 2), MM(py0 + park["y"] + h / 2)))
            park["x"] += w + 2.0
            park["row_h"] = max(park["row_h"], h)
            created.append(ref)
        else:
            cur = f"{fp.GetFPID().GetLibNickname().wx_str()}:{fp.GetFPID().GetLibItemName().wx_str()}"
            if cur.split(":")[-1] != want.split(":")[-1]:
                nfp, err = load_fp(dirs, want)
                if err:
                    errors.append(f"{ref}: {err}")
                else:
                    pos = pcbnew.VECTOR2I(fp.GetPosition().x, fp.GetPosition().y)
                    rot, flipped, locked = fp.GetOrientationDegrees(), fp.IsFlipped(), fp.IsLocked()
                    b.Remove(fp)
                    GRAVE.append(fp)
                    nfp.SetPosition(pos)
                    if flipped:
                        try:
                            nfp.Flip(pos, pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                        except (AttributeError, TypeError):
                            nfp.Flip(pos, False)
                    nfp.SetOrientationDegrees(rot)
                    nfp.SetLocked(locked)
                    try:
                        nfp.SetPath(pcbnew.KIID_PATH(kpath))
                    except Exception:
                        pass
                    b.Add(nfp)
                    fp = nfp
                    replaced.append(f"{ref}: {cur} -> {want}")
            if fp.GetReference() != ref:
                renamed.append(f"{fp.GetReference()} -> {ref}")
            before = (fp.GetValue(), fp.GetReference())
            set_fields(fp, c)
            if before != (fp.GetValue(), fp.GetReference()):
                updated.append(ref)
        # nets
        for pad in fp.Pads():
            num = pad.GetNumber()
            name = nl.pin.get((ref, num))
            if name is None:
                pad.SetNetCode(0)
                continue
            ni = b.FindNet(name)
            if ni is None:
                ni = pcbnew.NETINFO_ITEM(b, name)
                b.Add(ni)
            pad.SetNet(ni)
    extra = []
    for fp in list(b.GetFootprints()):
        r = fp.GetReference()
        board_only = False
        try:
            board_only = bool(fp.IsBoardOnly())
        except Exception:
            pass
        if r in comps or board_only or r.startswith(("REF**", "G***", "LOGO", "kibuzzard")):
            continue
        extra.append(r)
    removed = []
    if req.get("remove_extra"):
        for fp in list(b.GetFootprints()):
            if fp.GetReference() in extra:
                removed.append(fp.GetReference())
                b.Remove(fp)
                GRAVE.append(fp)
    b.BuildConnectivity()
    if req.get("save", True):
        pcbnew.SaveBoard(path, b)
        if keep is not None and open(pro, "rb").read() != keep:
            open(pro, "wb").write(keep)
    print("TWJSON " + json.dumps({"ok": not errors, "created": created, "updated": updated, "replaced": replaced,
                                  "renamed": renamed, "extra": extra, "removed": removed, "errors": errors,
                                  "footprints": len(list(b.GetFootprints()))}))


def _sheet_tstamps(path):
    from tw.sexp import parse, find, findall, value
    t = parse(open(path, encoding="utf-8").read())
    out = {}
    comps = find(t, "components")
    for c in (findall(comps, "comp") if comps else []):
        sp = find(c, "sheetpath")
        out[str(value(c, "ref", ""))] = str(value(sp, "tstamps", "")) if sp else ""
    return out


if __name__ == "__main__":
    code = 0
    try:
        main()
    except Exception as e:
        print("TWJSON " + json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}))
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
