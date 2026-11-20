"""Load the add-on into an offscreen stand-in main window."""
import importlib
import os
import sys
import types

from aqt.qt import QApplication, QDialog, QMainWindow, QMenu

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.environ.get("BS_SHOTS")


def _menu_tree(menu, indent=0):
    out = []
    for a in menu.actions():
        out.append(" " * indent + (a.text() or "----"))
        if a.menu():
            out += _menu_tree(a.menu(), indent + 2)
    return out


def _load():
    import aqt

    app = QApplication.instance() or QApplication([])
    saved = {}
    mw = QMainWindow()
    mw.app = app
    mw.col = None
    mw.form = types.SimpleNamespace(menuTools=QMenu("Tools"))
    mw.addonManager = types.SimpleNamespace(getConfig=lambda _n: dict(saved) or None,
                                            writeConfig=lambda _n, c: saved.update(c),
                                            setConfigAction=lambda *_a: None)
    aqt.mw = mw
    sys.path.insert(0, ROOT)
    sys.modules.pop("periodic_backup_sync", None)
    return mw, importlib.import_module("periodic_backup_sync")


def test_menu_joins_learning_toolkit_menu():
    mw, _pkg = _load()
    tree = _menu_tree(mw.form.menuTools)
    assert tree[0] == "Learning Toolkit" and tree[1].strip() == "Backup && Sync"
    assert mw.form.menuTools.actions()[0].menu().objectName() == "codex_learning_toolkit_menu"
    assert "Verified Backup Now (no sync)" in [t.strip() for t in tree]


def test_automatic_sync_is_off_by_default():
    _mw, pkg = _load()
    assert pkg._config()["enabled"] is False


def test_settings_dialog_opens(monkeypatch):
    _mw, pkg = _load()
    seen = {}

    def fake_exec(self):
        pass  # the dialog sizes itself to the screen
        seen["title"] = self.windowTitle()
        if SHOTS:
            self.grab().save(os.path.join(SHOTS, "settings.png"))
        return 0

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    pkg.open_settings()
    assert seen["title"] == "Backup & Sync Settings"
