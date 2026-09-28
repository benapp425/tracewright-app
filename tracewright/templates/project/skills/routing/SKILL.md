---
name: routing
description: Route the board like a person - supplies first and wide, pairs coupled and matched, signals short - with the grid router net by net, Freerouting for dense jobs, and hand-laid copper for critical nets. Use for any routing work.
---
# Routing

1. Rules first: net classes with the right widths (IPC-2221 for each supply's current path), clearances within
   the fab profile (`dfm.rules`), pair classes with computed impedance.
2. Planes and pours: on four layers, inner GND (and power) planes; on two layers, GND pours on both sides.
   Keepouts from the data sheets as rule areas.
3. Critical nets by hand (`copper`): switch nodes, the hot loop, pairs at connectors, sense lines.
4. Supplies with `route --nets ...` (wide classes route first), then pairs, then the rest short-first. The grid
   router streams every net live; watch for nets it reports failed and give them room or route them by hand.
5. Freerouting (`route` with engine freerouting) for a dense remainder -- then review its result: it ignores
   style (angles, via count).
6. `fill`, then `run_checks`: `drc`, `route.style`, `power.width` (declare `checks.power_paths`), `hs.pairs`.
7. `render` the board and look at it the way a reviewer would.
