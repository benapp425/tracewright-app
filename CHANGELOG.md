# Changelog

All notable changes to Tracewright. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

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
