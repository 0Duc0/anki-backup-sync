"""Configurable periodic collection sync with optional safety backups."""

from __future__ import annotations

import time
import threading
from datetime import datetime

from anki.errors import Interrupted

try:  # Present in all supported Anki versions; optional so tests can stub it.
    from anki.errors import SyncError, SyncErrorKind
except ImportError:  # pragma: no cover
    SyncError = SyncErrorKind = None
from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import (
    QAction,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFont,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPalette,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimer,
    QVBoxLayout,
    QWidget,
    Qt,
    qconnect,
)
from aqt.utils import showText, showWarning, tooltip

from .safety import POLICY_VERSION, SafetyError, preflight, profile_key


_timer: QTimer | None = None
_next_due_at = 0.0
_last_sync_at: datetime | None = None
_sync_requested = False
_external_syncing = False
_generation = 0
_profile_open = False
_session_pauses: dict[str, str] = {}
_poll_timer: QTimer | None = None
_startup_check_done: set[str] = set()
_last_mirror_status = "Not configured"

STARTUP_CHECK_DELAY_MS = 60 * 1000

DEFAULTS = {
    "enabled": False,
    "sync_interval_minutes": 30,
    "skip_while_reviewing": True,
    "sync_after_review": False,
    "backup_before_periodic_sync": True,
    "minimum_backup_interval_minutes": 60,
    "mirror_folder": "",
    "mirror_keep": 10,
    "startup_integrity_check": True,
    "show_notifications": False,
    "minimum_free_space_gib": 2,
    "safety_policy_version": POLICY_VERSION,
    "safety_pauses": {},
    "last_verified_backups": {},
}

# Settings the dialog owns. Internal state (pauses, backup times) is never
# written from a dialog snapshot, so a pause set while the dialog was open
# cannot be cleared by pressing Save.
USER_SETTING_KEYS = (
    "enabled", "sync_interval_minutes", "skip_while_reviewing", "sync_after_review",
    "backup_before_periodic_sync", "minimum_backup_interval_minutes", "mirror_folder",
    "mirror_keep", "startup_integrity_check", "show_notifications",
    "minimum_free_space_gib",
)

TOOLKIT_MENU_OBJECT = "codex_learning_toolkit_menu"
BACKUP_SYNC_MENU_OBJECT = "codex_learning_toolkit_backup_sync_menu"


def _rgba(color, alpha: int) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


def _dialog_style(widget: QWidget) -> str:
    palette = widget.palette()
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.WindowText)
    base = palette.color(QPalette.ColorRole.Base)
    muted = palette.color(QPalette.ColorRole.PlaceholderText)
    border = palette.color(QPalette.ColorRole.Mid)
    accent = palette.color(QPalette.ColorRole.Highlight)
    button = palette.color(QPalette.ColorRole.Button)
    button_text = palette.color(QPalette.ColorRole.ButtonText)
    return f"""
        QDialog#periodicSyncSettings, QDialog#periodicSyncStatus {{
            background: {_rgba(window, 255)};
            color: {_rgba(text, 255)};
        }}
        QLabel#dialogSubtitle, QLabel#mutedLabel {{ color: {_rgba(muted, 255)}; }}
        QLabel#infoBox {{
            background: {_rgba(accent, 20)};
            border-left: 3px solid {_rgba(accent, 190)};
            border-radius: 5px;
            padding: 8px 10px;
        }}
        QLabel#statusBadge {{
            background: {_rgba(accent, 30)};
            border: 1px solid {_rgba(accent, 100)};
            border-radius: 9px;
            font-weight: 700;
            padding: 5px 10px;
        }}
        QFrame#metricCard {{
            background: {_rgba(base, 220)};
            border: 1px solid {_rgba(border, 90)};
            border-radius: 9px;
        }}
        QGroupBox {{
            background: {_rgba(base, 185)};
            border: 1px solid {_rgba(border, 90)};
            border-radius: 9px;
            margin-top: 12px;
            padding: 13px 10px 9px 10px;
            font-weight: 650;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
        }}
        QSpinBox, QPushButton {{
            background: {_rgba(button, 245)};
            color: {_rgba(button_text, 255)};
            border: 1px solid {_rgba(border, 110)};
            border-radius: 7px;
            padding: 5px 9px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background: {_rgba(accent, 28)};
            border-color: {_rgba(accent, 125)};
        }}
    """


def _heading(text: str, *, delta: float = 5.0) -> QLabel:
    label = QLabel(text)
    font = label.font()
    size = font.pointSizeF() if font.pointSizeF() > 0 else 13.0
    font.setPointSizeF(size + delta)
    font.setWeight(QFont.Weight.Bold)
    label.setFont(font)
    return label


def _ensure_menu(parent: QMenu, object_name: str, title: str) -> QMenu:
    """Return a shared submenu regardless of add-on load order."""
    for parent_action in parent.actions():
        menu = parent_action.menu()
        if menu is not None and menu.objectName() == object_name:
            return menu
    menu = QMenu(title, parent)
    menu.setObjectName(object_name)
    parent.addMenu(menu)
    return menu


def _backup_sync_menu() -> QMenu:
    toolkit = _ensure_menu(
        mw.form.menuTools, TOOLKIT_MENU_OBJECT, "Learning Toolkit"
    )
    return _ensure_menu(
        toolkit, BACKUP_SYNC_MENU_OBJECT, "Backup && Sync"
    )


def _config() -> dict:
    config = dict(DEFAULTS)
    try:
        raw = mw.addonManager.getConfig(__name__)
    except Exception:
        raw = {"enabled": False}
    if isinstance(raw, dict):
        config.update(raw)
    for key in ("enabled", "skip_while_reviewing", "sync_after_review",
                "backup_before_periodic_sync", "show_notifications"):
        config[key] = config[key] is True
    # Unknown/malformed means "check": the integrity check is a safety feature.
    config["startup_integrity_check"] = config.get("startup_integrity_check") is not False
    for key, default, lower, upper in (("sync_interval_minutes", 30, 5, 240),
                                        ("minimum_free_space_gib", 2, 2, 100),
                                        ("minimum_backup_interval_minutes", 60, 10, 1440),
                                        ("mirror_keep", 10, 1, 100)):
        try:
            config[key] = min(upper, max(lower, int(config[key])))
        except (TypeError, ValueError, OverflowError):
            config[key] = default
    if not isinstance(config.get("mirror_folder"), str):
        config["mirror_folder"] = ""
    config["mirror_folder"] = config["mirror_folder"].strip()
    for key in ("safety_pauses", "last_verified_backups"):
        if not isinstance(config.get(key), dict):
            config[key] = {}
        else:
            config[key] = dict(config[key])
    # Upgrades cannot silently reactivate an old automatic-sync configuration.
    if config.get("safety_policy_version") != POLICY_VERSION:
        config["enabled"] = False
        config["sync_after_review"] = False
    return config


def _save_config(config: dict) -> None:
    mw.addonManager.writeConfig(__name__, config)


def _interval_minutes() -> int:
    return _config()["sync_interval_minutes"]


def _schedule_next() -> None:
    global _next_due_at
    _next_due_at = time.monotonic() + _interval_minutes() * 60


def _live_collection():
    """Return a usable collection while profile/full-sync transitions settle."""
    col = getattr(mw, "col", None)
    if not _profile_open or col is None or getattr(col, "db", None) is None:
        return None
    return col


def _current_key() -> str | None:
    col = _live_collection()
    path = getattr(col, "path", None)
    return profile_key(path) if isinstance(path, str) and path else None


def _pause_reason() -> str | None:
    key = _current_key()
    if key is None:
        return None
    return _session_pauses.get(key) or _config()["safety_pauses"].get(key)


def _set_pause(key: str, reason: str) -> None:
    """Persist before starting work; a process crash also leaves sync paused."""
    _session_pauses[key] = reason
    config = _config()
    config["safety_pauses"][key] = reason
    _save_config(config)


def _clear_pause(key: str) -> None:
    config = _config()
    config["safety_pauses"].pop(key, None)
    _save_config(config)
    _session_pauses.pop(key, None)


def _record_verified_backup(key: str) -> None:
    config = _config()
    config["last_verified_backups"][key] = time.time()
    _save_config(config)


def _backup_due(key: str, config: dict) -> bool:
    """Throttle automatic pre-sync backups.

    Anki keeps every backup made on the same day, and each verified backup also
    writes a full temporary copy. Backing up on every 10-minute sync would churn
    gigabytes per study session on the very disk that holds the collection.
    """
    if not config["backup_before_periodic_sync"]:
        return False
    try:
        last = float(config["last_verified_backups"].get(key) or 0)
    except (TypeError, ValueError):
        return True
    age = time.time() - last
    # A clock moved backwards also means "make a fresh backup".
    return age < 0 or age >= config["minimum_backup_interval_minutes"] * 60


def _profile_tag() -> str:
    try:
        name = getattr(mw.pm, "name", None)
    except Exception:
        name = None
    return name if isinstance(name, str) and name else "profile"


def _mirror_verified_backup(backup, config: dict, profile_tag: str, collection_path: str) -> str:
    """Runs in the background op. Never raises: the collection is fine either way."""
    folder = config["mirror_folder"]
    if not folder:
        return "Not configured"
    backup_path = getattr(backup, "path", None)
    if not backup_path:
        return "Skipped: no verified backup path was returned"
    from .mirror import MirrorError, mirror_backup

    try:
        result = mirror_backup(
            backup_path, folder, profile_tag=profile_tag, keep=config["mirror_keep"],
            collection_path=collection_path,
        )
    except MirrorError as error:
        return f"FAILED: {error}"
    except Exception as error:  # pragma: no cover - defensive
        return f"FAILED: {error}"
    note = (" (same disk as the collection: this protects you only if the folder is cloud-synced)"
            if result.same_device else "")
    return f"OK {datetime.now():%H:%M} → {result.path}{note}"


def _mirror_failed(status: str) -> bool:
    return status.startswith("FAILED")


def _describe_error(error: Exception) -> str:
    if (
        SyncError is not None
        and isinstance(error, SyncError)
        and getattr(error, "kind", None) is getattr(SyncErrorKind, "AUTH", object())
    ):
        return ("AnkiWeb rejected the stored login. Use Anki's own Sync button to sign in "
                f"again. ({error})")
    return str(error)


def _run_query(operation: QueryOp, label: str | None = None) -> None:
    """Require progress support so a future API change cannot hide sync work."""
    progress = getattr(operation, "with_progress", None)
    if label and not callable(progress):
        raise SafetyError("This Anki version cannot show cancellable progress safely.")
    if label:
        operation = progress(label)
    operation.run_in_background()


def _blocked_reason(
    *, require_enabled: bool = True, respect_review: bool = True,
    ignore_pause: bool = False, require_sync: bool = True,
) -> str | None:
    config = _config()
    if require_enabled and not config["enabled"]:
        return "Automatic periodic sync is disabled in Settings."
    if _live_collection() is None:
        return "No Anki collection is currently open."
    if not ignore_pause and (reason := _pause_reason()):
        return f"Safety pause: {reason} Use Check Collection & Resume after resolving it."
    try:
        signed_in = bool(mw.pm.sync_auth())
    except Exception:
        signed_in = False
    if require_sync and not signed_in:
        return "This profile is not signed in to AnkiWeb."
    if _sync_requested or _external_syncing:
        return "A collection sync or safety check is already running."
    if getattr(mw, "_background_op_count", 0):
        return "Anki is currently finishing another collection operation."
    media_syncer = getattr(mw, "media_syncer", None)
    try:
        media_is_syncing = bool(media_syncer and media_syncer.is_syncing())
    except Exception:
        media_is_syncing = True
    if media_is_syncing:
        return "Media synchronization is currently running."
    if respect_review and mw.state == "review" and config["skip_while_reviewing"]:
        return "Automatic sync is postponed until you leave the review screen."
    try:
        modal = mw.app.activeModalWidget()
    except Exception:
        return "Anki's dialog state could not be checked safely."
    if modal is not None:
        return "Close the open dialog before starting synchronization."
    if getattr(mw, "safeMode", False) or getattr(mw, "restoring_backup", False):
        return "Sync is disabled while Anki is in safe mode or restoring a backup."
    if require_enabled and require_sync:
        can_auto_sync = getattr(mw, "can_auto_sync", None)
        if not callable(can_auto_sync):
            return "Automatic sync is unavailable in this Anki version; use Anki's Sync button."
        try:
            ready = bool(can_auto_sync())
        except Exception:
            ready = False
        if not ready:
            return "Anki's automatic synchronization is disabled or not ready."
    col = _live_collection()
    if require_sync and not callable(getattr(col, "sync_collection", None)):
        return "This Anki version does not provide the supported collection sync API."
    if require_sync and any(not callable(getattr(col, name, None))
                            for name in ("latest_progress", "abort_sync")):
        return "This Anki version cannot safely monitor/cancel sync; use Anki's Sync button."
    return None


def _can_run_unattended(
    *, require_enabled: bool = True, respect_review: bool = True
) -> bool:
    return _blocked_reason(
        require_enabled=require_enabled, respect_review=respect_review
    ) is None


def _request_sync(*, automatic: bool, force_backup: bool = False) -> None:
    """Run normal sync only; full-sync choices always remain an explicit user action."""
    global _sync_requested, _poll_timer
    if not _can_run_unattended(
        require_enabled=automatic, respect_review=automatic
    ):
        return
    config = _config()
    expected_col = _live_collection()
    generation = _generation
    key = _current_key()
    if key is None:
        return
    try:
        backup_folder = mw.pm.backupFolder()
        auth = mw.pm.sync_auth()
        sync_media = mw.pm.media_syncing_enabled()
        profile_tag = _profile_tag()
        collection_path = str(expected_col.path)
        make_backup = force_backup or _backup_due(key, config)
        _set_pause(key, "A previous backup/sync did not finish. Check the collection before resuming.")
    except Exception as error:
        _session_pauses[key] = f"Could not persist safety state: {error}"
        if not automatic:
            showWarning(_session_pauses[key], parent=mw)
        return
    _sync_requested = True
    cancel_requested = threading.Event()
    poll_errors: list[Exception] = []
    try:
        poll_timer = QTimer(mw)
    except Exception as error:
        _sync_requested = False
        reason = f"Could not start safe sync monitoring: {error}"
        try:
            _set_pause(key, reason)
        except Exception:
            _session_pauses[key] = reason
        if not automatic:
            showWarning(reason, parent=mw)
        return
    _poll_timer = poll_timer
    poll_stopped = False
    sync_hook_started = False
    ui_finalized = False

    def stop_polling() -> None:
        global _poll_timer
        nonlocal poll_stopped
        if poll_stopped:
            return
        poll_stopped = True
        try:
            poll_timer.stop()
            poll_timer.deleteLater()
        except RuntimeError:
            # Qt may already have disposed the parent during profile shutdown.
            pass
        if _poll_timer is poll_timer:
            _poll_timer = None

    def finalize_ui() -> list[Exception]:
        """Match Anki's concluded-sync hook contract, even after partial failure.

        A failed network sync can have committed received changes already. UI,
        model and scheduler caches must reflect those changes. This is not proof
        of success and never clears the persisted failure marker.
        """
        nonlocal ui_finalized
        if ui_finalized:
            return []
        ui_finalized = True
        if generation != _generation or _live_collection() is not expected_col:
            return []
        callbacks = [getattr(expected_col, "_load_scheduler", None),
                     getattr(getattr(expected_col, "models", None), "_clear_cache", None)]
        if sync_hook_started:
            callbacks.append(gui_hooks.sync_did_finish)
        callbacks.extend((lambda: mw.reset(), lambda: mw.toolbar.redraw(),
                          lambda: mw.flags.require_refresh()))
        errors: list[Exception] = []
        for callback in callbacks:
            if callable(callback):
                try:
                    callback()
                except Exception as error:
                    errors.append(error)
        return errors

    def poll_progress() -> None:
        if generation != _generation or _live_collection() is not expected_col:
            stop_polling()
            return
        try:
            if mw.progress.want_cancel():
                cancel_requested.set()
            progress = expected_col.latest_progress()
            if progress.HasField("normal_sync"):
                normal = progress.normal_sync
                mw.progress.update(label=f"{normal.added}\n{normal.removed}", process=False)
                mw.progress.set_title(normal.stage)
                if cancel_requested.is_set():
                    expected_col.abort_sync()
        except Exception as error:
            # A broken progress API cannot keep raising on every Qt tick.
            poll_errors.append(error)
            cancel_requested.set()
            try:
                expected_col.abort_sync()
            except Exception:
                pass
            stop_polling()

    def operation(col):
        if generation != _generation or col is not expected_col:
            raise SafetyError("The profile or collection changed before sync started.")
        preflight(col, backup_folder, config["minimum_free_space_gib"])
        if cancel_requested.is_set():
            raise Interrupted("Cancelled before synchronization.")
        backup = mirror_status = None
        if make_backup:
            # A successfully written backup can still contain existing damage;
            # verify the archive independently before sending data to AnkiWeb.
            from .backup_safety import create_verified_backup
            backup = create_verified_backup(col, backup_folder)
            mirror_status = _mirror_verified_backup(backup, config, profile_tag, collection_path)
        if cancel_requested.is_set():
            raise Interrupted("Cancelled before synchronization.")
        return col.sync_collection(auth, sync_media), make_backup, mirror_status

    def failed(error: Exception) -> None:
        global _sync_requested
        stop_polling()
        if poll_errors:
            error = SafetyError(f"Sync progress monitoring failed: {poll_errors[0]}")
        if generation == _generation:
            _sync_requested = False
        finalization_errors = finalize_ui()
        if finalization_errors:
            error = SafetyError(f"{error}; Anki UI refresh also failed: {finalization_errors[0]}")
        cancelled = isinstance(error, Interrupted)
        message = "Backup/sync was cancelled." if cancelled else f"Backup/sync stopped: {_describe_error(error)}"
        try:
            _set_pause(key, message)
        except Exception:
            _session_pauses[key] = message
        # One message per failed request. The persisted latch prevents timer retries.
        if generation == _generation and _profile_open:
            if cancelled:
                tooltip(message + " Automatic attempts are paused.", parent=mw)
            else:
                showWarning(message + "\n\nAutomatic attempts are paused. Resolve the cause, then use Check Collection & Resume.", parent=mw)

    def finished(result) -> None:
        global _sync_requested, _last_sync_at, _last_mirror_status
        stop_polling()
        if generation != _generation or _live_collection() is not expected_col:
            return
        try:
            out, backed_up, mirror_status = result
            if backed_up:
                # The verified backup exists even if the sync step below fails.
                _record_verified_backup(key)
            if mirror_status is not None:
                _last_mirror_status = mirror_status
            if poll_errors:
                raise SafetyError(f"Sync progress monitoring failed: {poll_errors[0]}")
            # Like Anki: remember the server shard/endpoint and show its message
            # even when a full sync is required.
            mw.pm.set_host_number(out.host_number)
            if out.new_endpoint:
                mw.pm.set_current_sync_url(out.new_endpoint)
            if out.server_message:
                showText(out.server_message, parent=mw)
            # Anki's normal sync API may request a one-way sync; never select it.
            if out.required != out.NO_CHANGES:
                raise SafetyError("Anki requires a full synchronization. Use Anki's Sync button and review its upload/download choice.")
            _sync_requested = False
            finalization_errors = finalize_ui()
            if finalization_errors:
                raise SafetyError(f"Anki UI refresh failed: {finalization_errors[0]}")
            _clear_pause(key)
            _last_sync_at = datetime.now()
            if sync_media:
                mw.media_syncer.start_monitoring()
            if mirror_status and _mirror_failed(mirror_status):
                # Not a collection problem, so no pause; but never fail silently.
                showWarning("Sync completed, but the verified backup could not be copied "
                            f"to the mirror folder.\n\n{mirror_status}", parent=mw)
            elif not automatic or config["show_notifications"]:
                tooltip("Collection sync completed.", parent=mw)
            _schedule_next()
        except Exception as error:
            failed(error)

    try:
        sync_hook_started = True
        gui_hooks.sync_will_start()
        qconnect(poll_timer.timeout, poll_progress)
        poll_timer.setInterval(250)
        poll_timer.start()
        _run_query(QueryOp(parent=mw, op=operation, success=finished).failure(failed),
                   "Checking collection and synchronizing…")
    except Exception as error:
        failed(error)


def _timer_tick() -> None:
    try:
        _timer_tick_checked()
    except Exception as error:
        key = _current_key()
        if key:
            try:
                _set_pause(key, f"Automatic sync readiness check failed: {error}")
            except Exception:
                _session_pauses[key] = str(error)
        # No dialog from a recurring readiness timer. Status displays the pause.
        if _timer is not None:
            try:
                _timer.stop()
            except RuntimeError:
                pass


def _timer_tick_checked() -> None:
    global _next_due_at
    if not _profile_open or _pause_reason():
        return
    if time.monotonic() < _next_due_at:
        return
    if _can_run_unattended():
        _request_sync(automatic=True)
    else:
        # Busy/UI states can be retried; failures are persistently latched above.
        _next_due_at = time.monotonic() + 60


def _sync_finished(*_args) -> None:
    global _external_syncing
    # Anki emits this hook after errors too. Never use it to clear a safety pause
    # or claim that an external sync succeeded.
    _external_syncing = False
    _schedule_next()


def _sync_will_start(*_args) -> None:
    global _external_syncing
    if not _sync_requested:
        _external_syncing = True


def _state_changed(new_state, old_state, *_args) -> None:
    try:
        config = _config()
        if (
            old_state == "review"
            and new_state != "review"
            and config["enabled"]
            and config["sync_after_review"]
            and time.monotonic() >= _next_due_at
        ):
            _request_sync(automatic=True)
    except Exception as error:
        key = _current_key()
        if key:
            try:
                _set_pause(key, f"Post-review sync readiness failed: {error}")
            except Exception:
                _session_pauses[key] = str(error)


def _setup_timer(*_args) -> None:
    global _timer, _sync_requested, _profile_open, _generation, _external_syncing, _last_sync_at
    _generation += 1
    _profile_open = True
    _sync_requested = False
    _external_syncing = False
    _last_sync_at = None
    try:
        raw = mw.addonManager.getConfig(__name__) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict) or raw.get("safety_policy_version") != POLICY_VERSION:
        config = _config()
        config.update(enabled=False, sync_after_review=False, safety_policy_version=POLICY_VERSION)
        try:
            _save_config(config)
        except Exception as error:
            key = _current_key()
            if key:
                _session_pauses[key] = f"Could not save upgraded safety settings: {error}"
    if _timer is not None:
        try:
            _timer.stop()
            _timer.deleteLater()
        except RuntimeError:
            pass
    _timer = QTimer(mw)
    _timer.setInterval(30 * 1000)
    qconnect(_timer.timeout, _timer_tick)
    _schedule_next()
    _timer.start()
    generation = _generation
    try:
        QTimer.singleShot(STARTUP_CHECK_DELAY_MS, lambda: _startup_integrity_check(generation))
    except Exception:
        pass


def _stop_timer(*_args) -> None:
    global _timer, _sync_requested, _profile_open, _generation, _external_syncing, _poll_timer
    _generation += 1
    _profile_open = False
    _external_syncing = False
    if _timer is not None:
        try:
            _timer.stop()
            _timer.deleteLater()
        except RuntimeError:
            pass
        _timer = None
    _sync_requested = False
    if _poll_timer is not None:
        try:
            _poll_timer.stop()
        except RuntimeError:
            pass
        # The operation callback owns deletion of this timer.
        _poll_timer = None


def _run_verified_backup(*, clear_pause: bool) -> None:
    """Verify the collection, create and independently check a backup, mirror it.

    With clear_pause=True this is "Check Collection & Resume": only this explicit,
    successful check clears a failure latch. Neither variant starts a sync.
    """
    global _sync_requested
    reason = _blocked_reason(require_enabled=False, respect_review=False,
                             ignore_pause=True, require_sync=False)
    if reason:
        showWarning(reason, parent=mw)
        return
    expected_col, generation, key = _live_collection(), _generation, _current_key()
    if key is None:
        showWarning("No Anki collection is currently open.", parent=mw)
        return
    try:
        backup_folder = mw.pm.backupFolder()
    except Exception as error:
        showWarning(f"Anki's backup folder is unavailable.\n\n{error}", parent=mw)
        return
    config = _config()
    profile_tag = _profile_tag()
    collection_path = str(expected_col.path)
    _sync_requested = True

    def check(col):
        if col is not expected_col or generation != _generation:
            raise SafetyError("The profile changed before the check started.")
        preflight(col, backup_folder, config["minimum_free_space_gib"])
        from .backup_safety import create_verified_backup
        backup = create_verified_backup(col, backup_folder)
        return backup, _mirror_verified_backup(backup, config, profile_tag, collection_path)

    def failed(error):
        global _sync_requested
        if generation == _generation:
            _sync_requested = False
        message = f"Recovery check failed: {error}" if clear_pause else f"Verified backup failed: {error}"
        try:
            _set_pause(key, message)
        except Exception:
            _session_pauses[key] = str(error)
        if generation == _generation and _profile_open:
            showWarning(f"Checks failed; sync is paused.\n\n{error}", parent=mw)

    def checked(result):
        global _sync_requested, _last_mirror_status
        if generation != _generation or expected_col is not _live_collection():
            return
        _sync_requested = False
        backup, mirror_status = result
        try:
            _record_verified_backup(key)
            if clear_pause:
                _clear_pause(key)
        except Exception as error:
            failed(error)
            return
        _last_mirror_status = mirror_status
        _schedule_next()
        where = getattr(backup, "path", None) or "Anki's backup folder"
        if _mirror_failed(mirror_status):
            showWarning("The collection and its backup were verified, but copying the backup "
                        f"to the mirror folder failed.\n\nBackup: {where}\n{mirror_status}",
                        parent=mw)
            return
        mirrored = "" if mirror_status == "Not configured" else f"\nMirror: {mirror_status}"
        if clear_pause:
            tooltip("Collection and backup verified. Safety pause cleared; no sync was started."
                    + mirrored, parent=mw, period=6000)
        else:
            tooltip(f"Verified backup created: {where}{mirrored}", parent=mw, period=6000)

    try:
        _run_query(QueryOp(parent=mw, op=check, success=checked).failure(failed),
                   "Checking collection and creating a verified backup…")
    except Exception as error:
        failed(error)


def check_and_resume() -> None:
    """Only an explicit, successfully verified recovery clears a failure latch."""
    _run_verified_backup(clear_pause=True)


def verified_backup_now() -> None:
    """Make a checked (and optionally mirrored) backup without any network access."""
    _run_verified_backup(clear_pause=False)


def _startup_integrity_check(generation: int) -> None:
    """Detect silent storage damage early, before good backups age out.

    Anki's own backups copy whatever bytes are on disk; damage noticed days later
    may already be in every recent backup. A full SQLite integrity_check takes a
    few seconds and is read-only.
    """
    global _sync_requested
    try:
        if generation != _generation or not _profile_open or not _config()["startup_integrity_check"]:
            return
        col, key = _live_collection(), _current_key()
        if col is None or key is None or key in _startup_check_done:
            return
        if _sync_requested or _external_syncing or getattr(mw, "_background_op_count", 0):
            QTimer.singleShot(STARTUP_CHECK_DELAY_MS, lambda: _startup_integrity_check(generation))
            return
        _startup_check_done.add(key)
    except Exception:
        return

    def check(checked_col):
        if checked_col is not col or generation != _generation:
            return None
        rows = checked_col.db.all("PRAGMA integrity_check(1)")
        if rows != [["ok"]] and rows != [("ok",)]:
            raise SafetyError(f"SQLite integrity_check reported: {rows!r}")
        return None

    def failed(error):
        reason = (f"The collection failed its startup integrity check ({error}). Do not sync. "
                  "Close Anki, check the disk, and restore a verified backup.")
        try:
            _set_pause(key, reason)
        except Exception:
            _session_pauses[key] = reason
        if generation == _generation and _profile_open:
            showWarning(reason, parent=mw)

    try:
        _run_query(QueryOp(parent=mw, op=check, success=lambda _r: None).failure(failed))
    except Exception as error:
        failed(error)


def backup_and_sync_now() -> None:
    reason = _blocked_reason(require_enabled=False, respect_review=False)
    if reason:
        showWarning(f"Backup & Sync could not start.\n\n{reason}")
        return
    _request_sync(automatic=False, force_backup=True)


def sync_now() -> None:
    reason = _blocked_reason(require_enabled=False, respect_review=False)
    if reason:
        showWarning(f"Sync could not start.\n\n{reason}")
        return
    _request_sync(automatic=False)


def _format_countdown() -> str:
    if _pause_reason():
        return "Safety pause"
    if not _config()["enabled"]:
        return "Disabled"
    seconds = max(0, int(_next_due_at - time.monotonic()))
    return f"{seconds // 60}:{seconds % 60:02d}"


def show_status() -> None:
    config = _config()
    dialog = QDialog(mw)
    dialog.setObjectName("periodicSyncStatus")
    dialog.setWindowTitle("Backup & Sync Status")
    dialog.setMinimumWidth(600)
    dialog.setStyleSheet(_dialog_style(dialog))
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(18, 17, 18, 16)
    layout.setSpacing(11)

    header = QHBoxLayout()
    title_stack = QVBoxLayout()
    title_stack.setSpacing(1)
    title_stack.addWidget(_heading("Backup & Sync"))
    subtitle = QLabel("Current state of periodic collection synchronization")
    subtitle.setObjectName("dialogSubtitle")
    title_stack.addWidget(subtitle)
    header.addLayout(title_stack)
    header.addStretch(1)
    badge = QLabel("Safety pause" if _pause_reason() else ("Enabled" if config["enabled"] else "Disabled"))
    badge.setObjectName("statusBadge")
    header.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addLayout(header)

    last = (
        _last_sync_at.strftime("%H:%M:%S")
        if _last_sync_at
        else "None in this Anki session"
    )
    try:
        signed_in = bool(mw.pm.sync_auth())
    except Exception:
        signed_in = False
    automatic_reason = _blocked_reason(require_enabled=True, respect_review=True)
    readiness = "Ready" if automatic_reason is None else automatic_reason
    metrics = QGridLayout()
    metrics.setHorizontalSpacing(10)
    metrics.setVerticalSpacing(10)
    key = _current_key()
    try:
        last_backup_at = float(config["last_verified_backups"].get(key) or 0) if key else 0
    except (TypeError, ValueError):
        last_backup_at = 0
    last_backup = (datetime.fromtimestamp(last_backup_at).strftime("%d %b %H:%M")
                   if last_backup_at else "None recorded")
    values = (
        ("Next automatic attempt", _format_countdown()),
        ("Last completed sync", last),
        ("AnkiWeb account", "Signed in" if signed_in else "Not signed in"),
        ("Current readiness", readiness),
        ("Last verified backup", last_backup),
        ("Backup mirror", config["mirror_folder"] and _last_mirror_status or "Not configured"),
    )
    for index, (label_text, value_text) in enumerate(values):
        card = QFrame()
        card.setObjectName("metricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(11, 9, 11, 9)
        card_layout.setSpacing(2)
        label = QLabel(label_text.upper())
        label.setObjectName("mutedLabel")
        value = QLabel(value_text)
        value.setWordWrap(True)
        value_font = value.font()
        value_font.setWeight(QFont.Weight.DemiBold)
        value.setFont(value_font)
        card_layout.addWidget(label)
        card_layout.addWidget(value)
        metrics.addWidget(card, index // 2, index % 2)
    metrics.setColumnStretch(0, 1)
    metrics.setColumnStretch(1, 1)
    layout.addLayout(metrics)

    policy = QLabel(
        f"Automatic interval: <b>{_interval_minutes()} minutes</b> &nbsp;·&nbsp; "
        f"During reviews: <b>{'postpone' if config['skip_while_reviewing'] else 'allowed'}</b> "
        f"&nbsp;·&nbsp; After review: <b>{'sync' if config['sync_after_review'] else 'wait'}</b><br>"
        f"Pre-sync backup: <b>{'at most every ' + str(config['minimum_backup_interval_minutes']) + ' min' if config['backup_before_periodic_sync'] else 'off'}</b> "
        f"&nbsp;·&nbsp; Notifications: <b>{'enabled' if config['show_notifications'] else 'quiet'}</b>"
    )
    policy.setObjectName("infoBox")
    policy.setWordWrap(True)
    layout.addWidget(policy)

    actions = QHBoxLayout()
    verified_button = QPushButton("Verified Backup Now")
    sync_button = QPushButton("Sync Now")
    backup_button = QPushButton("Backup + Sync Now")
    resume_button = QPushButton("Check Collection && Resume")
    actions.addWidget(verified_button)
    actions.addWidget(sync_button)
    actions.addWidget(backup_button)
    actions.addWidget(resume_button)
    actions.addStretch(1)
    layout.addLayout(actions)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    qconnect(buttons.rejected, dialog.reject)
    layout.addWidget(buttons)

    def run_after_close(callback) -> None:
        dialog.accept()
        QTimer.singleShot(0, callback)

    qconnect(sync_button.clicked, lambda: run_after_close(sync_now))
    qconnect(
        backup_button.clicked, lambda: run_after_close(backup_and_sync_now)
    )
    qconnect(resume_button.clicked, lambda: run_after_close(check_and_resume))
    qconnect(verified_button.clicked, lambda: run_after_close(verified_backup_now))
    dialog.exec()


def _save_user_settings(chosen: dict) -> None:
    """Re-read config at save time: a safety pause or backup record written while
    the dialog was open must survive. Only user-owned keys come from the dialog."""
    latest = _config()
    latest.update({k: chosen[k] for k in USER_SETTING_KEYS if k in chosen})
    latest["safety_policy_version"] = POLICY_VERSION
    _save_config(latest)


def open_settings() -> None:
    config = _config()
    dialog = QDialog(mw)
    dialog.setObjectName("periodicSyncSettings")
    dialog.setWindowTitle("Backup & Sync Settings")
    dialog.setMinimumWidth(620)
    dialog.setStyleSheet(_dialog_style(dialog))
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 16)
    outer.setSpacing(8)
    # The settings scroll on small screens instead of squeezing rows on top of each other.
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setObjectName("periodicSyncSettingsBody")
    scroll.setWidget(body)
    scroll.viewport().setAutoFillBackground(False)
    body.setAutoFillBackground(False)
    outer.addWidget(scroll, 1)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(18, 17, 18, 4)
    layout.setSpacing(11)
    layout.addWidget(_heading("Periodic Backup & Sync"))
    intro = QLabel(
        "Keep this open profile synchronized at a predictable interval. Local card "
        "changes are always saved immediately; this controls AnkiWeb synchronization."
    )
    intro.setObjectName("dialogSubtitle")
    intro.setWordWrap(True)
    layout.addWidget(intro)

    enabled = QCheckBox("Enable automatic periodic collection sync")
    enabled.setChecked(bool(config["enabled"]))
    enabled_font = enabled.font()
    enabled_font.setWeight(QFont.Weight.DemiBold)
    enabled.setFont(enabled_font)
    layout.addWidget(enabled)

    schedule_group = QGroupBox("Schedule")
    schedule_form = QFormLayout(schedule_group)
    interval = QSpinBox()
    interval.setRange(5, 240)
    interval.setSuffix(" minutes")
    interval.setValue(_interval_minutes())
    schedule_form.addRow("Attempt a sync every:", interval)
    schedule_hint = QLabel(
        "Requires automatic sync enabled in Anki Preferences. Busy states postpone "
        "the attempt. Any failed check, backup or sync pauses further attempts."
    )
    schedule_hint.setObjectName("mutedLabel")
    schedule_hint.setWordWrap(True)
    schedule_form.addRow(schedule_hint)
    layout.addWidget(schedule_group)

    review_group = QGroupBox("While studying")
    review_layout = QVBoxLayout(review_group)
    skip_review = QCheckBox("Postpone while reviewing")
    skip_review.setChecked(bool(config["skip_while_reviewing"]))
    after_review = QCheckBox("Sync once after leaving the review screen")
    after_review.setChecked(bool(config["sync_after_review"]))
    review_layout.addWidget(skip_review)
    review_layout.addWidget(after_review)
    layout.addWidget(review_group)

    safety_group = QGroupBox("Safety and feedback")
    safety_layout = QVBoxLayout(safety_group)
    backup = QCheckBox("Create and independently verify a local backup before sync")
    backup.setChecked(bool(config["backup_before_periodic_sync"]))
    notifications = QCheckBox("Show a small notification when automatic sync completes")
    notifications.setChecked(bool(config["show_notifications"]))
    safety_layout.addWidget(backup)
    safety_layout.addWidget(notifications)
    backup_interval = QSpinBox()
    backup_interval.setRange(10, 1440)
    backup_interval.setSuffix(" minutes")
    backup_interval.setValue(config["minimum_backup_interval_minutes"])
    free_space = QSpinBox()
    free_space.setRange(2, 100)
    free_space.setSuffix(" GiB")
    free_space.setValue(config["minimum_free_space_gib"])
    free_space_form = QFormLayout()
    free_space_form.addRow("New backup at most every:", backup_interval)
    free_space_form.addRow("Minimum free storage:", free_space)
    safety_layout.addLayout(free_space_form)
    layout.addWidget(safety_group)

    # Always editable: protection that does not depend on automatic sync.
    protection_group = QGroupBox("Backup protection (works without automatic sync)")
    protection_layout = QVBoxLayout(protection_group)
    startup_check = QCheckBox("Run a full SQLite integrity check shortly after the profile opens")
    startup_check.setChecked(bool(config["startup_integrity_check"]))
    protection_layout.addWidget(startup_check)
    mirror_row = QHBoxLayout()
    mirror_edit = QLineEdit(config["mirror_folder"])
    mirror_edit.setPlaceholderText("Optional: another drive or an iCloud/Google Drive folder")
    browse = QPushButton("Choose…")
    clear_mirror = QPushButton("Clear")
    mirror_row.addWidget(mirror_edit, 1)
    mirror_row.addWidget(browse)
    mirror_row.addWidget(clear_mirror)
    mirror_form = QFormLayout()
    mirror_form.addRow("Copy verified backups to:", mirror_row)
    mirror_keep = QSpinBox()
    mirror_keep.setRange(1, 100)
    mirror_keep.setValue(config["mirror_keep"])
    mirror_form.addRow("Keep newest copies there:", mirror_keep)
    protection_layout.addLayout(mirror_form)
    mirror_hint = QLabel(
        "Each copy is checksum-verified. Only files this add-on created "
        "(anki-verified-*.colpkg) are ever removed from that folder."
    )
    mirror_hint.setObjectName("mutedLabel")
    mirror_hint.setWordWrap(True)
    protection_layout.addWidget(mirror_hint)
    layout.addWidget(protection_group)

    def choose_folder() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, "Folder for verified backup copies",
                                                  mirror_edit.text() or "")
        if chosen:
            mirror_edit.setText(chosen)

    qconnect(browse.clicked, choose_folder)
    qconnect(clear_mirror.clicked, lambda: mirror_edit.setText(""))

    note = QLabel(
        "Collection integrity and free storage are checked before every request. "
        "A safety pause survives restarts and requires Check Collection & Resume. "
        "Full upload/download always needs a deliberate choice through Anki's Sync button. "
        "Keep an independent backup: a backup on the same disk cannot protect against disk failure."
    )
    note.setObjectName("infoBox")
    note.setWordWrap(True)
    layout.addWidget(note)

    def update_enabled_state(checked: bool) -> None:
        schedule_group.setEnabled(checked)
        review_group.setEnabled(checked)
        safety_group.setEnabled(checked)

    qconnect(enabled.toggled, update_enabled_state)
    update_enabled_state(enabled.isChecked())

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save Settings")
    qconnect(buttons.accepted, dialog.accept)
    qconnect(buttons.rejected, dialog.reject)
    button_row = QHBoxLayout()
    button_row.setContentsMargins(18, 0, 18, 0)
    button_row.addWidget(buttons)
    outer.addLayout(button_row)
    screen = dialog.screen() or mw.screen()
    available = screen.availableGeometry().height() if screen is not None else 800
    wanted = body.sizeHint().height() + buttons.sizeHint().height() + 40
    dialog.resize(max(620, body.sizeHint().width() + 24), min(wanted, int(available * 0.9)))
    if not dialog.exec():
        return
    chosen = {
        "enabled": enabled.isChecked(),
        "sync_interval_minutes": interval.value(),
        "skip_while_reviewing": skip_review.isChecked(),
        "sync_after_review": after_review.isChecked(),
        "backup_before_periodic_sync": backup.isChecked(),
        "minimum_backup_interval_minutes": backup_interval.value(),
        "mirror_folder": mirror_edit.text().strip(),
        "mirror_keep": mirror_keep.value(),
        "startup_integrity_check": startup_check.isChecked(),
        "show_notifications": notifications.isChecked(),
        "minimum_free_space_gib": free_space.value(),
    }
    try:
        _save_user_settings(chosen)
    except Exception as error:
        showWarning(f"Could not save settings.\n\n{error}", parent=mw)
        return
    _schedule_next()
    tooltip("Periodic sync settings saved.", parent=mw)


backup_sync_menu = _backup_sync_menu()

settings_action = QAction("Settings…", mw)
qconnect(settings_action.triggered, open_settings)
backup_sync_menu.addAction(settings_action)

sync_action = QAction("Sync Now", mw)
qconnect(sync_action.triggered, sync_now)
backup_sync_menu.addAction(sync_action)

verified_action = QAction("Verified Backup Now (no sync)", mw)
qconnect(verified_action.triggered, verified_backup_now)
backup_sync_menu.addAction(verified_action)

backup_action = QAction("Backup + Sync Now", mw)
qconnect(backup_action.triggered, backup_and_sync_now)
backup_sync_menu.addAction(backup_action)

status_action = QAction("Status…", mw)
qconnect(status_action.triggered, show_status)
backup_sync_menu.addAction(status_action)

resume_action = QAction("Check Collection && Resume…", mw)
qconnect(resume_action.triggered, check_and_resume)
backup_sync_menu.addAction(resume_action)

gui_hooks.profile_did_open.append(_setup_timer)
gui_hooks.profile_will_close.append(_stop_timer)
gui_hooks.sync_did_finish.append(_sync_finished)
gui_hooks.sync_will_start.append(_sync_will_start)
gui_hooks.state_did_change.append(_state_changed)

# Unknown future hooks do not enable automatic work. Existing profile hooks
# remain sufficient to stop timers on shutdown.
temporary_close = getattr(gui_hooks, "collection_will_temporarily_close", None)
if temporary_close is not None:
    temporary_close.append(_stop_timer)
temporary_reopen = getattr(gui_hooks, "collection_did_temporarily_close", None)
if temporary_reopen is not None:
    temporary_reopen.append(_setup_timer)
