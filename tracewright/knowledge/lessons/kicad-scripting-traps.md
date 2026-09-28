---
title: KiCad scripting and generated-file traps
tags: [kicad, pcbnew, swig, zones, schematic, scripting]
source: a CM5 carrier board, KiCad 10.0.6 (2026-09-24/26)
---
Things that silently mislead code that reads or writes KiCad files.

1. **Zone fills are "fractured".** `zone.GetFilledPolysList(layer)` returns outlines whose holes are
   joined to the boundary by zero-width slits. Point-in-polygon on those misreads points on a slit as
   outside the copper. Copy and unfracture first: `s = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(l)); s.Unfracture()`.
   The `filled_polygon` records in the .kicad_pcb file are fractured the same way.
2. **Destroying the Python wrapper of a removed item corrupts SWIG's type table.** After
   `board.Remove(item)`, once that wrapper is garbage-collected, later getters (`fp.GetPosition()`) return
   raw `SwigPyObject`s. Collect lists before removing, keep removed items referenced until the process
   ends, and leave with `os._exit(0)` after saving so no destructor runs at shutdown.
3. **Rule areas used in DRU conditions must cover every copper layer they should govern**; an area on
   F/B only leaves inner-layer via pads outside it.
4. **Per-net keepouts need DRU, not rule-area flags** ("no vias" blocks GND stitching too). Use a named
   area and `(rule ... (condition "A.intersectsArea('X') && A.NetName != 'GND'") (constraint disallow track via))`.
5. **Schematic fills have no fixed draw order among rectangles** (kicad-cli plots sort by type). If a fill is
   ever needed, draw the under-tint as an empty `text_box` with a fill and overlays as filled `rectangle`s --
   but professional sheets use no fills at all (lesson `schematic-conventions`).
6. **Grid routers vs off-grid hand copper**: a 0.1 mm-grid route can end 0.05 mm off a hand track's
   centre line; snap parallel ends to the track's end, and skip ends already on a centre line (hand Ts).
7. **Lint the plotted sheet, not estimated text widths.** kicad-cli SVG export gives page millimetres and
   every text as `<g class="stroked-text"><desc>TEXT</desc>` glyph strokes: box each text by its own
   strokes. KiCad plots some fields twice (dedupe); consecutive lines of one note touch by design.
8. **`pcbnew.SaveBoard` of a new BOARD() rewrites the .kicad_pro** with defaults (net classes, rules). Save
   the project file's bytes before and put them back after.
9. **Footprint paths for "Update PCB from Schematic"**: `fp.SetPath(KIID_PATH(sheet_tstamps + symbol_tstamp))`
   from the netlist's `(sheetpath (tstamps ...))` and `(tstamps ...)`, or KiCad's own update treats every part
   as new.
10. **KiCad 10 board files name nets directly** (`(net "GND")`); KiCad <= 9 used codes and a net table.
11. **Pad angles in the file are absolute** (footprint rotation included); pad positions are footprint-local.
12. **Rotating a footprint to a non-90-degree angle rewrites its rectangles as polygons** (fp_rect -> fp_poly)
    and turning it back does not restore them: the geometry is the same, the file is not. Compare boards
    geometrically, not textually, after such moves.
13. **KiCad 10 netlists write a pin's function as `<name>_<number>`** (`(pinfunction "VCC_8")`, `"EN_1"`);
    KiCad 8/9 wrote just the name. Strip the `_<pin number>` suffix before comparing pin names with a data
    sheet table, or every name-based check silently stops matching.
14. **A pin with a no-connect flag has `+no_connect` appended to its netlist pin type**
    (`(pintype "open_collector+no_connect")`, `"power_in+no_connect"`). Split it off before comparing types, or
    an output left open on purpose looks like a floating input to every type-based check.
