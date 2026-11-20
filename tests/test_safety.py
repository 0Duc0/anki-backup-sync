"""Backup verification, mirror copies and preflight checks against a real Anki collection."""
import os
import zipfile
from pathlib import Path

import pytest
from anki.collection import Collection

import backup_safety
import mirror
import safety


@pytest.fixture
def col(tmp_path):
    (tmp_path / "profile").mkdir()
    c = Collection(str(tmp_path / "profile" / "collection.anki2"))
    model = c.models.by_name("Basic")
    for i in range(25):
        note = c.new_note(model)
        note["Front"], note["Back"] = f"front {i}", f"back {i}"
        c.add_note(note, c.decks.id("Default"))
    yield c
    c.close()


def test_verified_backup_matches_collection(col, tmp_path):
    result = backup_safety.create_verified_backup(col, str(tmp_path / "backups"))
    assert Path(result.path).is_file() and result.created
    assert result.fingerprint == backup_safety.collection_fingerprint(col.db.all)


def test_damaged_or_different_backup_is_rejected(col, tmp_path):
    good = backup_safety.create_verified_backup(col, str(tmp_path / "backups"))
    # a backup that does not match the collection
    note = col.get_note(col.find_notes("")[0])
    note["Back"] = "changed after the backup"
    col.update_note(note)
    with pytest.raises(RuntimeError, match="does not exactly match"):
        backup_safety.verify_backup_archive(
            Path(good.path), temporary_parent=tmp_path,
            expected_fingerprint=backup_safety.collection_fingerprint(col.db.all))
    # an archive without a collection
    empty = tmp_path / "empty.colpkg"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("media", "{}")
    with pytest.raises(RuntimeError, match="no collection database"):
        backup_safety.verify_backup_archive(empty, temporary_parent=tmp_path, expected_fingerprint="x")


def test_mirror_copies_hash_checked_and_prunes_only_own_files(tmp_path):
    src_dir, target = tmp_path / "backups", tmp_path / "mirror"
    src_dir.mkdir(); target.mkdir()
    (target / "holiday-photo.jpg").write_bytes(b"not ours")
    (target / "anki-verified-other-profile-x.colpkg").write_bytes(b"other profile")
    made = []
    for i in range(4):
        src = src_dir / f"backup-2026-11-2{i}.colpkg"
        src.write_bytes(os.urandom(2048))
        made.append(mirror.mirror_backup(src, target, profile_tag="User 1", keep=2))
    names = sorted(p.name for p in target.iterdir())
    assert names == ["anki-verified-User_1-backup-2026-11-22.colpkg", "anki-verified-User_1-backup-2026-11-23.colpkg",
                     "anki-verified-other-profile-x.colpkg", "holiday-photo.jpg"]
    assert made[-1].removed == ("anki-verified-User_1-backup-2026-11-21.colpkg",)
    with pytest.raises(mirror.MirrorError, match="must differ"):
        mirror.mirror_backup(src, src_dir, profile_tag="User 1", keep=2)
    with pytest.raises(mirror.MirrorError, match="not available"):
        mirror.mirror_backup(src, tmp_path / "missing-drive", profile_tag="User 1", keep=2)


def test_preflight(col, tmp_path):
    safety.preflight(col, str(tmp_path / "backups"), minimum_gib=0)
    with pytest.raises(safety.SafetyError, match="Not enough free space"):
        safety.check_disk_space(col, str(tmp_path), minimum_gib=10**6)
