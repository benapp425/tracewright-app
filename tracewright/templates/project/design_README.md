# design/

Scripts that make or change the design, so every step can be re-run and reviewed:

- `schematic.py` -- the schematic generator (tw.sch). Run it, then `tw.sch.finish(project)` (ERC + netlist).
  Once the schematic has been edited by hand in KiCad, change the files in place instead, or fold the
  hand edit into this script first -- never regenerate over someone's work.
- `placement.py` -- the placement table with the reason for each position.
- `checks/` -- project-specific checks (data sheet pin tables, land patterns, interface rules).
