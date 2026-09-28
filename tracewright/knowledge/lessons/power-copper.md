---
title: Sizing power copper by the current path
tags: [power, current, ipc-2221, trace-width, pours]
source: Tracewright's checks on a CM5 carrier board (2026-09-26)
---
- Size a supply along the **path the current takes** (source -> load), not per net: a rail's light branches
  to small ICs and decoupling caps carry almost nothing. Declare paths:
  `"checks": {"power_paths": [{"net": "+5V", "from": "L101", "to": "J201", "amps": 5}]}`.
- The narrowest point the current cannot avoid is the **widest-path bottleneck** through tracks, vias and
  pours (a pour or via array counts as wide). Short necks (< 1 mm) into a pad are normal.
- Sense, feedback and enable pins are on supply nets but are not loads: only follow power-in / power-out
  pins, connectors, inductors, fuses, switches and shunts.
- IPC-2221: I = k dT^0.44 A^0.725 (A in mil^2; k 0.048 outer, 0.024 inner). 1 oz = 0.035 mm; JLC 4-layer
  inner copper is 0.5 oz (0.0152 mm) by default -- inner planes carry less than you think.
