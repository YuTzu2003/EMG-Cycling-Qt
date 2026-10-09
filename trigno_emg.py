"""Compatibility launcher for the Trigno command-line recorder."""

from delsys.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
