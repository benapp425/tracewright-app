---
name: placement
description: Place the footprints on the board with intent - outline and mounting first, then connectors, the processor, power stages and decoupling - live, in visible steps the user can steer. Use when laying out or rearranging parts.
---
# Placement

1. `sync_board` (update the board from the schematic): new parts appear parked right of the board.
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
`run_checks` with `pcb.placement`, `power.decoupling` and `power.regulators` (plus `power.switcher` for a
converter's hot loop and `pcb.esd` for protection at the connectors), and `render` the board to look at it.
