---
name: routing
description: Route the board like a person - supplies first and wide, pairs coupled and matched, signals short - with the grid router net by net, Freerouting for dense jobs, and hand-laid copper for critical nets. Use for any routing work.
---
# Routing

1. Rules first: declare each supply's voltage and current, pairs' impedance and RF lines with the `nets` tool,
   then `nets` classes with apply (IPC-2221 widths for the currents, IPC-2141 widths and gaps for the
   impedances over this stack-up); clearances within the fab profile (`dfm.rules`).
2. Dense parts first. `routability` (or `./tw routability`) says whether the board can be routed in its room and
   layers: each BGA's escape (fine with a layer to spare; tight when it needs every signal layer), the crowded lines
   across the board, the demand against the free area. Tight or impossible: tell the user the fixes it gives (a
   signal layer more, a larger board, parts moved) before routing. `./tw escape` (board what=escape) has the detail.
   A via that does not fit, or layers the user will not add: offer HDI (vias in pads, microvias; it costs more: the
   user turns it on in Board > Routing).
   On a breakout-style board (free GPIO brought out to connectors), `pins` proposes the pin plan: each free GPIO on
   the connector pin that lies the way it leaves the chip, so they run side by side instead of crossing. Show the
   user the moves and the crossings before and after; apply only with their OK (it changes the schematic and the
   pin map), then bring any pin table the design keeps up to date.
   Then `breakout` with make_room: the parts under each BGA moved off its via spots, a via for every ball that needs
   one, each signal ball's escape out to the edge of its ball field on its layer, and vias beside fine-pitch parts
   whose nets change layer. `route` then takes each net up at its port; escapes it did not use come off again.
   `./tw space` shows where the board is crowded; regions (`region`) keep tracks out of an area, forbid vias, allow
   finer tracks or demand more spacing.
3. Planes and pours: the stack-up's planes (`stackup` show; apply pours them), each signal layer next to one; on
   two layers, GND pours on both sides. The router routes on the signal layers only, each in its direction, and
   keeps a pair's second half on its partner's layers. Keepouts from the data sheets as rule areas.
4. The routing plan (`board` what="routing", the app's Board > Routing tab): every net is auto, guided (routed
   first, keeping its rules: pairs, crystals, clocks, switch nodes, heavy currents) or hand (RF feeds, current-sense
   pairs, high voltage; the router leaves these). The user can move a net between modes and pick the router's preset
   (Balanced, Dense, Few vias, Shortest); honour both. Route hand nets yourself (`copper`), keeping their rules, or
   name them in `route` nets if the user agrees. Critical copper by hand too: the hot loop, pairs at connectors. The
   layer plan (`routing_plan` action layers): pairs and fast lines on the layers beside a ground plane (the plan
   suggests them); the router keeps a planned net to its layers.
5. Supplies with `route --nets ...` (wide classes route first), then pairs, then the rest short-first. The grid
   router streams every net live; watch for nets it reports failed and give them room or route them by hand. Its
   reply names the nets left for hand routing; build/route-report.json has each net's length, vias and notes (a via
   on a line meant to have none).
6. Freerouting (`route` with engine freerouting) for a dense remainder -- then review its result: it ignores
   style (angles, via count).
7. Pairs and length groups are tuned after the route (meanders to within their budget; `./tw tune` again after
   hand edits). `fill`, then `run_checks`: `drc`, `route.style`, `route.quality` (detours, needless vias, dangling copper),
   `power.width` and `power.drop` (declare `checks.power_paths`), `power.pours` (islands, stitching), `hs.pairs`,
   `si.layer_change` (a ground via beside every fast signal via), `si.stubs`, `si.length_groups`, `si.noise`,
   `hdi.vias` and `hdi.fanout` on dense boards.
8. `render` the board and look at it the way a reviewer would.
