---
title: Common circuit mistakes that pass ERC
tags: [schematic, i2c, led, reset, usb, decoupling, crystal, load-switch, module]
source: accumulated review experience
---
- **I2C** needs pull-ups (2.2 k - 10 k to the bus supply) somewhere; say where if they are off-board.
- **LEDs** need a current limit (series resistor or a constant-current driver pin).
- **Reset / enable / boot pins** must be tied or driven; a no-connect flag on them is a bug.
- **Every supply pin gets its own small capacitor** at the pin (0.1 uF or what the data sheet says), plus
  the bulk the regulator's data sheet asks for at its input and output.
- **Switching regulators**: the hot loop (input cap - switch - ground) is the smallest loop on the board;
  feedback divider next to the FB pin, its ground to the quiet ground; the switch node small.
- **Crystals**: load capacitors from the data sheet's CL and the stray estimate; nothing routed between
  the pads or under the crystal.
- **Voltage ratings**: ceramic capacitors lose most of their capacitance near rated voltage (use 2x the
  rail); TVS stand-off above the maximum operating voltage.
- **Load switches and USB power switches need their input capacitor at VIN**, not just somewhere on the rail.
  The RT9742 data sheet (p. 13, 15) warns that without it an output short can ring the input past its absolute
  maximum and destroy the switch; it asks for the ceramic "as close as possible to the VIN pins" (10 uF typical).
  Found by the decoupling check on a CM5 carrier (the nearest +3V3 capacitor was 50 mm away).
- **Pull-ups can live inside a module.** The Raspberry Pi CM5 has 1.8 k pull-ups on GPIO2/3 (I2C1, to GPIO_VREF)
  and on SDA0/SCL0 (camera / display I2C, to CM5_3.3V) -- CM5 datasheet p. 12, 19, 20 -- so the carrier needs
  none there; declare such nets in tracewright.json `checks.external_pullups` and note the source, rather than
  adding a second set. Check other modules' data sheets the same way.
- **Not every power pin must be connected**: read the data sheet before "fixing" a no-connect on a supply
  pin. u-blox SAM-M10Q V_BCKP "should be left open if not used" (integration manual UBX-22020019, section 3.5).
