---
name: routing
description: Route the board like a person - supplies first and wide, pairs coupled and matched, signals short - with the grid router net by net, Freerouting for dense jobs, and hand-laid copper for critical nets. Use for any routing work.
---
# Routing

1. Rules first: declare each supply's voltage and current, pairs' impedance and RF lines with the `nets` tool,
   then `nets` classes with apply (IPC-2221 widths for the currents, IPC-2141 widths and gaps for the
   impedances over this stack-up); clearances within the fab profile (`dfm.rules`).
2. Planes and pours: on four layers, inner GND (and power) planes; on two layers, GND pours on both sides.
   Keepouts from the data sheets as rule areas.
3. Critical nets by hand (`copper`): switch nodes, the hot loop, pairs at connectors, sense lines.
4. Supplies with `route --nets ...` (wide classes route first), then pairs, then the rest short-first. The grid
   router streams every net live; watch for nets it reports failed and give them room or route them by hand.
5. Freerouting (`route` with engine freerouting) for a dense remainder -- then review its result: it ignores
   style (angles, via count).
6. `fill`, then `run_checks`: `drc`, `route.style`, `route.quality` (detours, needless vias, dangling copper),
   `power.width` and `power.drop` (declare `checks.power_paths`), `power.pours` (islands, stitching), `hs.pairs`,
   `si.layer_change` (a ground via beside every fast signal via), `si.stubs`, `si.length_groups`, `si.noise`.
7. `render` the board and look at it the way a reviewer would.
