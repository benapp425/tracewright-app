---
name: part-selection
description: Choose parts that can be bought and assembled (JLC / LCSC), and verify each symbol and footprint against the manufacturer's documents. Use when picking or changing any part.
---
# Part selection and verification

1. Search: `parts search "<MPN or description>"`. Prefer JLC Basic parts, then Extended with stock of several
   hundred boards' worth. Note the query date shown: stock claims carry it.
2. Confirm the exact variant with `parts code C<lcsc>`: package (the SOIC-8 body width, the pad pitch), voltage
   and temperature ratings, and the data sheet link.
3. Get the data sheet (WebFetch the manufacturer's PDF). From it, take the pin table and the recommended land
   pattern -- page numbers go in docs/parts.md.
4. Footprint: KiCad stock if its dimensions match the land pattern (pitch, pad size, pin 1, numbering
   direction); otherwise draw it in the project library. Add the data sheet's layout keep-outs as rule areas in
   the footprint.
5. Symbol: KiCad stock if its pin numbers and names match the table; otherwise `make_ic` from the table.
6. Write a project check (`design/checks/`) comparing the netlist pins to the data sheet table for each IC, with
   a planted-fault test.
7. Record in docs/parts.md: MPN, LCSC, stock (date), data sheet, symbol/footprint source, how verified.

If a document cannot be fetched, say so and mark the part unverified -- never fill in from memory.
