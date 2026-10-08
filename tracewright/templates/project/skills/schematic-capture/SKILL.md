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
- Notes come in two layers. On the sheet: one plain line an engineer reads at a glance, beside the part it is about:
  `g.note("Boot straps: IO2, IO8 high", near=r202, why="IO2 must be high at reset (ESP32-C3 data sheet ch. 4); IO8 ...")`.
  The reasoning, numbers and data sheet references go in `why=` (or `g.why(part, text)` alone): the design notes,
  kept off the sheet, shown in the app beside the part. No paragraphs and no chapter references on the sheet. For a
  schematic that is not script-drawn, the `notes` tool adds them.
- Draw support parts at the pin they serve: `g.pull(u, "EN", "R10k", "+3V3", net="EN", cap="C1u")` for an RC on an
  enable, `decouple` for supply pins, `series` for line parts. Give every connector pin's net its signal's name, and
  every sheet its title.
- `p.layout()` per page, then `d.write(hw)` and `tw.sch.finish(project)`: it reports `connections` ("as asked", or
  what KiCad's netlist does differently -- fix those before anything else), `crowded` (patterns drawn aside
  because there was no room at the pin: give the group more room or split it) and `plot` (the sheets as KiCad
  plots them: every place text overlaps text, a wire, a symbol or a block border, read from the strokes -- fix each
  one, it is what the user will see), `notes` (notes to rewrite: too long, or not beside a part) and `critic` (notes
  far from their parts, support parts drawn away from their pin, connector pins without names, sheets without titles). The engine sizes text with KiCad's own glyph widths (tw.font), so what it
  places clear is clear on the plot. Signals leaving a group get labels; signals on several sheets get global
  labels, redrawn in the project's style. In the hierarchical style, four or more signals between the same sheets
  travel as one bus ({GPIO_SD}: one pin and one line on the cover page, the signals named on each sheet), and the
  cover page is laid out again around its sheets' pins; leave the sheet symbols' places and sizes to it.
- Decoupling is drawn in a row beside the part by default, each row with the pin it serves under it ("near U1 pin
  P12"), and the layout is asked to keep those parts at that pin (a BGA's small capacitors on the bottom).
- Run it with `./tw schematic`, in the foreground: under a minute even for a big BGA. It prints each sheet as it is
  laid out, then finish's steps, and ends with "Schematic done ..." (report in build/schematic-report.json) or
  "Schematic failed ...". Do not run it in the background or watch it with monitors.

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
- Run `./tw schematic` (the script ends with `tw.sch.finish(project)`: upgrade + the project's style + ERC + netlist), then
  `run_checks` with `sch.*` (incl. `sch.style`, `sch.text` for the notes, and `sch.pinout`: every symbol's pins
  against the real part's pinout), `bom.*` (incl.
  `bom.package`), `power.domains` (parts on different rails wired together), `power.regulators`,
  `power.thermal` (declare the rails' currents), `power.decoupling`, `lessons.*`. Look at every sheet with `render`.

**Existing schematic** -- change it in place: `tw.sch.edit.set_fields` for fields (values, MPN, LCSC),
`set_flags` for DNP, `rename_net` for a net's name, or make the edit in KiCad live. The user edits the same way from
the app (a part's card, a label on the sheet): their edits arrive in your next turn's context -- fold them into
design/schematic.py before you run it again, or the script puts the old values back. Take a `snapshot` first. If KiCad has the schematic open, save it there before editing
the file and reload it after (the `kicad` tool does both). `./tw style` shows how the sheets are joined;
`./tw style flat` or `./tw style hierarchical` redraws them the other way, proved against KiCad's netlist.

Rules: 1.27 mm grid; 3.81 mm pins for 2-digit numbers next to no-connect flags, 5.08 mm for 3 digits; never let
two wires of different nets share a line; one name per rail; PWR_FLAG where a rail enters; every placed part
has Value, Footprint, MPN, Manufacturer, LCSC.
