---
name: schematic-capture
description: Draw or change the schematic - generate it with tw.sch for new designs, or edit the existing sheets in place - to professional conventions (functional blocks, left-to-right flow, notes, filled title blocks, theme colours only) and readability checks. Use for any schematic work.
---
# Schematic capture

**New design** -- write `design/schematic.py` and let `tw.sch.auto` lay the sheets out (worked example:
`tools/tw/examples/auto_demo.py`). Say what connects, grouped by function; place no coordinates:
- `p = Page(d, sheet, base=100, catalog=CAT)`; `g = p.group("3.3 V REGULATOR")` (one titled block per function);
  `u = g.part("LDO", "U", ref="U1")` -- the group's main part; later parts go to its right. A single-row
  connector faces its circuit by itself (`face="auto"`: mirrored so its pins point right when it opens the group,
  left when it follows other parts; pin 1 stays on top) -- no need to rotate connectors by hand.
- On a part's pins: `g.power(u, pins, "+3V3", flag=False)` (supplies up, ground down; sideways at connectors;
  several pins of one rail on the same side share a bar and one symbol),
  `g.decouple(u, pin, ["C100n", "C10u"], "+3V3")`, `g.pull(u, pin, "R10k", "+3V3" or "GND", net="RESET")`,
  `g.series(u, pin, "R22", "USB_D_P", before="MCU_D_P")`, `g.indicator(u, pin, "R1k", "LED_G")`,
  `g.crystal(u, "XI", "XO", "Y16M", ["C18p", "C18p"])`, `g.net(u, pins, "I2C_SDA")`, `g.nc(u, pins)`.
- Alone in a group: `g.led("+3V3", "R1k", "LED_R")`; a divider: `g.divider("VBAT", "VBAT_SENSE", "R100k", "R33k",
  cap="C100n", kind="analog")` (rail, resistor, the tap labelled, resistor, ground; the filter capacitor beside);
  notes: `g.note("C2 22u keeps the LDO stable.", near="C2")`.
- `p.layout()` per page, then `d.write(hw)` and `tw.sch.finish(project)`: it reports `connections` ("as asked", or
  what KiCad's netlist does differently -- fix those before anything else) and `crowded` (patterns drawn aside
  because there was no room at the pin: give the group more room or split it). Signals leaving a group get
  labels; signals on several sheets get global labels, redrawn in the project's style.

For what the patterns cannot draw, the low-level builder (`tools/tw/examples/demo_board.py`):
- A `catalog()` of `Part(symbol, footprint, value, MPN, manufacturer, LCSC, datasheet)`; stock symbols with
  `stock("Device", "R")`; new ICs with `make_ic(...)` from the data sheet's pin table (name every pin exactly as
  the data sheet does; stacked pins hidden on the visible one).
- Follow the lesson `schematic-conventions`: a cover sheet (`Design(..., title=, company=)`, `d.contents`,
  `notes`), one sheet per function (`d.sheet(..., description=)`), each function in a `block(x1, y1, x2, y2,
  "TITLE")` -- thin, unfilled, no colours. Signal flow left to right; supplies up, ground down.
- Parts where they act: `pin_cap(ic, pin, caps, rail)` for decoupling at the pin, `pull(point, R, rail)` for
  pull-ups on a line, `inline(ic, pin, R)` for series parts, `pin_lab` stubs with labels on busy pins,
  hierarchical labels between sheets. Dashed `zone`s + caption around hot loops, switch nodes, crystals, pairs.
  If the project's schematic style is flat, draw it this way anyway: `finish` redraws the sheets flat.
- Notes next to the parts they explain (`note`): the formula for a value, the current, the rating margin, the
  layout constraint -- a line or two, written like an engineer's markup. The cover: the board's name, one line on
  what it is, `d.contents`, a short numbered `notes` list, revisions. Never: tool instructions, explanations of
  how labels work, part counts, text inside sheet symbols, review checklists or firmware requirements (docs/).
- Run the script, then `tw.sch.finish(project)` (upgrade + the project's style + ERC + netlist), then
  `run_checks` with `sch.*` (incl. `sch.style`, `sch.text` for the notes, and `sch.pinout`: every symbol's pins
  against the real part's pinout), `bom.*` (incl.
  `bom.package`), `power.domains` (parts on different rails wired together), `power.regulators`,
  `power.thermal` (declare the rails' currents), `power.decoupling`, `lessons.*`. Look at every sheet with `render`.

**Existing schematic** -- change it in place: `tw.sch.edit.set_fields` for fields (values, MPN, LCSC), or make
the edit in KiCad live. Take a `snapshot` first. If KiCad has the schematic open, save it there before editing
the file and reload it after (the `kicad` tool does both). `./tw style` shows how the sheets are joined;
`./tw style flat` or `./tw style hierarchical` redraws them the other way, proved against KiCad's netlist.

Rules: 1.27 mm grid; 3.81 mm pins for 2-digit numbers next to no-connect flags, 5.08 mm for 3 digits; never let
two wires of different nets share a line; one name per rail; PWR_FLAG where a rail enters; every placed part
has Value, Footprint, MPN, Manufacturer, LCSC.
