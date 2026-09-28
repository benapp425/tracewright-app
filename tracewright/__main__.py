"""python -m tracewright [serve|new|import|list|doctor] -- see `tracewright --help`."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
