"""Desktop application entry point, run with ``uv run main.py``."""
from __future__ import annotations

import sys
from pathlib import Path

# The desktop application and web application share the root ``modules``
# directory without installing either project as an editable package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from emg_rehab.desktop_window import EmgRehabilitationWindow, main

__all__ = ["EmgRehabilitationWindow", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
