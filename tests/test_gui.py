import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from cqta.app.controller import Controller
from cqta.app.settings import Settings
from cqta.gui.bridge import CoreBridge
from cqta.gui.connection_dialog import ConnectionDialog
from cqta.gui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, n=20):
    import time

    for _ in range(n):
        app.processEvents()
        time.sleep(0.01)


def test_main_window_flow(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    s = Settings()
    s.callsign = "K1ABC"
    s.download_dir = str(tmp_path)
    ctrl = Controller(s)
    bridge = CoreBridge(ctrl)
    ctrl.start()
    try:
        w = MainWindow(bridge)
        w.show()
        w.open_qso("W2XYZ")
        pump(qapp)
        assert "W2XYZ" in w.qsos
        assert w.tabs.count() == 1
        w.files_window.show()
        w.nodes_window.show()
        w.log_window.show()
        pump(qapp)
        w.close()
    finally:
        ctrl.shutdown()


def test_connection_dialog(qapp):
    s = Settings()
    d = ConnectionDialog(s)
    d.transport.setCurrentIndex(2)
    d.kiss.host.setText("localhost")
    d._accept()
    assert d.settings.transport == "kiss"
    assert d.settings.kiss.host == "localhost"
