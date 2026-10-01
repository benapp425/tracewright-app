---
name: release
description: Verify the finished design and produce the fab release - full checks, readiness report, JLC-fitted CPL, outputs and the release zip. Use when the board is routed and the user wants to order it.
---
# Verification and release

1. `run_checks` with everything, `refresh`. Every error fixed. Every warning either fixed or waived with the
   `waive` tool (action propose, the finding's key and the reason -- the reason is required -- plus a `title`:
   the decision in a few plain words, and the `source` when a data sheet or app note shows it). An error you
   cannot fix is only a proposal when you waive it: it keeps counting until the user approves it on the
   Sign-off page, so tell them in the chat why you propose it.
1b. Evidence for every requirement: `evidence` action list shows each requirement (the guided start's list, the
   bullets of docs/requirements.md, the limits) and what it has. For each one without evidence, record what shows
   it is met -- a check that covers it, a calculation with its numbers, a data sheet page, a simulation -- or, when
   only the built board can show it, kind hardware with the bring-up step (status open). The limits get theirs
   from the req.limits check by themselves; the temperature requirements get each linear regulator's junction
   temperature from power.thermal (declare the load currents in checks.currents so it can work them out).
   A simulation: `simulate` with the circuit as a SPICE netlist (the real values; a part's data sheet model or
   a simple equivalent you name), the probes, a pass criterion (`check`: {probe, at, min, max} or {probe,
   corner: {min, max}} for a -3 dB point) and the requirement with a label. A FAIL is a finding: fix the
   circuit, then run it again (`./tw sim docs/sim/<name>.cir` re-runs one in place).
2. `./tw selftest` passes (the checks can still catch their planted faults).
3. `docs/review.md`: verified from the files / needs the built board / open issues. `docs/bring-up.md`: the test
   order for the first board, as `## ` sections of `- [ ]` steps, each naming the value to expect where
   there is one (`TP3 3V3 = 3.30 V ± 2 %`, `idle current < 50 mA`, `VBUS to GND > 1 kΩ`): the app turns it
   into a checklist that judges the readings typed at the bench.
4. Outputs: `./tw outputs`. Then `./tw cpl --write`, `./tw outputs` again, and `./tw cpl` must report every
   placement agrees with JLC's footprints (polarity by pin name for LEDs and diodes).
5. Check the BOM: every placed part has an LCSC code with stock; not-assembled parts are listed in
   fab.not_assembled.
6. The release zip is `build/release/<name>-rev<rev>-<date>.zip`. Tell the user what it contains, the check
   verdict, and what only the built board can prove. Never call it ready while problems remain.
7. Ask the user to sign the design off (Checks > Sign-off): they approve or reject each proposed waiver there,
   and ordering waits for their sign-off. The release stage only counts as done once they have signed off.
