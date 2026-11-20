# Periodic Backup & Sync 3.2.0

Optional normal AnkiWeb collection sync while Anki stays open, plus backup
protection that works even when automatic sync is off.

## Protection that works without sync

- **Startup integrity check** (on by default): about a minute after a profile
  opens, a read-only SQLite `integrity_check` runs in the background (a few
  seconds). Damage found means a persistent safety pause and one warning, so you
  learn about storage problems before every recent backup contains them.
- **Verified Backup Now (no sync)**: creates an Anki backup, reopens the actual
  archive in a temporary folder, compares its complete contents with the open
  collection and runs `integrity_check` on it. No network access.
- **Backup mirror** (optional): every verified backup is also copied to a folder
  you choose, ideally another drive or an iCloud/Google Drive folder. Copies are
  flushed, re-hashed and published atomically. The newest N copies are kept.
  Only files named `anki-verified-<profile>-*.colpkg` are ever deleted there. A
  failed copy shows a warning but does not pause sync. The collection is fine.

## Sync

Automatic sync is off by default. It needs both this add-on's setting and Anki's
own automatic-sync preference. Every request checks SQLite integrity and free
space. Automatic pre-sync backups are throttled to at most one per
`minimum_backup_interval_minutes` (default 60). Anki keeps every backup made on
the same day, so a backup on every 10-minute sync would fill the disk quickly.
`Backup + Sync Now` always creates a fresh backup.

Any failed check, backup, sync or cancellation pauses further attempts. A marker
is persisted before work begins, so an interrupted process also stays paused
after restart. Fix the cause, then use **Check Collection & Resume**. It verifies
the collection and a fresh backup and starts no sync. Saving settings never
clears a pause.

The add-on performs normal sync only. If AnkiWeb requires a full sync, it records
the server endpoint and message the way Anki does, then pauses. Choose upload or
download deliberately with Anki's own Sync button.

Menu: `Tools` → `Learning Toolkit` → `Backup & Sync`.
