# {{name}}

A Tracewright PCB project. KiCad project: `{{kicad_project}}`.

@.claude/tracewright.md

## Where things are

- `BRIEF.md` -- the request the project started from (projects begun in Tracewright); `docs/requirements.md`
  is the agreed version. An imported project keeps its own documents in `docs/`.
- `docs/` -- architecture, decisions (one entry per decision, with the reason), parts (with sources and
  dated stock queries), review, bring-up plan.
- `design/` -- scripts that generate or change the design (schematic generators, placement tables);
  `design/checks/` -- project-specific checks, each with a planted-fault test.
- `tools/tw/` -- this project's copy of the toolkit; run it with `./tw` (see `.claude/tracewright.md`). The app
  replaces it when it updates: work around a shortfall in your own script under `design/` and record a lesson,
  rather than editing it here (edits here are set aside, not kept, at the next update).
- `build/` -- generated reports, plots and fab files (not in git; regenerate with `./tw check`, `./tw outputs`).
- `.claude/knowledge/` -- lessons learned on earlier boards. `.claude/skills/` -- how-tos for each stage.
- Stages have gates: the `stage` tool only marks a stage done (or moves on past it) when its gate holds -- the
  requirements written, the checks run on the design as it is now with no errors, nothing unrouted, the
  user's sign-off for the release -- and says what is missing otherwise. Waive findings with the `waive`
  tool (with a plain title), never by editing tracewright.json; record what shows each requirement is met with
  the `evidence` tool as you verify.
