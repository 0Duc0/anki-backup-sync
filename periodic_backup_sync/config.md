# Periodic Backup & Sync

Use `Tools` → `Learning Toolkit` → `Backup & Sync` → `Settings…` instead of editing
these values by hand.

- `enabled`: automatic periodic sync (default off; also needs Anki's own automatic-sync preference)
- `sync_interval_minutes`: 5–240 (default 30)
- `skip_while_reviewing` / `sync_after_review`
- `backup_before_periodic_sync`: verified backup before automatic sync
- `minimum_backup_interval_minutes`: 10–1440 (default 60); throttles automatic pre-sync backups
- `mirror_folder`: optional second location for verified backups ("" = off)
- `mirror_keep`: newest copies kept in the mirror folder, 1–100 (default 10)
- `startup_integrity_check`: integrity check after the profile opens (default on)
- `minimum_free_space_gib`: at least 2, and at least 3× the collection size
- `show_notifications`

`safety_pauses`, `last_verified_backups` and `safety_policy_version` are internal
state. Do not edit them to bypass a failed check.
