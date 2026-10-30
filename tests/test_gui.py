import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
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


def test_duty_cycle_menu_item_follows_node(qapp, monkeypatch):
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    ctrl = Controller(Settings())
    bridge = CoreBridge(ctrl)
    ctrl.start()
    try:
        w = MainWindow(bridge)
        a = w.duty_action
        assert a.isVisible() is False
        bridge.duty.emit(True, True, False)
        assert a.isVisible()
        assert a.isChecked() and a.isEnabled()
        bridge.duty.emit(True, True, True)
        assert not a.isEnabled()
        bridge.duty.emit(False, False, False)
        assert not a.isChecked() and not a.isVisible()
        w.close()
    finally:
        ctrl.shutdown()


def test_dialogs_are_non_native(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    from cqta.gui import dialogs

    def answer():
        box = next(x for x in qapp.topLevelWidgets() if isinstance(x, QMessageBox))
        assert box.testOption(QMessageBox.Option.DontUseNativeDialog)
        box.button(QMessageBox.StandardButton.Yes).click()

    QTimer.singleShot(100, answer)
    assert dialogs.question(None, "Close QSO", "Send QRT?") is True


def test_times_are_utc(monkeypatch):
    import time

    from cqta.gui import timefmt

    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    assert timefmt.hms(86400 + 3661) == "01:01:01Z"
    assert timefmt.hm(0) == "00:00Z"
    assert timefmt.date_hm(0) == "1970-01-01 00:00Z"


def test_last_heard_shows_clock_and_age(qapp):
    import time

    from cqta.app.chat import Station
    from cqta.gui import timefmt
    from cqta.gui.panels import StationsPanel

    ctrl = Controller(Settings())
    panel = StationsPanel(CoreBridge(ctrl))
    t = time.time() - 125
    panel.set_stations([Station("W2XYZ", t, "10.1.2.3")])
    text = panel.table.item(0, 1).text()
    assert text == f"{timefmt.hm(t)} (2 min ago)"
    panel.set_stations([Station("W2XYZ", time.time() - 20, "10.1.2.3")])
    assert panel.table.item(0, 1).text().endswith("(<1 min ago)")


def test_app_icon_is_the_cqta_png(qapp):
    from cqta.gui.app import ICON_PATH, make_icon

    assert ICON_PATH.name == "cqta.png" and ICON_PATH.exists()
    assert not make_icon().isNull()


class FakeScanner:
    instances = []

    def __init__(self, on_device, on_error):
        self.on_device, self.on_error, self.running = on_device, on_error, False
        FakeScanner.instances.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def test_ble_selection_like_ios(qapp, monkeypatch):
    import cqta.gui.connection_dialog as cd

    monkeypatch.setattr(cd, "BleScanner", FakeScanner)
    FakeScanner.instances.clear()
    s = Settings()
    s.transport = "meshtastic"
    s.mesh.kind = "ble"
    d = ConnectionDialog(s)
    assert not d.connect_btn.isEnabled()  # nothing selected yet
    d.show()
    qapp.processEvents()
    sc = FakeScanner.instances[-1]
    assert sc.running
    sc.on_device("AA:01", "Meshtastic_1234", -70)
    sc.on_device("AA:02", "Meshtastic_abcd", -55)
    sc.on_device("AA:01", "Meshtastic_1234", -60)  # same radio again: updated in place, order kept
    pump(qapp, 3)
    r = d.mesh.radios
    assert r.rowCount() == 2
    assert r.item(0, 1).text() == "Meshtastic_1234" and r.item(0, 2).text() == "-60 dBm"
    d.mesh._pick(1, 0)
    assert r.item(1, 0).text() == "✓" and r.item(0, 0).text() == ""
    assert d.connect_btn.isEnabled()
    d._accept()
    assert (d.settings.mesh.kind, d.settings.mesh.address, d.settings.mesh.name) == ("ble", "AA:02", "Meshtastic_abcd")
    assert not sc.running  # scanning stops when the dialog closes


def test_ble_scan_stops_when_leaving_bluetooth(qapp, monkeypatch):
    import cqta.gui.connection_dialog as cd

    monkeypatch.setattr(cd, "BleScanner", FakeScanner)
    FakeScanner.instances.clear()
    s = Settings()
    s.mesh.kind = "ble"
    d = ConnectionDialog(s)
    d.show()
    sc = FakeScanner.instances[-1]
    assert sc.running
    d.transport.setCurrentIndex(2)
    assert not sc.running
    d.reject()


def test_nodes_sortable_and_contacts_menu(qapp, tmp_path, monkeypatch):
    from cqta.app.controller import NodeRow
    from cqta.gui.nodes_window import NodesWindow

    w = NodesWindow(CoreBridge(Controller(Settings())))
    w.set_nodes([NodeRow(1, "Zed", "Z", "10.115.0.9", 100.0), NodeRow(2, "alpha", "A", "10.115.0.10", 300.0),
                 NodeRow(3, "Mid", "M", "10.115.0.2", 200.0)])
    col = lambda c: [w.nodes.item(r, c).text() for r in range(3)]
    w.nodes.sortByColumn(0, Qt.AscendingOrder)
    assert col(0) == ["alpha", "Mid", "Zed"]
    w.nodes.sortByColumn(2, Qt.AscendingOrder)
    assert col(2) == ["10.115.0.2", "10.115.0.9", "10.115.0.10"]  # numeric, not textual
    w.nodes.sortByColumn(3, Qt.DescendingOrder)
    assert col(0) == ["alpha", "Mid", "Zed"]
    w.set_nodes([NodeRow(3, "Mid", "M", "10.115.0.2", 200.0)])  # a refresh keeps working while sorted
    assert w.nodes.rowCount() == 1


def test_file_window_known_stations(qapp, monkeypatch):
    from cqta.gui.files_window import FilesWindow

    ctrl = Controller(Settings())
    ctrl.settings.file_target = "W1AW"
    ctrl.start()
    try:
        w = FilesWindow(CoreBridge(ctrl))
        assert w.target.text() == "W1AW"
        w._fill_contacts()
        assert [a.text() for a in w.contacts_menu.actions()] == ["No known contacts yet"]
        monkeypatch.setattr(Controller, "contacts", lambda self: [("K1ABC · 10.0.0.5", "K1ABC")])
        w._fill_contacts()
        w.contacts_menu.actions()[0].trigger()
        assert w.target.text() == "K1ABC"
    finally:
        ctrl.shutdown()


def test_nodes_window_follows_transport(qapp):
    from cqta.app.controller import NodeRow
    from cqta.gui.nodes_window import NodesWindow

    w = NodesWindow(CoreBridge(Controller(Settings())))
    w.set_nodes([NodeRow(0, "K1ABC", "", "10.0.0.5", 100.0)], "kiss")
    assert "Heard stations" in w.nodes_title.text()
    assert w.nodes.isColumnHidden(1) and w.nodes.item(0, 0).text() == "K1ABC"
    assert w.nodes.horizontalHeaderItem(0).text() == "Callsign"
    w.set_nodes([NodeRow(1, "Zed", "Z", "10.115.0.9", 100.0)], "meshtastic")
    assert "Meshtastic nodes" in w.nodes_title.text() and not w.nodes.isColumnHidden(1)
    w.set_nodes([], "")
    assert w.nodes.rowCount() == 0
