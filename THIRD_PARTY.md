# Third-party software

Tracewright installs these Python packages (their own licenses apply):

| Package | License | Used for |
|---|---|---|
| claude-agent-sdk | MIT | the Claude agent (bundles the Claude Code CLI under Anthropic's terms) |
| kicad-python (kipy) | MIT | the live link to a running KiCad (IPC API) |
| aiohttp | Apache-2.0 | the local web server |
| numpy | BSD-3-Clause | the grid router |
| Pillow | MIT-CMU (HPND) | images and renders |
| pypdf | BSD-3-Clause | merging PDF pages |
| truststore | MIT | HTTPS certificates from the system's trust store |

The interface includes (in `tracewright/web/vendor/` and `tracewright/web/js/icons.js`):

| Library | License | Used for |
|---|---|---|
| three.js 0.186 (with OrbitControls, GLTFLoader, RoomEnvironment) | MIT | the 3D view |
| Lucide icons 1.48 (the icons the interface uses) | ISC | icons |

It runs, but does not include or link:

| Program | License | Used for |
|---|---|---|
| KiCad (kicad-cli, KiCad's Python / pcbnew) | GPL-3.0-or-later | everything KiCad: ERC, DRC, plots, board edits, fab outputs |
| Freerouting (optional) | GPL-3.0 | whole-board autorouting, run as a separate program |
| git | GPL-2.0 | project history |

KiCad's stock symbol, footprint and 3D libraries are used from the user's KiCad installation
(CC-BY-SA 4.0 with the KiCad libraries exception). Part data comes from JLCPCB, LCSC and EasyEDA at
the user's request.
