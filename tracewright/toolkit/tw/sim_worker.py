#!/usr/bin/env python3
"""One ngspice run, in a process of its own (tw/sim.py starts it): the shared library KiCad ships, a netlist from
stdin as JSON {"lib", "netlist", "probes", "points"}, the analyses it asks for, and "TWJSON {...}" on stdout: the
plot's vectors (each probe, or every vector when none are asked for), thinned to `points`, and ngspice's own lines."""
import ctypes, json, sys
from ctypes import c_int, c_char_p, c_void_p, c_bool, c_double, c_short, POINTER, CFUNCTYPE, Structure


class VecInfo(Structure):
    _fields_ = [("v_name", c_char_p), ("v_type", c_int), ("v_flags", c_short), ("v_realdata", POINTER(c_double)),
                ("v_compdata", c_void_p), ("v_length", c_int)]


class Complex(Structure):
    _fields_ = [("re", c_double), ("im", c_double)]


def main():
    req = json.loads(sys.stdin.read())
    lib = ctypes.CDLL(req["lib"])
    log = []
    SendChar = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
    SendStat = CFUNCTYPE(c_int, c_char_p, c_int, c_void_p)
    Exit = CFUNCTYPE(c_int, c_int, c_bool, c_bool, c_int, c_void_p)
    BG = CFUNCTYPE(c_int, c_bool, c_int, c_void_p)
    on_char = SendChar(lambda s, i, u: log.append(s.decode(errors="replace").replace("stdout ", "").replace("stderr ", "")) or 0)
    on_stat = SendStat(lambda s, i, u: 0)
    on_exit = Exit(lambda status, unload, quit, i, u: 0)
    on_bg = BG(lambda running, i, u: 0)
    lib.ngSpice_Init.argtypes = [SendChar, SendStat, Exit, c_void_p, c_void_p, BG, c_void_p]
    lib.ngSpice_Init(on_char, on_stat, on_exit, None, None, on_bg, None)
    lib.ngGet_Vec_Info.restype = POINTER(VecInfo)
    lib.ngGet_Vec_Info.argtypes = [c_char_p]
    lib.ngSpice_Circ.argtypes = [POINTER(c_char_p)]
    lib.ngSpice_Command.argtypes = [c_char_p]
    lib.ngSpice_CurPlot.restype = c_char_p
    lib.ngSpice_AllVecs.restype = POINTER(c_char_p)
    lib.ngSpice_AllVecs.argtypes = [c_char_p]
    lines = [l.encode() for l in req["netlist"].splitlines() if l.strip()]
    if not lines or not lines[-1].strip().lower().startswith(b".end"):
        lines.append(b".end")
    arr = (c_char_p * (len(lines) + 1))(*lines, None)
    if lib.ngSpice_Circ(arr) != 0:
        print("TWJSON " + json.dumps({"ok": False, "error": "ngspice could not load the netlist", "log": log[-60:]}))
        return
    lib.ngSpice_Command(b"run")
    plot = (lib.ngSpice_CurPlot() or b"").decode()
    names = []
    av = lib.ngSpice_AllVecs(plot.encode())
    i = 0
    while av and av[i]:
        names.append(av[i].decode())
        i += 1
    want = [p.lower() for p in req.get("probes") or []] or names
    out, points = {}, int(req.get("points") or 2000)
    scale = None
    for n in names:
        if n.lower() in ("time", "frequency", "v-sweep", "i-sweep", "temp-sweep"):
            scale = n
    for p in ([scale] if scale else []) + want:
        key = p
        if key.lower().startswith("v(") and key.endswith(")"):         # v(out) -> out
            key = key[2:-1]
        if key.lower().startswith("i(") and key.endswith(")"):         # i(v1) -> v1#branch
            key = key[2:-1] + "#branch"
        info = lib.ngGet_Vec_Info(f"{plot}.{key}".encode())
        if not info:
            out[p] = None
            continue
        v = info.contents
        n = v.v_length
        step = max(1, n // points)
        if v.v_realdata:
            vals = [v.v_realdata[k] for k in range(0, n, step)]
            out[p] = {"values": vals, "complex": False}
        elif v.v_compdata:
            cp = ctypes.cast(v.v_compdata, POINTER(Complex))
            out[p] = {"values": [abs(complex(cp[k].re, cp[k].im)) for k in range(0, n, step)], "complex": True,
                      "phase": [__import__("math").degrees(__import__("cmath").phase(complex(cp[k].re, cp[k].im))) for k in range(0, n, step)]}
        else:
            out[p] = None
    errs = [l for l in log if "error" in l.lower()]
    print("TWJSON " + json.dumps({"ok": not errs and any(out.values()), "plot": plot, "scale": scale, "vectors": out,
                                  "names": names[:80], "log": log[-60:], "error": errs[0] if errs else None}))


if __name__ == "__main__":
    main()
