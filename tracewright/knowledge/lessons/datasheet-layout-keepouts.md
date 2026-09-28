---
title: Datasheet layout notes need keepouts in the footprint
tags: [footprint, keepout, crystal, sensor, gnss, layout, pour]
source: a CM5 carrier board's parts audit (2026-09-24)
---
Vendor "no copper here" notes are violated by zone pours even when tracks and vias are kept out. DRC and a
pad-geometry check both pass.

- **32 kHz crystal (Epson FC-135R)**: "do not design any circuit patterns in the shaded area" between the
  pads. The stock footprint has the right lands but no keepout; the router ran a track and a via there and
  the GND pour flooded the gap. Fix: a footprint rule area over the pad gap (no tracks, vias or pour).
- **Pressure sensor (Bosch BMP581)**: no vias or traces under it and no solder mask under the part. A small
  keepout stopped tracks and vias, but the pour still reached into the whole-body mask opening. Fix: a
  second rule area the size of the mask opening, no pour and no vias (tracks allowed for pad escapes).
- **GNSS module (u-blox SAM-M10Q)**: no signal traces under the module but GND vias are recommended (a plain
  keepout blocks them: use a DRU rule allowing only GND); layer changes of its lines 20 mm from the edge;
  nothing within 10 mm of the antenna edges.

How to apply: read the layout notes as well as the land drawing, and sample the finished copper *including
fills* inside the forbidden area (`lessons.crystal` does this for crystals).
