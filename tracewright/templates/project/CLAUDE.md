# {{name}}

A Tracewright PCB project. KiCad project: `{{kicad_project}}`.

@.claude/tracewright.md

## Where things are

- `BRIEF.md` -- the request the project started from (projects begun in Tracewright); `docs/requirements.md`
  is the agreed version. An imported project keeps its own documents in `docs/`.
- `docs/` -- architecture, decisions (one entry per decision, with the reason), parts (with sources and
  dated stock queries), review, bring-up plan.
- `design/` -- scripts that generate or change the design (schematic generators, placement tables);
  `design/checks/` -- project-specific checks, each with a planted-fault test.
- `tools/tw/` -- this project's copy of the toolkit; run it with `./tw` (see `.claude/tracewright.md`). The app
  replaces it when it updates: work around a shortfall in your own script under `design/` and record a lesson,
  rather than editing it here (edits here are set aside, not kept, at the next update).
- `build/` -- generated reports, plots and fab files (not in git; regenerate with `./tw check`, `./tw outputs`).
- `.claude/knowledge/` -- lessons learned on earlier boards. `.claude/skills/` -- how-tos for each stage.
- Stages have gates: the `stage` tool only marks a stage done (or moves on past it) when its gate holds -- the
  requirements written, the checks run on the design as it is now with no errors, nothing unrouted, the
  user's sign-off for the release -- and says what is missing otherwise. Waive findings with the `waive`
  tool (with a plain title), never by editing tracewright.json; record what shows each requirement is met with
  the `evidence` tool as you verify. Where a circuit's behaviour is the requirement (a filter's corner, a
  divider or sense amplifier's output, an RC delay, an LED's current, a supply's start-up), show it with the
  `simulate` tool (KiCad's ngspice): it keeps the netlist and plot in docs/sim and records the result as the
  requirement's evidence. The regulators' heat (power.thermal) goes with the temperature requirements by itself.
- The laid-out board can be simulated (`board_sim`, the user's Simulate tab): a rail's voltage drop and current
  density through its copper, the board's heat from the parts that get warm, where a fast net's return current runs
  and the gaps it detours round, a net's impedance and edge at the far end (and the series resistor that tames it),
  a rail's impedance against its target, and a block of the schematic in ngspice. Run them before calling a layout
  done on anything with heavy currents, fast edges or a dense supply; say what you assumed (currents, edges).
- Getting it built (`make`, the user's Parts > Make): the BOM's health, the test points to add, the fab and
  assembly drawings, a V-scored panel, the enclosure fit and an OpenSCAD box. The user's block library (`blocks`)
  holds circuits that worked before: prefer one over drawing the same circuit afresh, and offer to save a new
  circuit that works as a block.
- Schematic notes: one plain line on the sheet beside the part it explains; the reasoning goes in the design notes
  (`why=` in the schematic script, or the `notes` tool), which the app shows beside the part. Sheets are joined
  hierarchically (sheet pins) unless the project says flat (tracewright.json schematic.style).
- The copper layers (2 to 12) are a plan you make with the `stackup` tool -- signal layers and their routing
  directions, planes and their nets, the fab's build -- before the board is made; the router follows it.
