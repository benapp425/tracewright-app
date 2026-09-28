---
name: schematic-capture
description: Draw or change the schematic - generate it with tw.sch for new designs, or edit the existing sheets in place - to professional conventions (functional blocks, left-to-right flow, notes, filled title blocks, theme colours only) and readability checks. Use for any schematic work.
---
# Schematic capture

**New design** -- write `design/schematic.py` with `tw.sch` (worked example: `tools/tw/examples/demo_board.py`):
- A `catalog()` of `Part(symbol, footprint, value, MPN, manufacturer, LCSC, datasheet)`; stock symbols with
  `stock("Device", "R")`; new ICs with `make_ic(...)` from the data sheet's pin table (name every pin exactly as
  the data sheet does; stacked pins hidden on the visible one).
- Follow the lesson `schematic-conventions`: a cover sheet (`Design(..., title=, company=)`, `d.contents`,
  `notes`), one sheet per function (`d.sheet(..., description=)`), each function in a `block(x1, y1, x2, y2,
  "TITLE")` -- thin, unfilled, no colours. Signal flow left to right; supplies up, ground down.
- Parts where they act: `pin_cap(ic, pin, caps, rail)` for decoupling at the pin, `pull(point, R, rail)` for
  pull-ups on a line, `inline(ic, pin, R)` for series parts, `pin_lab` stubs with labels on busy pins,
  hierarchical labels between sheets. Dashed `zone`s + caption around hot loops, switch nodes, crystals, pairs.
- Notes next to the parts they explain (`note`): the formula for a value, the current, the rating margin, the
  layout constraint.
- Run the script, then `tw.sch.finish(project)` (upgrade + ERC + netlist), then `run_checks` with `sch.*` (incl.
  `sch.style`),
  `bom.*`, `power.decoupling`, `lessons.*`. Look at every sheet with `render`.

**Existing schematic** -- change it in place: `tw.sch.edit.set_fields` for fields (values, MPN, LCSC), or make
the edit in KiCad live. Take a `snapshot` first. If KiCad has the schematic open, save it there before editing
the file and reload it after (the `kicad` tool does both).

Rules: 1.27 mm grid; 3.81 mm pins for 2-digit numbers next to no-connect flags, 5.08 mm for 3 digits; never let
two wires of different nets share a line; one name per rail; PWR_FLAG where a rail enters; every placed part
has Value, Footprint, MPN, Manufacturer, LCSC.
