# Changelog

All notable changes to Tracewright. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

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
