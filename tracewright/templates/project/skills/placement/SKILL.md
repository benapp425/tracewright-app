---
name: placement
description: Place the footprints on the board with intent - outline and mounting first, then connectors, the processor, power stages and decoupling - live, in visible steps the user can steer. Use when laying out or rearranging parts.
---
# Placement

Place in four stages and say why for every part. Each `place` call carries the stage (fixed: connectors, holes and
what the floorplan fixed; main: the main chips; support: their decoupling, crystals, pull-ups and filters; rest) and
a `why` on every move: one plain line the user reads when they pick the part ("2 mm from U3 pin 7, GND via beside
it", "at the left edge: the cable comes in there"). Mark a stage done (`done: true`); if the user asked to check
each stage, the tool tells you to stop and wait. Keep the user's constraints (Placement panel: a part near a pin,
parts together, a part away from others, at an edge, on a side) and add your own where they matter (`constraints`).
`board` what=placement shows the score and its parts (short connections, few crossings, decoupling at the pins,
connectors at the edge, constraints kept, hot parts apart): read it after each stage and fix what scores low.

1. The stack-up first (`stackup`): decide the copper layers from the design -- 2 for simple, slow boards; 4 once
   there is anything fast, a fine-pitch part to fan out or EMC to meet; 6 or more for several fast interfaces or a
   BGA with many rows -- which are signal layers (and their routing direction) and which are planes (and their
   nets), and say why in a sentence. Within the agreed layer count; changing it is the user's call.
   `sync_board` (update the board from the schematic) makes the board with that many layers; new parts appear
   parked right of the board. `stackup` apply once the outline is drawn puts the build and the plane pours on.
2. Outline and mounting holes (ask the user about size and shape if it is not in the requirements). With a
   floorplan from a guided start (`./tw floorplan`), `./tw floorplan apply` does this and step 3's positions:
   the outline, each block's area on Dwgs.User, the connectors and holes where the user agreed or dragged them.
3. Connectors on the edges where their cables go, facing out (check the footprint's "PCB edge" mark). Place
   each block's parts inside its floorplan area; `placement.floorplan` checks connectors and holes against it.
4. The processor / main IC, then power stages: switcher, inductor and input capacitors in the tightest loop;
   regulator input and output capacitors at their pins.
5. Decoupling capacitors at the supply pins they serve, same side, short ground return.
6. Crystals right at their pins; sensitive analog away from switch nodes; RF parts per their keep-outs.
7. Everything else by signal flow; refs readable, polarity marks visible.

Work a functional group at a time with `place` (it animates live in KiCad and in the app), say what the group
is and why it sits there, and `show` / `annotate` when you want the user's opinion. After each group run
`run_checks` with `pcb.placement`, `placement.escape` (every pad has room for a track to leave it),
`power.decoupling` and `power.regulators` (plus `power.switcher` for a
converter's hot loop and `pcb.esd` for protection at the connectors), and `render` the board to look at it.
