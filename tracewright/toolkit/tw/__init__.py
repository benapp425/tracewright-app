"""Tracewright toolkit: the self-contained design tools that live in every project (tools/tw).

Everything here works without the Tracewright app: run `./tw <command>` in the project folder.
Python 3.9+ (KiCad's bundled Python runs the pcbnew scripts); the standard library is enough for
everything except the grid router and a few geometry checks, which use numpy when it is present.
"""
__version__ = "0.1.0"
