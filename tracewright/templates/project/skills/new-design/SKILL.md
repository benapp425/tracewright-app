---
name: new-design
description: Start a board from a written brief - the intake (every question that changes the design, asked up front), agreed requirements, then architecture and a plan before drawing anything. Use at the start of a new project or when the brief changes.
---
# Starting a new design

**1. Intake -- all questions now, so the rest of the run needs nothing from the user.**
Read `BRIEF.md` and list what it fixes and what it leaves open against this checklist:

| Topic | Decide |
|---|---|
| Function | what it does, where it lives (enclosure, outdoors, vehicle) |
| Interfaces | every connector and bus; the host or cable it meets; pinouts that are fixed |
| Power | source, voltage range, current budget, battery, protection that is really needed |
| Parts | must-use and must-avoid parts; the main IC / sensor family |
| Mechanical | size limit, mounting holes, connector positions and edges, height limits |
| Manufacturing | fab, layers, assembly by the fab or by hand, quantity, budget |
| Bring-up | programming / debug header, test points, status LEDs |

Ask everything the brief leaves open **in one go** with the question tool (up to four questions per
call, two or three calls at most, each with 2-4 options and your recommended option first, written so a
non-specialist can answer). Do not ask what you can decide from the data sheets, the lessons or good
practice -- decide it and list it as an assumption. Look parts up (`parts search`) before asking about
them, so the options are real (in stock, with prices).

In a guided start, draw what you work out on the canvas as you go (tool `canvas`): the requirements, the
block diagram, the connectors, the **floorplan** -- the board to scale with its size, mounting holes, each
connector on its edge and the main blocks where they will go (sizes from the real footprints; a block is the
area its parts will need; `side: bottom` for what goes underneath -- a board-to-board connector, a BGA's capacitors,
parts that do not fit on top) -- and the key parts. The floorplan is where the user sees the board's shape before
anything is drawn: ask about what decides it (which edge a connector faces, how big the board may be, where
the holes go). What the user drags there is theirs: keep it, and ask before changing it.

**2. Requirements.** Write `docs/requirements.md` with the answers and a section **Assumptions** (what
you decided for them). Ask once for confirmation. When the user agrees, set stage `brief` to done.
From then on the project runs in the mode the user chose (autonomous by default): **do not stop to ask**
-- take your recommended option, record it under "Assumptions to review" in `docs/decisions.md`, and
continue. Stop only for something that would make the board unsafe or impossible to build.

**3. Architecture** (`docs/architecture.md`): block diagram in words, the power tree with the current of
every branch, the interface list with constraints, and the key parts with the reason for each. Prefer parts
JLC stocks (`parts search`); keep the design simple -- no load switches or extra protection the requirements
do not need. Record choices in `docs/decisions.md`.

**4. Parts** (`docs/parts.md`): for each part the MPN, LCSC code, dated stock, the data sheet link, and where
the symbol and footprint come from (KiCad stock, the vendor, or drawn here from the land pattern on page N).

**5.** Then capture the schematic (skill `schematic-capture`), set up the board, place, route, verify, and
release, marking stages as you go (`stage`). At the end, summarise every assumption you made for review.
