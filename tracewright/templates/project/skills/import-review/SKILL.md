---
name: import-review
description: Review a KiCad project that was imported or opened in place - understand it, run every check, and report what is verified, what is wrong and what needs hardware. Use as the first step on any existing design.
---
# Reviewing an existing design

1. `status` and `./tw status`: what files exist, board summary, which stage the design looks to be in.
2. Read the schematic (`render` each sheet; `Hierarchy` in Python for the part list) and summarise the design
   in a few lines: function, power tree, main parts, interfaces.
3. `run_checks` (everything). Group the findings: real defects, probable defects to confirm with the data
   sheet, style issues, and things the checks cannot know (write them down as questions).
4. For each real defect, point at it (`show`) and propose the smallest fix. Do not start fixing before the user
   agrees on the list, unless they asked you to fix everything.
5. Set the stage tracker to match reality (e.g. schematic done, routing active) with a note per stage.
6. Write `docs/review.md`: verified from the files / needs the built board / open issues.

Never overwrite the user's files wholesale: edit in place (`tw.sch.edit`, board ops), keep their formatting,
and take a `snapshot` before large changes.
