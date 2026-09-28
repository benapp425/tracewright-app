---
title: Footprints and connectors that look right and are not
tags: [footprint, connector, ffc, raspberry-pi, usb-c, standoff, verification]
source: a CM5 carrier board (2026-09-23/24)
---
Each of these passes ERC and DRC.

1. **Raspberry Pi camera FFC (Hirose FH12-22S / 15-pin)**: Raspberry Pi's own footprint numbers the pads
   the opposite way to KiCad's stock one of the same name. The RPi CSI pinout is pin 1 GND ... last pin
   3V3; with the stock footprint 3V3 and GND trade places on the cable. FH12 is bottom-contact, front-flip:
   the cable enters on the actuator side with its conductors facing down. (`lessons.rpi_ffc` checks the
   pin 1 / last pin nets.)
2. **Standoff part numbers**: Wurth 9774040243R is an **M2** 4 mm SMT spacer, not M2.5 (that is
   9774040151R). Check the drawing's size/type before trusting a standoff MPN; PEM-style SMT standoffs want a
   specific board hole (e.g. 4.22 mm +0.08) and pad.
3. **Collinear wires join nets**: a USB-C D+ jumper drawn along the D- wire merged the pair with no ERC
   error (all pins passive). (`sch.wiring`.)
4. **USB-C receptacles**: A6/B6 are D+, A7/B7 D-; both rows must be joined. Each CC pin needs its own
   5.1 k pull-down on a sink. (`lessons.usb_c`.)
5. **Verify pad 1, pitch, pad size and the numbering direction** against the vendor land pattern for every
   connector; fit every pad (mounting pads too), not only the numbered ones.
