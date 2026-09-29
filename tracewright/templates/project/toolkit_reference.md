# Toolkit reference (./tw)

Run in the project folder. Everything writes its results to `build/` and prints a summary.

| Command | What it does |
|---|---|
| `./tw status` | project files, board summary, last check verdict |
| `./tw check [ids...]` | the design checks -> build/checks.json, checks_report.md, readiness.md (`--list`, `--refresh`, `--offline`) |
| `./tw selftest` | plant one fault per check in a copy of the fixture board; every check must catch its fault |
| `./tw erc`, `./tw drc` | KiCad's own checks (JSON in build/) |
| `./tw netlist` | build/<name>.net |
| `./tw svg` | plot every schematic sheet to build/sch_svg/ |
| `./tw render --sheet /Power/ [--region x0,y0,x1,y1] [-o f.png]` | PNG of a sheet (page mm) |
| `./tw render --board [--layers F.Cu,B.Cu] [--region ...]` | PNG of the board |
| `./tw sync` | update the board from the schematic (keeps placement and routing; new parts parked right of the board) |
| `./tw place moves.json` | apply `[{"ref": "U1", "x": 10, "y": 20, "rot": 90, "side": "F"}]` |
| `./tw pcb ops.json` | apply board operations (below) |
| `./tw route [--nets A B] [--clear] [--engine grid|freerouting]` | route (grid router: human style, net by net) |
| `./tw fill` | refill zones |
| `./tw firmware` | a firmware starter from the schematic: `firmware/pins.h` (each MCU pin's net as a constant in the Arduino core's naming, with what it drives), `firmware/PINS.md`, and a bring-up sketch (written once; LEDs, I2C scan, buttons, analog) |
| `./tw floorplan` / `./tw floorplan apply [--outline]` | the guided start's floorplan in board coordinates (outline, holes, each connector on its edge, the blocks' areas); `apply` draws the outline when the board has none, the areas on Dwgs.User (group "Floorplan"), and moves the connectors and holes on the board to their places (turn each connector to face off its edge) |
| `./tw nets` / `./tw nets set VPYRO kind=power voltage=8.4 current=5` / `./tw nets classes [--apply]` | the net model: what each net is (declared or inferred); declare a net; net classes sized for the declared currents and impedances |
| `./tw parts search "AMS1117-3.3"` / `./tw parts code C6186` | JLC / LCSC data, cached with the query date |
| `./tw cpl [--write]` | CPL against JLC's own footprints; --write stores corrections |
| `./tw outputs [--no-renders]` | Gerbers, drill, BOM, CPL, PDFs, STEP, renders, release zip |

## Board operations (`./tw pcb ops.json`, or the app's `copper` / `place` tools)

Coordinates are board millimetres, KiCad axes (y down), angles counter-clockwise in degrees.

```json
[{"op": "outline", "rect": [100, 100, 150, 135], "radius": 2},
 {"op": "move", "ref": "U1", "x": 120, "y": 117.5, "rot": 0, "side": "F"},
 {"op": "track", "net": "GND", "layer": "F.Cu", "a": [110, 110], "b": [115, 110], "w": 0.3},
 {"op": "via", "net": "GND", "x": 115, "y": 110, "d": 0.6, "drill": 0.3},
 {"op": "zone", "net": "GND", "layers": ["F.Cu", "B.Cu"], "polygon": [[100, 100], [150, 100], [150, 135], [100, 135]]},
 {"op": "rule_area", "name": "Y1 keepout", "layers": ["F.Cu"], "polygon": [...], "no_tracks": true, "no_vias": true, "no_pour": true},
 {"op": "delete", "nets": ["SDA"], "kinds": ["track", "via"]},
 {"op": "text", "text": "REV A", "x": 140, "y": 132, "layer": "F.SilkS", "size": 1.0},
 {"op": "fill"}]
```

## Python API (for design/ scripts; `import sys; sys.path.insert(0, "tools")`)

- `tw.env.project()` -- paths of this project (`.sch`, `.pcb`, `.build`, `.setting("fab.layers")`).
- `tw.board.Board.load(path)` -- the board as geometry: `footprints[ref].pads[i].poly`, `tracks`, `vias`,
  `zones`, `outline`, `summary()`.
- `tw.schematic.Hierarchy.load(path)` -- sheets, symbols (ref, value, bbox, pins with positions), labels.
- `tw.netlist.Netlist.load(path)` -- `parts`, `nets`, `net_of(ref, pin)`, `pin_name`, `pin_type`, `net_class`.
- `tw.sch` -- generate schematics: `Design(name, title=, company=, rev=, comments=)` (`root`, `sheet(...,
  description=)`, `contents`), `Builder` (`two`, `ic`, `pin_lab`, `pin_rail`, `pin_gnd`, `pin_cap`, `pull`, `inline`,
  `tee`, `block`, `zone`, `note`, `notes`), `Part`, `stock("Device", "R")`, `make_ic(...)` for new ICs,
  `finish(project)`. Drawn to the lesson `schematic-conventions` (theme colours, unfilled titled blocks).
  See `tools/tw/examples/demo_board.py` for a complete worked example.
- The net model: `Design.net("VPYRO", kind="power", voltage=8.4, current=5)`, `g.power(u, "3", "+3V3", voltage=3.3,
  current=0.2)`, `g.net(q, "3", "PYRO_MAIN", current=5)` in a schematic script; `tw.netmodel.for_project(p)` reads it
  (kinds: power, ground, pair, clock, fast, rf, analog, signal; fields: voltage, current, pair, iface, impedance,
  class, note). A check (`nets.model`) flags declarations that match no net and supplies with no voltage.
- `tw.sch.edit.set_fields(project, {"R1": {"LCSC": "C25744"}})` -- change fields in an existing schematic,
  keeping the file's formatting.
- `tw.pcb.client.apply(project, ops)` -- the board operations above (live into KiCad when it has the board open).
- `tw.route.driver.route(project, nets=[...], on_progress=print)` -- the grid router.
- `tw.checks` -- write project checks in `design/checks/*.py`:

```python
from tw.checks import check, Finding

@check("project.u1_pins", "U1 pins match the TPS62A02 data sheet table", "Parts & BOM", needs=("netlist",))
def u1_pins(ctx):
    table = {"1": "EN", "2": "GND", "3": "SW", "4": "VIN", "5": "FB"}   # data sheet p.3, rev C
    out = []
    for pin, want in table.items():
        got = ctx.netlist.pin_name("U1", pin)
        if got != want:
            out.append(Finding("project.u1_pins", "error", f"U1 pin {pin} is {got!r}, data sheet says {want}"))
    return out
```

and a planted-fault test next to it (`design/checks/test_<name>.py`) that mutates the netlist
(`ctx.netlist.mutated({("U1", "3"): "GND"})`) and asserts the check reports it.
