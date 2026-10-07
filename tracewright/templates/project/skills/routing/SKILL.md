---
name: routing
description: Route the board like a person - supplies first and wide, pairs coupled and matched, signals short - with the grid router net by net, Freerouting for dense jobs, and hand-laid copper for critical nets. Use for any routing work.
---
# Routing

1. Rules first: declare each supply's voltage and current, pairs' impedance and RF lines with the `nets` tool,
   then `nets` classes with apply (IPC-2221 widths for the currents, IPC-2141 widths and gaps for the
   impedances over this stack-up); clearances within the fab profile (`dfm.rules`).
2. Dense parts first: `./tw escape` (board what=escape) says how each BGA gets its pins out -- tracks between its balls,
   whether a via fits between four, how many signal layers its rings need. Too few layers: say so and plan more (up to
   12). A via that does not fit, or layers the user will not add: offer HDI (vias in pads, microvias; it costs more:
   the user turns it on in Board > Routing). Then `fanout` the part before routing; the router starts from its vias.
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
7. `fill`, then `run_checks`: `drc`, `route.style`, `route.quality` (detours, needless vias, dangling copper),
   `power.width` and `power.drop` (declare `checks.power_paths`), `power.pours` (islands, stitching), `hs.pairs`,
   `si.layer_change` (a ground via beside every fast signal via), `si.stubs`, `si.length_groups`, `si.noise`,
   `hdi.vias` and `hdi.fanout` on dense boards.
8. `render` the board and look at it the way a reviewer would.
