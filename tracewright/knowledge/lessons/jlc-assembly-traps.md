---
title: JLC assembly (CPL / BOM) traps
tags: [jlc, jlcpcb, lcsc, cpl, assembly, rotation, polarity]
source: a CM5 carrier board (2026-09-24/25)
---
JLC places each part by its **own** library footprint for the LCSC code (EasyEDA's), turned by the CPL
rotation about that footprint's origin -- not KiCad's. Fit JLC's pads onto the board's pads (rotation +
shift, no mirror) and store the correction per LCSC code: `./tw cpl --write`, regenerate outputs, `./tw cpl`.

- Data: `https://easyeda.com/api/products/<C-code>/components?version=6.4.19.5`; PAD records in
  `packageDetail.dataStr.shape` (10 mil units about `head.x/y`, y down); symbol pins in `dataStr.shape` `P~`.
  The CDN returns 403 without a full browser User-Agent and refuses bursts: back off and retry. After a
  few dozen requests in a row it can stop answering (non-JSON replies) for minutes; the `cpl.jlc` check
  therefore shares a 90 s lookup budget and reports the parts it could not fetch as "not verified" (an
  error, re-run later: answers are cached, so each run gets further). Never read that as the board.
- **Check the CPL that ships.** An imported design often carries its own pick-and-place (a plugin's
  `production/positions.csv`, a hand-corrected `assembly/JLCPCB_CPL.csv`), and two such files can disagree:
  the check fits every one it finds; set `fab.cpl` in tracewright.json to the file you upload.
- 28 of 147 placements on that board needed a correction: SOT-23/-5/TSOT 180; some SOT-23-5/6 and UQFN 90;
  LGA-28 180; the CM5 socket 90; a Molex 5267 header 180 + 2.5 mm (every servo plug would have been
  reversed); XT30 180 + 5.6 mm (polarity swapped); origin-only shifts on USB-C 1.3 mm, TO-252 1.8 mm.
- **Pad-number traps -- match by pin name, not number**: JLC's LED symbols number the *anode* 1, KiCad the
  cathode. Some electrolytics have unnamed pins (the silk '+' is on pad 1). Some microSD sockets number
  card-detect differently. CM5 J2 pads 101-200 vs JLC's 1-100.
- **Marked non-polarised parts**: a shielded inductor's stripe marks the start lead ("connect high dv/dt
  here"); a checker that accepts any half-turn for 2-pin parts never flags it.
- JLC's assembly stock (search API `stockCount`) and LCSC's warehouse (`stockNumber`) are different pools;
  record both with the query date.
