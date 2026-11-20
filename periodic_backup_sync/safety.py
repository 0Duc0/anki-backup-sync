"""Read-only safety checks shared by sync preflight and recovery checks."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


GIB = 1024 ** 3
POLICY_VERSION = 1


class SafetyError(RuntimeError):
    """A failed preflight must prevent backup/sync from starting."""


def profile_key(collection_path: str) -> str:
    return hashlib.sha256(os.path.abspath(collection_path).encode()).hexdigest()


def checked_collection_path(col) -> Path:
    if col is None or getattr(col, "db", None) is None:
        raise SafetyError("The collection is temporarily closed.")
    path = getattr(col, "path", None)
    if not isinstance(path, str) or not path:
        raise SafetyError("This Anki version does not expose the collection path.")
    return Path(path)


def check_disk_space(col, backup_folder: str, minimum_gib: int = 2) -> None:
    path = checked_collection_path(col)
    try:
        # WAL is part of the live database; allow room for snapshots and sync.
        size = path.stat().st_size
        wal = Path(str(path) + "-wal")
        if wal.exists():
            size += wal.stat().st_size
        required = max(minimum_gib * GIB, size * 3)
        locations = [path.parent, Path(backup_folder)]
        for location in locations:
            while not location.exists() and location != location.parent:
                location = location.parent
            free = shutil.disk_usage(location).free
            if free < required:
                raise SafetyError(
                    f"Not enough free space at {location}: {free / GIB:.1f} GiB "
                    f"available; at least {required / GIB:.1f} GiB required."
                )
    except OSError as error:
        raise SafetyError(f"Could not check storage safely: {error}") from error


def check_collection(col) -> None:
    checked_collection_path(col)
    rows = col.db.all("PRAGMA quick_check(1)")
    if rows != [("ok",)] and rows != [["ok"]]:
        raise SafetyError(f"Collection integrity check failed: {rows!r}")


def preflight(col, backup_folder: str, minimum_gib: int = 2) -> None:
    check_disk_space(col, backup_folder, minimum_gib)
    check_collection(col)
