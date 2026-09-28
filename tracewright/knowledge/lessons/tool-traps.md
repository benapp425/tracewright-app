---
title: Tool behaviours worth knowing (Freerouting, kicad-cli, EasyEDA)
tags: [freerouting, kicad-cli, tools, autorouter, privacy]
source: Tracewright development (2026-09-26)
---
- **Freerouting 2.x** opens its GUI for `--help` (no text help). Run it headless with
  `-de in.dsn -do out.ses -mp N -mt N -da -dct 0 --gui.enabled=false --api_server.enabled=false`;
  `-da` turns off its usage analytics, which are on by default. KiCad 10's Python still has
  `pcbnew.ExportSpecctraDSN(board, path)` and `ImportSpecctraSES(board, path)`.
- **kicad-cli has no "update PCB from schematic"**; do it with pcbnew (the toolkit's `./tw sync`).
- **kicad-cli pcb drc --refill-zones** gives a DRC of the board as it will be filled; without it, unfilled
  zones produce false unconnected items.
- **kicad-cli sch export svg** names sheets `<project>-<sheet name>.svg`; `--exclude-drawing-sheet` leaves
  out the frame; `--theme _builtin_default` keeps colours predictable.
- **Generated-code autorouters can ignore clearance and width settings** (tscircuit: byte-identical copper
  whatever the clearance; 2.5 mm power traces came back 0.127 mm). Always DRC the result against the real
  rules and check power widths separately.
- **Disabling placement DRC hides real collisions** (tscircuit `placementDrcChecksDisabled` masked eleven
  overlapping pairs). Never switch a check off without an independent replacement.
- **Unrestricted rule areas stall Freerouting.** KiCad exports a rule area that restricts nothing (one that only
  names a region for a custom DRC rule, like the router's "TW neck" areas) as a hard DSN keepout; every pass
  then leaves the same connections unrouted. The toolkit drops such areas from a copy before exporting.
- **KiCad's IPC API (live link)** is off by default: Preferences > Plugins > Enable KiCad API (kicad_common.json
  `api.enable_server`), then restart KiCad. The socket is /tmp/kicad/api.sock; kicad-python's version must
  match KiCad's (0.8.0 for 10.0.6). Board edits made through it are undoable in KiCad; call save() before
  editing the file some other way, and revert() after, or the two copies diverge.
- **Quit KiCad before editing its settings files** (kicad_common.json and friends): a running KiCad keeps its
  own copy and can write it back when it quits, undoing the edit. Tracewright's "Turn on KiCad's API" button
  re-applies the setting once KiCad has exited for this reason.
