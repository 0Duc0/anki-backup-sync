"""Copy verified backups to a second folder (another disk or a cloud-synced folder).

A backup that lives only on the collection's own disk is lost together with it.
Only files this module created (``MIRROR_PREFIX`` + ``.colpkg``) are ever
pruned; nothing else in the target folder is touched.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MIRROR_PREFIX = "anki-verified-"
_PARTIAL_SUFFIX = ".partial"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class MirrorError(RuntimeError):
    """The verified backup could not be copied safely."""


@dataclass(frozen=True)
class MirrorResult:
    path: str
    removed: tuple[str, ...]
    same_device: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prefix(profile_tag: str) -> str:
    tag = _SAFE_NAME.sub("_", profile_tag)[:40] or "profile"
    return f"{MIRROR_PREFIX}{tag}-"


def mirror_name(profile_tag: str, source: Path) -> str:
    return _prefix(profile_tag) + source.name


def _owned(folder: Path, profile_tag: str) -> list[Path]:
    prefix = _prefix(profile_tag)
    return sorted(
        (
            p
            for p in folder.iterdir()
            if p.is_file() and p.name.startswith(prefix) and p.name.endswith(".colpkg")
        ),
        key=lambda p: p.name,
    )


def mirror_backup(
    source: str | Path,
    folder: str | Path,
    *,
    profile_tag: str,
    keep: int,
    collection_path: str | Path | None = None,
) -> MirrorResult:
    """Copy, flush, re-hash and atomically publish one verified backup."""
    source = Path(source)
    target_folder = Path(folder).expanduser()
    if not str(folder).strip():
        raise MirrorError("No mirror folder is configured.")
    if not target_folder.is_dir():
        raise MirrorError(
            f"The mirror folder is not available: {target_folder}. "
            "Connect the drive or choose another folder."
        )
    try:
        if target_folder.resolve() == source.parent.resolve():
            raise MirrorError("The mirror folder must differ from Anki's backup folder.")
        size = source.stat().st_size
        free = shutil.disk_usage(target_folder).free
        if free < size * 2 + 256 * 1024**2:
            raise MirrorError(
                f"Not enough free space in the mirror folder ({free / 1024**3:.1f} GiB)."
            )
        final = target_folder / mirror_name(profile_tag, source)
        partial = final.with_name(final.name + _PARTIAL_SUFFIX)
        expected = _sha256(source)
        if final.exists() and _sha256(final) == expected:
            published = final
        else:
            try:
                with source.open("rb") as src, partial.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                if _sha256(partial) != expected:
                    raise MirrorError("The mirrored copy does not match the verified backup.")
                os.replace(partial, final)
            finally:
                if partial.exists():
                    partial.unlink()
            published = final
        removed: list[str] = []
        keep = max(1, int(keep))
        owned = _owned(target_folder, profile_tag)
        for old in owned[:-keep]:
            if old == published:
                continue
            old.unlink()
            removed.append(old.name)
        same_device = False
        if collection_path:
            same_device = Path(collection_path).stat().st_dev == target_folder.stat().st_dev
        return MirrorResult(str(published), tuple(removed), same_device)
    except MirrorError:
        raise
    except OSError as error:
        raise MirrorError(f"Could not copy the backup to the mirror folder: {error}") from error
