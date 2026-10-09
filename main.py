"""Compatibility launcher for the EMG Cycling graphical interface."""

from delsys.gui import TrignoWindow, main

__all__ = ["TrignoWindow", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
