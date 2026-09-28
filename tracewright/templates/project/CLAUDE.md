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
- `tools/tw/` -- this project's copy of the toolkit; run it with `./tw` (see `.claude/tracewright.md`).
- `build/` -- generated reports, plots and fab files (not in git; regenerate with `./tw check`, `./tw outputs`).
- `.claude/knowledge/` -- lessons learned on earlier boards. `.claude/skills/` -- how-tos for each stage.
