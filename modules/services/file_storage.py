"""Temporary upload-file handling for website workflows."""
from __future__ import annotations

import os
import logging
import re
import shutil
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)
_LEGACY_FILE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}_[^/\\]+\.csv$", re.I)
_WORK_DIRECTORY = re.compile(r"^\.work-[0-9a-f]{32}$")


def _lock(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def cleanup_temporary_files(*, include_legacy: bool = True) -> None:
    """Remove abandoned workspaces, skipping jobs held by any live worker."""
    _cleanup_directory(temporary_root(create=False), include_legacy=include_legacy)
    if include_legacy:
        # Retire the previous project-local temporary directory without
        # recreating it, and leave unrelated files untouched.
        legacy_root = ROOT / "data" / "tmp"
        _cleanup_directory(legacy_root, include_legacy=True)
        try:
            legacy_root.rmdir()
        except OSError:
            pass


def _cleanup_directory(root: Path, *, include_legacy: bool) -> None:
    if not root.is_dir() or root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        return
    root = root.resolve()
    for path in root.iterdir():
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            continue
        if path.resolve().parent != root:
            continue
        try:
            if include_legacy and path.is_file() and _LEGACY_FILE.fullmatch(path.name):
                # Flat files are from the previous implementation. Run startup
                # cleanup before requests are accepted by the new application.
                path.unlink(missing_ok=True)
            elif path.is_dir() and _WORK_DIRECTORY.fullmatch(path.name):
                lease = path / ".lease"
                if not lease.is_file():
                    continue
                try:
                    with lease.open("r+b") as stream:
                        _lock(stream)
                        # The OS releases this lock automatically on a crash.
                except (OSError, BlockingIOError):
                    continue
                shutil.rmtree(path)
        except OSError:
            logger.warning("暫存檔尚無法清除，將於下次清理重試：%s", path)


def temporary_root(*, create: bool = True) -> Path:
    # Use a workspace-specific system temp location, never data/tmp.
    workspace_key = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"emg-cycling-{workspace_key}"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def temporary_files():
    cleanup_temporary_files(include_legacy=False)
    root = temporary_root().resolve()
    directory = root / f".work-{uuid4().hex}"
    directory.mkdir()
    lease = (directory / ".lease").open("w+b")
    lease.write(b"1")
    lease.flush()
    _lock(lease)

    def new_path(name: str) -> Path:
        if Path(name).name != name or name in {"", ".", "..", ".lease"}:
            raise ValueError("Invalid temporary filename")
        return directory / name

    try:
        yield new_path
    finally:
        lease.close()
        try:
            shutil.rmtree(directory)
        except OSError:
            logger.warning("暫存資料夾尚無法清除，將於下次清理重試：%s", directory)
