"""Tracewright: an AI pair designer for KiCad printed circuit boards.

Each project is a folder with its KiCad files, the self-contained toolkit (tools/tw), the design
scripts the agent writes (design/), docs and generated outputs (build/). The app serves a local web
UI with the live board and schematic, the checks, and a Claude agent that designs alongside you --
in the files, and live in an open KiCad through its IPC API.
"""
import os as _os, sys as _sys

__version__ = "1.1.0"
REPO_URL = "https://github.com/benapp425/tracewright-app"    # where releases and update checks come from
APP_NAME = "Tracewright"

# The toolkit is importable as the top-level package `tw`, exactly as in a project folder
# (tools/tw), so project checks written as `from tw.checks import check` share the app's registry.
_TOOLKIT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "toolkit")
if _TOOLKIT not in _sys.path:
    _sys.path.insert(0, _TOOLKIT)

# HTTPS from Python (GitHub, Google sign-in, PCBWay, the update check) checks certificates against the
# system's trust store, as Safari and curl do: python.org's Python ships with no certificates until its
# "Install Certificates" script is run, and every request would fail verification.
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
except Exception:                                      # not installed: Python's own certificate store
    pass
