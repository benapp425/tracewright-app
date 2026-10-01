#!/usr/bin/env python3
"""A long-running board editor under KiCad's Python: one board kept loaded, ops applied to it as they come (the
app's in-app editing and Claude's board tools) and the file saved after each batch, so an edit takes milliseconds
instead of a KiCad start. One JSON request per line on stdin, one "TWJSON {...}" line back for each:

  {"id": 1, "cmd": "apply", "board": "x.kicad_pcb", "ops": [...], "save": true, "fill_after": false,
   "stop_on_error": true}                                  -> {"id", "ok", "saved", "results", "changes"}
  {"id": 2, "cmd": "ping"}                                 -> {"id", "ok": true}
  {"id": 3, "cmd": "close"}                                -> exits

The board is read again whenever its file changed since this worker last read or wrote it (KiCad saved it, a
script or git changed it), so it never writes over someone else's edit. Same ops as kpy_ops.py."""
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcbnew
import kpy_ops

STATE = {"path": None, "board": None, "stamp": None}


def _stamp(path):
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def board(path):
    path = os.path.abspath(path)
    if STATE["board"] is None or STATE["path"] != path or STATE["stamp"] != _stamp(path):
        if STATE["board"] is not None:
            kpy_ops.GRAVE.append(STATE["board"])        # never let Python destroy what SWIG still points into
        b = pcbnew.LoadBoard(path)
        kpy_ops.FPS.clear()
        kpy_ops.find_fp(b, "")                           # index the footprints before anything is removed
        STATE.update(path=path, board=b, stamp=_stamp(path))
    return STATE["board"]


def apply(req):
    path = req["board"]
    b = board(path)
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    keep = open(pro, "rb").read() if os.path.exists(pro) else None
    changes = []
    ok, results = kpy_ops.apply_ops(b, req.get("ops", []), changes, req.get("stop_on_error", True))
    saved = False
    if req.get("save", True) and (ok or not req.get("stop_on_error", True)):
        if req.get("fill_after"):
            kpy_ops.do_fill(b, changes)
        pcbnew.SaveBoard(path, b)
        saved = True
        if keep is not None and open(pro, "rb").read() != keep:   # SaveBoard may rewrite project settings: put them back
            open(pro, "wb").write(keep)
        STATE["stamp"] = _stamp(path)
    elif not ok:
        STATE["board"] = None                            # a half-applied batch: read the file again next time
    return {"ok": ok, "saved": saved, "results": results, "changes": changes}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            cmd = req.get("cmd")
            if cmd == "close":
                break
            if cmd == "ping":
                out = {"ok": True}
            elif cmd == "apply":
                out = apply(req)
            else:
                out = {"ok": False, "error": f"unknown command {cmd}"}
        except Exception as e:
            STATE["board"] = None
            out = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}
        out["id"] = rid
        sys.stdout.write("TWJSON " + json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.flush()
        os._exit(0)
