---
title: Generating schematics with code (tw.sch)
tags: [schematic, generator, kisch, layout, readability]
source: a generated CM5 carrier schematic (2026-09-23/25)
---
- Emit KiCad 9 S-expressions and let `kicad-cli sch upgrade` rewrite them natively (also proves they parse).
- 1.27 mm grid everywhere; rectangles snap to it.
- A local label sits above its wire, a hierarchical label is centred on it.
- Field placement: small 2-pin parts beside themselves, ICs/connectors above the body; move fields off
  wires explicitly when a gate or enable wire runs where the value text would be.
- Space parallel capacitors 15.24 mm apart so "22u 25V" values do not run into the next part.
- Zones (dashed outlines) must include the parts' field boxes and stop at pin tips, or their borders cut
  labels; check every sheet with `sch.render` after each change.
- No tints or colour keys: blocks are thin unfilled boxes with titles, in the theme's colours
  (lesson `schematic-conventions`). `pin_cap`, `pull` and `inline` draw decoupling, pull-ups and series
  parts where they act, with the junction dots.
- Deterministic UUIDs (hash of sheet + reference) keep diffs small between generations.
