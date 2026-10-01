"""Apply board operations: live into the open KiCad (IPC API) when it has this board open, else on
the file through KiCad's Python. Both paths take the same op list (see kpy_ops.py)."""
import os, json
from .. import env, kicad
from ..netlist import Netlist
from ..libtable import LibTables


LIVE_OPS = {"move", "track", "tracks", "via", "vias", "delete", "lock", "fill"}


def apply(project, ops, save=True, live="auto", fill_after=False, stop_on_error=True):
    """Returns {"ok", "via": "live"|"file"|"file+reload", "results", "changes"}.

    With KiCad showing this board, ops the IPC API supports run live (one undo step); a list with
    other ops (zones, outline, rule areas, text) is done as: save in KiCad, edit the file, reload."""
    if live in ("auto", True):
        try:
            from .. import live as livemod
            link = livemod.link_for(project.pcb)
            if link is not None:
                if all(o.get("op") in LIVE_OPS for o in ops):
                    res = link.apply(ops)
                    res["via"] = "live"
                    if save and res.get("ok"):
                        link.save()
                    return res
                link.save()
                rc, out, err = kicad.kpy("kpy_ops.py", input_json={"board": project.pcb, "ops": ops, "save": True,
                                                                  "fill_after": fill_after,
                                                                  "stop_on_error": stop_on_error}, check=False)
                res = kicad.last_json(out) or {"ok": False, "error": (err or out)[-2000:]}
                link.revert()
                res["via"] = "file+reload"
                return res
        except ImportError:
            pass
        except Exception as e:                           # the live link failed: say so, fall back to the file
            if live is True:
                raise
            first_err = f"live link unavailable ({e}); edited the file"
        else:
            first_err = None
    else:
        first_err = None
    res = _on_file(project, ops, save, fill_after, stop_on_error)
    res["via"] = "file"
    if first_err:
        res["note"] = first_err
    return res


def _on_file(project, ops, save, fill_after, stop_on_error):
    """The ops on the board file: through the long-running board worker (tw/pcb/worker.py), else KiCad's Python once."""
    from . import worker
    if worker.enabled():
        try:
            return worker.apply(project.pcb, ops, save=save, fill_after=fill_after, stop_on_error=stop_on_error)
        except Exception:
            pass                                          # the one-shot script below
    rc, out, err = kicad.kpy("kpy_ops.py", input_json={"board": project.pcb, "ops": ops, "save": save,
                                                      "fill_after": fill_after, "stop_on_error": stop_on_error},
                             check=False)
    return kicad.last_json(out) or {"ok": False, "error": (err or out)[-2000:]}


def fp_dirs(project, lib_ids):
    lt = LibTables(project.hw)
    out, missing = {}, []
    for lid in lib_ids:
        lib = lid.split(":")[0] if ":" in lid else ""
        if lib in out:
            continue
        d = lt.footprint_dir(lid)
        if d:
            out[lib] = d
        else:
            missing.append(lib)
    return out, missing


def sync(project, remove_extra=False, save=True):
    """Update the board from the schematic (creates an empty board first if there is none)."""
    if not project.has_pcb():                # with the stack-up plan's layer count, else the agreed limit's
        from .. import stackup, constraints
        plan = stackup.get(getattr(project, "cfg", None))
        n = (plan or {}).get("layers") or (constraints.get(getattr(project, "cfg", None) or {}) or {}).get("layers") or 2
        new_board(project, copper=int(n) if int(n) in stackup.COUNTS else 2)
    net = os.path.join(project.build, f"{project.stem}.net")
    kicad.netlist(project.sch, net)
    nl = Netlist.load(net)
    libs = {p["footprint"] for p in nl.parts.values() if p["footprint"]}
    dirs, missing = fp_dirs(project, libs)
    req = {"board": project.pcb, "netlist": net, "fp_dirs": dirs, "remove_extra": remove_extra, "save": save}
    live_link = None
    try:
        from .. import live as livemod
        live_link = livemod.link_for(project.pcb)
    except Exception:
        live_link = None
    if live_link is not None:
        live_link.save()                 # keep the user's unsaved edits: save, update the file, reload
    rc, out, err = kicad.kpy("kpy_sync.py", input_json=req, check=False)
    res = kicad.last_json(out) or {"ok": False, "error": (err or out)[-2000:]}
    if missing:
        res.setdefault("errors", []).append("libraries not found in the library tables: " + ", ".join(sorted(missing)))
        res["ok"] = False
    if live_link is not None:
        try:
            live_link.revert()
            res["live_reloaded"] = True
        except Exception as e:
            res["live_reloaded"] = f"reload failed: {e}"
    return res


EMPTY_BOARD = """(kicad_pcb
	(version 20241229)
	(generator "tracewright")
	(generator_version "10.0")
	(general
		(thickness 1.6)
		(legacy_teardrops no)
	)
	(paper "A4")
	(layers
{layers}
		(9 "F.Adhes" user "F.Adhesive")
		(11 "B.Adhes" user "B.Adhesive")
		(13 "F.Paste" user)
		(15 "B.Paste" user)
		(5 "F.SilkS" user "F.Silkscreen")
		(7 "B.SilkS" user "B.Silkscreen")
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(17 "Dwgs.User" user "User.Drawings")
		(19 "Cmts.User" user "User.Comments")
		(21 "Eco1.User" user "User.Eco1")
		(23 "Eco2.User" user "User.Eco2")
		(25 "Edge.Cuts" user)
		(27 "Margin" user)
		(31 "F.CrtYd" user "F.Courtyard")
		(29 "B.CrtYd" user "B.Courtyard")
		(35 "F.Fab" user)
		(33 "B.Fab" user)
	)
	(setup
		(pad_to_mask_clearance 0)
		(allow_soldermask_bridges_in_footprints no)
		(pcbplotparams
			(layerselection 0x00000000_00000000_55555555_5755f5ff)
			(outputformat 1)
			(outputdirectory "")
		)
	)
	(net 0 "")
)
"""


def new_board(project, copper=2):
    """Write an empty board (KiCad upgrades the file to its own format)."""
    if copper <= 2:
        layers = '\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)'
    else:
        inner = "".join(f'\t\t({4 + 2 * i} "In{i + 1}.Cu" signal)\n' for i in range(copper - 2))
        layers = '\t\t(0 "F.Cu" signal)\n' + inner + '\t\t(2 "B.Cu" signal)'
    os.makedirs(os.path.dirname(project.pcb), exist_ok=True)
    with open(project.pcb, "w") as f:
        f.write(EMPTY_BOARD.format(layers=layers))
    try:
        kicad.pcb_upgrade(project.pcb)
    except kicad.KiCadError:
        pass
    return project.pcb
