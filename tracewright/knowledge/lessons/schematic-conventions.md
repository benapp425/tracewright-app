---
title: How professional schematics are drawn (conventions)
tags: [schematic, style, conventions, labels, layout, readability, standards, colour]
source: research 2026-09-26 -- IEEE 315 / ASME Y14.44 / IPC-2612-1 / IEC 60062, KiCad KLC, industry guides; checked against Raspberry Pi's CM5IO KiCad design
---
**Layout**
- Signal flows **left to right**: input connectors and inputs on the left, processing in the middle,
  outputs and output connectors on the right; feedback runs right to left.
- **Supplies up, ground down**: higher voltages toward the top of the sheet; supply symbols point up,
  ground symbols point down and are never upside down; negative rails point down. Sideways power
  symbols are fine at connector pins only.
- **One function per sheet** (power, processor, each interface group), A4 or A3 landscape. The first
  sheet is the cover: what the board is, a contents list of the sheets with what each holds, the
  general notes, revision notes.
- Inside a sheet, **group each function in a thin, unfilled box with a title** (2 mm text, top centre),
  leaving white space between groups. No tints.
- **Parts sit where they act**: each decoupling capacitor wired to the power pin it serves (not on a
  distant power symbol), the pull-up at the line it pulls, the crystal at its oscillator pins, the
  feedback divider at FB, the series resistor in line with its pin. The schematic shows the layout
  engineer what goes together.
- Everything on the **1.27 mm (50 mil) grid**; no wire through a symbol or text; as few crossings as
  possible (a jog is better than a crossing).

**Wires and names**
- A **junction dot on every T**; never a four-way junction (split it into two Ts a grid step apart).
  Wires that cross without a dot are not connected.
- **Power symbols** for rails and ground, never long supply wires across the sheet.
- On busy ICs and connectors, a **short stub and a local net label** on each pin beats a long wire.
  **Hierarchical labels** (sheet pins) carry signals between sheets so the structure shows which
  signals cross which sheets; global labels are for power only.
- **Net names**: UPPERCASE with underscores (I2C_SDA, SPI_MOSI, USB_D_P / USB_D_N for a pair);
  active-low with the overbar on the library pin name (~{RESET}) and a trailing _N on the net
  (RESET_N) -- one convention per project; buses as prefix + index (D0..D7). Labels read left to
  right (horizontal text).
- A **no-connect flag** on every unused pin.

**Colour and text**
- **No custom colours.** Leave colours at the default so KiCad's theme draws them and the PDF prints
  cleanly in black and white; colour must never carry meaning (monochrome prints, colour-blind
  readers). Raspberry Pi's CM5IO sheets use no custom colours and no fills at all.
- **Text 1.27 mm** (50 mil) for fields, labels and notes, 2 mm for block titles, nothing below 1 mm.

**Designators and values**
- Class letters (IEEE 315, ASME Y14.44, IPC-2612-1 appendix A): R resistor, RN network, C capacitor,
  L inductor, FB ferrite bead, D diode or LED, Q transistor, U IC or module, J connector (P plug),
  SW switch, Y crystal, F fuse, BT battery, TP test point, JP jumper, K relay, T transformer,
  H mounting hole.
- Number by sheet (R101 on sheet 1, R201 on sheet 2) and annotate left to right, top to bottom.
- One value notation throughout: IEC 60062 (4k7, 100n, 4u7) or decimals (4.7k, 100nF). Add the
  voltage rating to bulk capacitors and to any on a rail above about 6 V ("22u 25V"), the tolerance
  to precision resistors ("10k 1%"). Package and dielectric go in fields, not in the value.
- Parts not fitted stay on the sheet, marked DNP (KiCad draws the cross).

**Documentation**
- **Title block on every sheet**: title (board - sheet), revision, date, company or author, a comment.
- **Notes beside what they explain**: why this value, the current, the rating margin, the layout
  constraint ("place within 2 mm of U1 pin 8"). Short, sentence case, 1.27 mm.
- **General notes** on the cover apply unless a local note says otherwise ("Resistors 0402 1 %,
  capacitors 0402 X7R 16 V, unless noted").

The `sch.style` check enforces what can be checked from the files: custom colours and filled shapes
(notes), four-way junctions, upside-down ground and supply symbols, text under 1 mm, missing title
block fields, and designator letters that do not match the part. The worked example is
`tools/tw/examples/demo_board.py`.

Sources: Zuken "4 rules for better PCB schematics"; HMC "How to draw circuits"; Sierra Circuits
"Schematic design rules"; Schemalyzer "30 rules for clear, professional circuits"; Flux "PCB schematic
best practices"; EMA "Rules to make schematics clear"; Altium schematic review checklist; KiCad KLC
S4.7 (active-low pins); ASME Y14.44-2008 and IEEE 315 (designators); IEC 60062 (RKM value code);
Raspberry Pi Ltd CM5IO rev 2 KiCad sources.
