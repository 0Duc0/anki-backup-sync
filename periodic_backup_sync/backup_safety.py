"""Fail-closed collection checks and independently opened backup verification.

The archive is restored only into a fresh temporary directory. It is never
imported into the user's collection. Keep this file self-contained so each
standalone add-on can ship the same tested safety code.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")
_lock = threading.Lock()
MIN_FREE_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class VerifiedBackup:
    path: str
    created: bool
    same_filesystem: bool
    fingerprint: str


def _live_db(col):
    db = getattr(col, "db", None)
    if db is None:
        raise RuntimeError(
            "The Anki collection is closed. Reopen the profile before retrying."
        )
    return db


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def assert_healthy_collection(col, backup_folder: str | None = None) -> None:
    db = _live_db(col)
    path = Path(col.path)
    if not path.is_file() or path.stat().st_size < 100:
        raise RuntimeError("The collection file is missing or unexpectedly empty.")
    rows = db.all("PRAGMA quick_check(1)")
    if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
        raise RuntimeError(
            "The collection failed SQLite verification. Stop and restore a verified backup."
        )
    required = max(MIN_FREE_BYTES, path.stat().st_size * 3)
    destinations = [path.parent]
    if backup_folder:
        destinations.append(_existing_parent(Path(backup_folder)))
    for folder in destinations:
        free = shutil.disk_usage(folder).free
        if free < required:
            raise RuntimeError(
                f"Not enough free space for a checked update: {free / 1024**3:.1f} GiB "
                f"available; at least {required / 1024**3:.1f} GiB required."
            )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def collection_fingerprint(query: Callable[..., list]) -> str:
    """Hash all application tables in bounded chunks, including blobs exactly.

    SQLite quote() normalizes BLOBs consistently between Anki's DBProxy (which
    otherwise represents bytes as lists) and Python's read-only sqlite3 reader.
    BINARY ordering avoids dependence on an external Unicode collation.
    """
    digest = hashlib.sha256()
    tables = query(
        "SELECT name,sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name COLLATE BINARY"
    )
    for name, schema in tables:
        digest.update(json.dumps([name, schema], ensure_ascii=False).encode())
        fields = query(f"PRAGMA table_info({_quoted(name)})")
        # Anki's package restore deliberately refreshes these two timestamps
        # to require a full sync. All actual data/schema tables are still
        # compared exactly, including their per-row modification metadata.
        names = [
            str(field[1])
            for field in fields
            if not (name == "col" and field[1] in {"mod", "scm"})
        ]
        keys = [
            str(field[1])
            for field in sorted(fields, key=lambda row: row[5])
            if field[5]
        ]
        if not keys:
            keys = ["rowid"]
        key_sql = [f"{_quoted(key)} COLLATE BINARY" for key in keys]
        ordered = ",".join(key_sql)
        values = ",".join(f"quote({_quoted(column)})" for column in names)
        last: tuple | None = None
        while True:
            args: tuple[Any, ...] = ()
            where = ""
            if last is not None:
                if len(keys) == 1:
                    where = f"WHERE {key_sql[0]}>?"
                else:
                    where = f"WHERE ({ordered})>({','.join('?' for _ in keys)})"
                args = last
            rows = query(
                f"SELECT {','.join(_quoted(key) for key in keys)},{values} "
                f"FROM {_quoted(name)} {where} ORDER BY {ordered} LIMIT 1000",
                *args,
            )
            if not rows:
                break
            for row in rows:
                digest.update(
                    json.dumps(list(row[len(keys) :]), ensure_ascii=False).encode()
                )
                digest.update(b"\n")
            last = tuple(rows[-1][: len(keys)])
    return digest.hexdigest()


def verify_backup_archive(
    path: Path, *, temporary_parent: Path, expected_fingerprint: str
) -> None:
    """Decode the actual archive with Anki, then check its complete DB contents."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if not (
                {"collection.anki21b", "collection.anki21", "collection.anki2"} & names
            ):
                raise RuntimeError("The backup has no collection database.")
            bad_member = archive.testzip()
            if bad_member:
                raise RuntimeError(f"The backup archive is damaged: {bad_member}")
        # Anki has no public Collection method for decoding modern compressed
        # .colpkg files. Use an independent backend, with only temporary paths.
        from anki._backend import RustBackend
        from anki.collection import Collection

        backend = RustBackend()
        restore = getattr(backend, "import_collection_package", None)
        if not callable(restore):
            raise RuntimeError(
                "This Anki version cannot verify compressed backups safely."
            )
        with tempfile.TemporaryDirectory(
            prefix="verify-deck-backup-", dir=temporary_parent
        ) as folder:
            destination = Path(folder) / "collection.anki2"
            restore(
                col_path=str(destination),
                backup_path=str(path),
                media_folder=str(Path(folder) / "collection.media"),
                media_db=str(Path(folder) / "collection.media.db2"),
            )
            # Compare before opening with Collection, since opening an older
            # schema can upgrade it. This connection is strictly read-only.
            with closing(
                sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.create_collation(
                    "unicase",
                    lambda a, b: (a.casefold() > b.casefold())
                    - (a.casefold() < b.casefold()),
                )
                fingerprint = collection_fingerprint(
                    lambda sql, *args: db.execute(sql, args).fetchall()
                )
            if fingerprint != expected_fingerprint:
                raise RuntimeError(
                    "The backup does not exactly match the current collection."
                )
            # Let Anki supply its real SQLite collations for the full check.
            checked = Collection(str(destination))
            try:
                result = checked.db.all("PRAGMA integrity_check")
                if len(result) != 1 or str(result[0][0]).lower() != "ok":
                    raise RuntimeError(
                        "The backup failed SQLite integrity verification."
                    )
            finally:
                checked.close()
    except Exception as error:
        raise RuntimeError(
            f"Backup verification failed.. {error}"
        ) from error


def create_verified_backup(col, backup_folder: str) -> VerifiedBackup:
    assert_healthy_collection(col, backup_folder)
    folder = Path(backup_folder)
    folder.mkdir(parents=True, exist_ok=True)
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_size) for p in folder.glob("*.colpkg")
    }
    created = bool(
        col.create_backup(
            backup_folder=str(folder), force=True, wait_for_completion=True
        )
    )
    after = sorted(
        folder.glob("*.colpkg"), key=lambda p: p.stat().st_mtime_ns, reverse=True
    )
    changed = [
        p for p in after if before.get(p) != (p.stat().st_mtime_ns, p.stat().st_size)
    ]
    if created and not changed:
        raise RuntimeError(
            "Anki reported a backup, but no fresh backup file could be verified."
        )
    if not after:
        raise RuntimeError("No collection backup was produced..")
    # Never silently fall back to an older file after a malformed new backup.
    latest = changed[0] if created else after[0]
    fingerprint = collection_fingerprint(_live_db(col).all)
    verify_backup_archive(
        latest, temporary_parent=folder, expected_fingerprint=fingerprint
    )
    if collection_fingerprint(_live_db(col).all) != fingerprint:
        raise RuntimeError(
            "The collection changed while the backup was being verified. Try again."
        )
    return VerifiedBackup(
        str(latest),
        created,
        Path(col.path).stat().st_dev == folder.stat().st_dev,
        fingerprint,
    )


def atomic_update(col, operation: Callable[[], T]) -> T:
    """Run all Anki/SQL writes in the supported DBProxy transaction.

    Reopening after commit/rollback clears cached models and the backend undo
    stack; rolled-back backend operations otherwise leave stale undo entries.
    A bulk migration is recoverable with its verified backup, not partial Undo.
    """
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Another deck update is already running.")
    try:
        db = _live_db(col)
        transact = getattr(db, "transact", None)
        if not callable(transact):
            raise RuntimeError(
                "This Anki version does not provide safe update transactions."
            )
        before = collection_fingerprint(db.all)
        result: list[T] = []
        try:
            transact(lambda: result.append(operation()))
        except BaseException as error:
            try:
                col.close()
                col.reopen()
                if collection_fingerprint(_live_db(col).all) != before:
                    raise RuntimeError(
                        "The rolled-back collection does not match its pre-update state."
                    )
            except Exception as reopen_error:
                raise RuntimeError(
                    "The update stopped and its rollback could not be verified. "
                    "Keep sync disabled and use the verified pre-update backup. "
                    f"Original error: {error}; reopening: {reopen_error}"
                ) from error
            raise RuntimeError(
                f"The update was rolled back; no partial migration was kept. {error}"
            ) from error
        try:
            col.close()
            col.reopen()
        except Exception as error:
            raise RuntimeError(
                "The update committed, but Anki could not reopen the collection. "
                "Keep sync disabled, restart Anki, and retain the verified pre-update backup. "
                f"{error}"
            ) from error
        return result[0]
    finally:
        _lock.release()
