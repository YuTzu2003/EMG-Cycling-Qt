"""On-disk cache for expensive, reproducible per-test analysis results."""
from __future__ import annotations

import hashlib
import os
import pickle
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = ROOT / "data" / "analysis_cache"
ANGLE_CACHE_VERSION = 1


def _test_directory(test_id: str) -> Path:
    # Do not use an externally supplied identifier directly as a path.
    return CACHE_ROOT / hashlib.sha256(test_id.encode("utf-8")).hexdigest()


def _key(settings: object) -> str:
    values = asdict(settings) if hasattr(settings, "__dataclass_fields__") else repr(settings)
    return hashlib.sha256(repr((ANGLE_CACHE_VERSION, values)).encode("utf-8")).hexdigest()


def load_angle(test_id: str, settings: object):
    path = _test_directory(test_id) / f"angle-{_key(settings)}.pickle"
    try:
        with path.open("rb") as source:
            return pickle.load(source)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
        return None


def save_angle(test_id: str, settings: object, result: dict) -> None:
    directory = _test_directory(test_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"angle-{_key(settings)}.pickle"
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("wb") as destination:
            pickle.dump(result, destination, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def invalidate(test_id: str) -> None:
    directory = _test_directory(test_id)
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.is_file():
            entry.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass
