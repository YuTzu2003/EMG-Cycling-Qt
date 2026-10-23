"""Temporary upload-file handling for website workflows."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def temporary_root() -> Path:
    path = Path(os.environ.get("TEMP_DIR", ROOT / "data" / "tmp"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def temporary_files():
    created: list[Path] = []

    def new_path(name: str) -> Path:
        path = temporary_root() / f"{uuid4()}_{name}"
        created.append(path)
        return path

    try:
        yield new_path
    finally:
        for path in created:
            path.unlink(missing_ok=True)
