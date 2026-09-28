---
title: Keep the design simple (user preference)
tags: [preference, architecture, power, reliability]
source: a designer's review of a CM5 carrier board
---
Do not overcomplicate the board with load switches and other parts that can stop it working. Add a load
switch, sequencer, ideal diode, eFuse or extra protection only when a requirement or a data sheet calls for
it, and say why. A reverse-polarity PFET, a fuse and a TVS at the battery input is the usual floor for a
battery-powered board; beyond that, justify every part. Fewer parts: fewer footprints to verify, fewer JLC
stock risks, fewer bring-up surprises.
