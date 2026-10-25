"""Desktop application entry point, run with ``uv run main.py``."""
from __future__ import annotations

from emg_rehab.gui import EmgRehabilitationWindow, main

__all__ = ["EmgRehabilitationWindow", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
