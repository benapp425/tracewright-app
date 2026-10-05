# Changelog

All notable changes to Tracewright. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-04

### Fixed
- **An expired Claude sign-in.** When Claude's sign-in on this Mac has expired, the chat now says so and how to sign in again (in Terminal, `claude`, then `/login`), with Open Terminal and Send again buttons. Before, Claude Code's error appeared as if Claude had said it.

## [1.0.0] - 2026-10-01

Edit the board and the schematic yourself, talk to Claude on the design itself, and see what Claude changed before you keep it.

### Added
- **Board editing.** Move, turn, flip and lock parts. Route tracks that find their way round what is in the way, change layer with a via, and delete copper. Draw and reshape pours and keep-outs. Every edit is one undo step (⌘Z), and reaches KiCad live when it has the board open.
- **Differential pairs and length tuning.** Route a pair as one, at its net class's gap. Tune a track to a length with meanders. Shove a track aside to make room.
- **Up to 10 layers.** Claude plans the stack-up: which layers carry signals and in which direction, which are planes and for which nets, on the fab's standard build. The router routes every signal layer, and the Order tab prices 6, 8 and 10 layers.
- **Flags as threads.** A flag is a request or a question, with drawings: a pen, an arrow, a box, text. Sketch where a track should go, or an area to keep clear. Claude answers in the thread: done (green), or why not (red). You can still say do it anyway.
- **Needs your OK.** Nothing interrupts a run. Afterwards, a short list shows what Claude went ahead with that you may want to see: a part swapped after the parts were agreed, a connector moved after the floorplan, a change to your own work, a looser rule. Keep it, or Undo and Claude puts it back. Changes to the agreed limits are asked, and kept as agreed until you approve. Settings choose what is listed.
- **Suggested layouts.** Suggest a layout on the floorplan, or Suggest placement in the board editor. Claude's proposal appears as ghosts with a note on each item. Take all of it, some, or none.
- **Cost before you run.** The message box estimates a request's cost and its share of your plan's limit from your past requests of the same kind. A run close to the limit finishes its step and pauses.
- **Second opinion.** Optional, in Settings: a separate reviewer reads each run and lists up to four concerns under it.
- **My parts.** Save a part you have checked from its card: its symbol, footprint, 3D model, pin table and your notes. Every project can use it again, and Claude looks there first.
- **Simulation.** Claude simulates circuits with KiCad's ngspice (a filter's corner, a divider's output, an RC delay, a supply's start-up) to show a requirement holds. The plot, the verdict and the netlist are under Docs › Simulations, and the result counts as the requirement's evidence. Edit the netlist and run it again.
- **Regulator heat on the sign-off page.** Each linear regulator's junction temperature, worked out at the top of the operating range, appears with the temperature requirements.
- **Schematic editing.** Change a part's value, footprint, part number, LCSC code and DNP from its card. Click a net label to rename the net; KiCad's netlist confirms nothing else changed before it is saved. ⌘Z undoes it.
- **The design in one read.** Claude reads every part and net at once in a compact form, which uses less of your plan.

### Changed
- **Sign-off.** Rebuilt as a review: the verdict and what is left, each with its button; every requirement with its evidence; waivers as cards with plain titles and their sources; a printable packet.
- **Floorplan.** Everything can be dragged, turned (R) and locked, keep-outs move too, and the page keeps its place while you work.
- **Schematic text.** Measured with KiCad's own font, glyph by glyph, so what the layout places clear is clear on the plot. Every redraw also reports any text that overlaps on KiCad's plot.
- **3D view.** Its model is cached outside the project folder, so a project in iCloud no longer uploads a new copy after every change.

### Fixed
- **Docs panel.** Sections no longer appear twice when the panel loads twice at once.
- **Sign-off page.** Scrolls on small windows.

## [0.4.0] - 2026-09-29

A simpler workspace, a floorplan to start from, and a sign-off before anything is ordered.

### Added
- **Floorplan.** At the start, set the board's size, mounting holes, connectors and main parts by dragging them. Claude lays the board out from it.
- **Sign-off.** Before ordering, review what the checks found, approve or reject each waiver, and sign the design off. A later change to the design reopens it.
- **Stage gates.** A stage counts as done only when its work is: the requirements written, the checks run on the design as it is, nothing left unrouted.
- **Undo a turn.** After each turn, a card shows what changed on the board, in the schematic and in the docs. Undo puts the files back as they were.
- **Attachments.** Drop, paste or pick files into a message. Pictures go to Claude; data sheets, libraries and 3D models go into the project.
- **Dictation.** Click the microphone, or hold it while you talk. The Mac app recognizes speech on the Mac when it can.
- **@-mentions.** Type @ to point Claude at a part, net, sheet or file.
- **Net names on the board.** Zoom in to read the net on each track, pad and via.
- **Data sheet library.** Each part's data sheet is saved in the project with its pin table, and the pinout check trusts it over the parts library.
- **Firmware starter.** A pin map and a bring-up sketch, written from the schematic.
- **Stock watch.** Checks your parts' stock against what an order needs, warns when one runs short, and finds in-stock stand-ins.
- **Design limits.** An Advanced section in the requirements: size, height, layers, currents, temperature, cost, quantity and more. The checks hold the design to them.
- **Net model.** Every net has a kind, a voltage, a current and its pair. The checks and the net classes follow it.
- **Plan usage.** The run monitor shows how much of your Claude plan's limit is used and when it resets. The chat shows it too when you are close.
- **Run report.** Time, turns and cost for each stage, on the sign-off page.

### Changed
- **Five places.** Overview, Design, Parts, Checks and Project replace the eleven tabs. The Ask Claude buttons are gone: ask in the chat.
- **Run monitor.** Mission Control is now a working view of a run: the plan, the stages, the board as it grows, the latest steps and the checks.
- **Checks.** Every check says what it examined, or why it does not apply. Nothing passes without looking.
- **Waivers.** Claude proposes a waiver with its reason. An error stops counting only once you approve it.
- **Schematics.** Connectors face the circuit they serve, dividers stand top to bottom, and supply pins on one side share one symbol. A crowded side is drawn again with room kept for the pins that need it.
- **Placement.** A new check makes sure every pad has room for its track to leave.
- **Silkscreen.** Reference designators that sit on pads, other text or the board edge are moved before release.
- **Home screen.** Opens at once with the last project list, and says when a project's files are only in iCloud.

### Fixed
- **Costs.** Each turn was shown with the whole conversation's running cost, so conversation totals and the run report came out several times too high. Turns now show their own cost, and older conversations are corrected when opened.
- **Parts left off schematics.** An LED or a crystal with no room beside its pins was left out of the schematic. It is now drawn beside the part.
- **Router.** A second run no longer stacks stitching vias. Rerouting clears stray tracks left on unconnected pads, redraws the neck-down areas when a part has moved, and names the pour islands it could not join.
- **Waivers after moving a project.** Some schematic findings were keyed by the file's full path, so moving the project folder dropped their waivers.
- **Docs panel.** No longer shows an error when a project has no firmware folder.

## [0.3.0] - 2026-09-28

Routing and schematics that look like a person made them, and more ways to check a design.

### Added
- **Guided start.** Describe your board in the chat while a live canvas fills in the requirements, block diagram, connectors and parts. Press Start when it looks right.
- **Schematic settings for each project.** How sheets connect (sheet pins or global labels), how supplies and decoupling are drawn, how nets and pairs are named, reference numbers, values, paper size and notes.
- **Flat or hierarchical.** Switch a schematic between sheet pins and global labels. Nothing is saved unless KiCad's netlist is unchanged.
- **Part inspector.** Click a part to see its pins, nets, footprint and stock. Open it for photos, parameters and findings.
- **Net types.** The Copper panel tags power rails with their voltage, and marks differential pairs, clocks and fast signals. Filter by type.
- **16 new checks.** Schematic integrity, text on sheets, naming conventions, power and signal layout, and routing quality.
- **Continues after the usage limit.** If an autonomous run hits your Claude usage limit, it waits for the reset and then continues.

### Changed
- **Router.** Differential pairs run side by side. Nets with extra vias or detours get a second pass, and nets that fail get another try. On a large board: every net routed, 9% fewer vias, 2% less track.
- **Schematics.** Drawn by rule: parts grouped by function, decoupling at the pins, gate resistors with their pull-downs, neighboring pins on one net wired together, notes kept clear of parts, and sheets packed onto the smallest paper that fits.
- **Toolkit updates.** Changes you or Claude made to a project's copy of the toolkit are saved before an update replaces them.

### Fixed
- **Stop.** Stop works in the first moments of a turn.
- **Answers.** Your answers in the chat no longer end with "(Recommended)".
- **Freerouting.** Stops when it stops making progress, instead of running for up to an hour, and leaves the board as it was.

## [0.2.0] - 2026-09-27

A cleaner, calmer Tracewright, with accounts, guided setup and new tools for testing and ordering boards.

### Added
- **Accounts.** Sign in with an email and password, or with Google. The first account owns the app.
- **Password reset.** Reset a forgotten password with Touch ID in the Mac app, or with `tracewright account reset`.
- **Guided setup.** Checks KiCad and Claude, then helps you choose a look, a workflow and a first project.
- **Workspace tour.** A short tour the first time you open a project.
- **Overview tab.** Status, next steps, checks, parts and cost for each project at a glance.
- **Compare versions.** See what moved and which copper changed since any checkpoint.
- **Save money.** Finds exact JLC Basic equivalents for Extended resistors and capacitors.
- **Bring-up checklist.** Test the built board step by step, with each reading checked against the plan.
- **Release notes.** This window, after every update.
- **Update notices.** A notice when a new version is available, with a download link.
- **Mac app download.** One app for Apple silicon and Intel that installs its engine on first launch.

### Changed
- **Cleaner workspace.** Pin the tabs you use; the rest are under More. Tools and focus mode are in the tab bar.
- **Simpler chat.** One menu in the header, and quick actions to start a conversation.
- **Home screen.** Projects first, with Ideas, Import and New project in the header.
- **Clearer text.** Shorter labels, hints and messages throughout the app.
- **Settings.** Your account comes first, with simpler options.
- **Accounts on a server.** The first account needs the server password, and new accounts are closed by default.
- **Bring-up plans.** New projects start with a checklist, and numbered plans are read as steps.

### Fixed
- **Downloads.** Files keep their names instead of arriving as `file.zip`.
- **Surface finish.** Boards with fine-pitch parts are ordered with ENIG.
- **HTTPS certificates.** Connections to GitHub, Google and PCBWay use the Mac's trust store, so they work with python.org's Python.

## [0.1.0] - 2026-09-27

The first release.

### Added
- **Projects.** Start from a description, import KiCad and other formats, or clone from GitHub. Every change is checkpointed.
- **Claude in KiCad.** Claude places, routes and edits with you, live in KiCad's PCB Editor.
- **Views.** Board, schematic, 3D, BOM, checks, design rules, docs, files and history.
- **36 design checks.** Schematic, BOM, placement, routing, fab limits, assembly, signal integrity and power.
- **Ordering.** JLCPCB and PCBWay, with parts lists for DigiKey, Mouser and LCSC.
- **Tools.** Mission Control, timelapse, calculators, project ideas and a Touch ID lock.
- **Server mode.** Run Tracewright on a server with Docker.
