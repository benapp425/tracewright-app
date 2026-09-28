# {{name}}: bring-up plan

What only the built board can show, in the order to test it. Tracewright shows this plan as a checklist at
the bench (Docs, Bring-up checklist): each `- [ ]` step can be ticked, and a step that names the value to
expect (`= 3.30 V ± 2 %`, `< 50 mA`, `> 1 kΩ`, `about 2 Hz`) gets a box for the reading, judged as it is typed.

## 1. Before power
- [ ] Visual inspection: polarity marks, part orientation, no solder bridges.
- [ ] No shorts between each supply rail and GND (a multimeter's resistance range).

## 2. First power
- [ ] A current-limited bench supply; note the idle current.
- [ ] Each rail at its nominal voltage; nothing gets hot.

## 3. Interfaces
- [ ] Each interface, one at a time.
