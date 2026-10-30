from __future__ import annotations

import copy
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..app.settings import Settings
from ..core.ipv4 import parse_address, random_ip
from ..transports import audio
from ..transports.network import list_interfaces
from ..transports.mesh_links import BleScanner, list_serial_ports
from . import dialogs

TRANSPORTS = [("meshtastic", "Meshtastic"), ("afsk", "AFSK sound modem"), ("kiss", "KISS TCP TNC"),
              ("network", "Network interface")]
MESH_KINDS = [("serial", "USB serial"), ("ble", "Bluetooth LE"), ("tcp", "TCP / Wi-Fi")]


def _spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    s.setFixedWidth(120)
    s.setAlignment(Qt.AlignRight)
    return s


class MeshPage(QWidget):
    """Meshtastic: serial port, live Bluetooth LE list (like the iOS app) or TCP host."""

    seen = Signal(str, str, int)
    scan_error = Signal(str)
    changed = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        c = settings.mesh
        self.kind = QComboBox()
        for k, label in MESH_KINDS:
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(max(self.kind.findData(c.kind), 0))

        self.serial = QComboBox()
        self.serial.setEditable(True)
        self._serial_saved = c.address if c.kind == "serial" else ""
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_serial)
        serial_row = QHBoxLayout()
        serial_row.setContentsMargins(0, 0, 0, 0)
        serial_row.addWidget(self.serial, 1)
        serial_row.addWidget(refresh)
        self.serial_widget = QWidget()
        self.serial_widget.setLayout(serial_row)

        self.host = QLineEdit(c.address if c.kind == "tcp" else "")
        self.host.setPlaceholderText("meshtastic.local or 192.168.1.20")
        self.port = _spin(1, 65535, c.port)
        tcp_row = QHBoxLayout()
        tcp_row.setContentsMargins(0, 0, 0, 0)
        tcp_row.addWidget(self.host, 1)
        tcp_row.addWidget(QLabel("Port"))
        tcp_row.addWidget(self.port)
        self.tcp_widget = QWidget()
        self.tcp_widget.setLayout(tcp_row)

        # BLE: devices in discovery order, RSSI updated in place, check mark on the selected radio
        self.ble_selected: tuple[str, str] = (c.address, c.name) if c.kind == "ble" and c.address else ("", "")
        self.radios = QTableWidget(0, 3)
        self.radios.horizontalHeader().hide()
        self.radios.verticalHeader().hide()
        self.radios.setShowGrid(False)
        self.radios.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.radios.setSelectionMode(QAbstractItemView.NoSelection)
        self.radios.setMinimumHeight(140)
        h = self.radios.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.radios.cellClicked.connect(self._pick)
        self.ble_status = QLabel()
        self.ble_status.setStyleSheet("color: gray")
        self.ble_status.setWordWrap(True)
        self.ble_widget = QWidget()
        bl = QVBoxLayout(self.ble_widget)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.radios)
        bl.addWidget(self.ble_status)
        self._rows: dict[str, int] = {}
        self._scanner: Optional[BleScanner] = None
        self._active = False

        self.note = QLabel()
        self.note.setWordWrap(True)
        form = QFormLayout(self)
        form.addRow("Connection", self.kind)
        form.addRow(self.serial_widget)
        form.addRow(self.ble_widget)
        form.addRow(self.tcp_widget)
        form.addRow(self.note)

        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.host.textChanged.connect(self.changed)
        self.serial.editTextChanged.connect(self.changed)
        self.seen.connect(self._on_seen)
        self.scan_error.connect(self._on_error)
        if c.kind == "ble" and c.address:
            self._add_row(c.address, c.name or c.address, None)
        self._kind_changed()

    # Kind switching and scanning

    def _kind_changed(self) -> None:
        k = self.kind.currentData()
        self.serial_widget.setVisible(k == "serial")
        self.ble_widget.setVisible(k == "ble")
        self.tcp_widget.setVisible(k == "tcp")
        self.note.setText({"ble": "", "serial": "",
                           "tcp": "Meshtastic nodes serve their API on TCP port 4403."}[k])
        if k == "serial":
            self.refresh_serial()
        self._update_scanning()
        self.changed.emit()

    def set_active(self, active: bool) -> None:
        """Scanning only runs while this page is on screen (as on iOS, where it follows the connection tab)."""
        self._active = active
        self._update_scanning()

    def _update_scanning(self) -> None:
        want = self._active and self.kind.currentData() == "ble"
        if want and self._scanner is None:
            self.ble_status.setText("Scanning for Meshtastic devices…")
            self.ble_status.setStyleSheet("color: gray")
            self._scanner = BleScanner(self.seen.emit, self.scan_error.emit)
            self._scanner.start()
        elif not want and self._scanner is not None:
            self._scanner.stop()
            self._scanner = None

    def _on_error(self, message: str) -> None:
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner = None
        self.ble_status.setText(message)
        self.ble_status.setStyleSheet("color: #c0392b")

    # BLE list

    def _add_row(self, address: str, name: str, rssi: Optional[int]) -> int:
        row = self.radios.rowCount()
        self.radios.insertRow(row)
        self._rows[address] = row
        self.radios.setItem(row, 0, QTableWidgetItem(""))
        self.radios.setItem(row, 1, QTableWidgetItem(name))
        r = QTableWidgetItem("" if rssi is None else f"{rssi} dBm")
        r.setForeground(Qt.gray)
        r.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.radios.setItem(row, 2, r)
        self.radios.item(row, 0).setData(Qt.UserRole, address)
        self._mark(row)
        return row

    def _mark(self, row: int) -> None:
        address = self.radios.item(row, 0).data(Qt.UserRole)
        self.radios.item(row, 0).setText("✓" if address == self.ble_selected[0] else "")

    def _on_seen(self, address: str, name: str, rssi: int) -> None:
        row = self._rows.get(address)
        if row is None:
            self._add_row(address, name, rssi)
            return
        self.radios.item(row, 1).setText(name)
        self.radios.item(row, 2).setText(f"{rssi} dBm")
        if address == self.ble_selected[0]:
            self.ble_selected = (address, name)

    def _pick(self, row: int, _col: int) -> None:
        address = self.radios.item(row, 0).data(Qt.UserRole)
        self.ble_selected = (address, self.radios.item(row, 1).text())
        for r in range(self.radios.rowCount()):
            self._mark(r)
        self.changed.emit()

    # Serial

    def refresh_serial(self) -> None:
        current = self.serial.currentText() or self._serial_saved
        self.serial.clear()
        for d in list_serial_ports():
            self.serial.addItem(d.label, d.address)
        idx = self.serial.findData(current)
        if idx >= 0:
            self.serial.setCurrentIndex(idx)
        else:
            self.serial.setEditText(current)

    def _serial_address(self) -> str:
        idx, text = self.serial.currentIndex(), self.serial.currentText()
        return self.serial.itemData(idx) if idx >= 0 and self.serial.itemText(idx) == text else text.strip()

    # Result

    def can_connect(self) -> bool:
        k = self.kind.currentData()
        return bool({"serial": self._serial_address(), "ble": self.ble_selected[0],
                     "tcp": self.host.text().strip()}[k])

    def apply(self, settings: Settings) -> Optional[str]:
        k = self.kind.currentData()
        if not self.can_connect():
            return "Choose a Meshtastic device." if k != "tcp" else "Enter the Meshtastic node's host name or address."
        m = settings.mesh
        m.kind, m.port = k, self.port.value()
        m.address = {"serial": self._serial_address, "ble": lambda: self.ble_selected[0],
                     "tcp": lambda: self.host.text().strip()}[k]()
        m.name = self.ble_selected[1] if k == "ble" else ""
        return None


class AfskPage(QWidget):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        c = settings.afsk
        self.input = QComboBox()
        self.output = QComboBox()
        self.address = QLineEdit(c.address)
        rnd = QPushButton("Random")
        rnd.clicked.connect(lambda: self.address.setText(random_ip()))
        self.level = QDoubleSpinBox()
        self.level.setFixedWidth(120)
        self.level.setAlignment(Qt.AlignRight)
        self.level.setRange(0.05, 1.0)
        self.level.setSingleStep(0.05)
        self.level.setValue(c.tx_level)
        self.tx_delay = _spin(0, 3000, c.tx_delay_ms, " ms")
        self.tx_tail = _spin(0, 1000, c.tx_tail_ms, " ms")
        self.csma = QCheckBox("Wait for a quiet channel before transmitting (CSMA)")
        self.csma.setChecked(c.csma)
        self.holdoff = _spin(0, 5000, c.clear_holdoff_ms, " ms")
        self.jitter = _spin(0, 5000, c.clear_jitter_ms, " ms")
        self.burst = _spin(1, 16, c.max_burst_frames)
        self.note = QLabel()
        self.note.setWordWrap(True)
        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(lambda: self._populate(c))

        ip_row = QHBoxLayout()
        ip_row.addWidget(self.address, 1)
        ip_row.addWidget(rnd)
        advanced = QGroupBox("Advanced")
        af = QFormLayout(advanced)
        af.addRow("TX delay (key-up lead-in)", self.tx_delay)
        af.addRow("TX tail", self.tx_tail)
        af.addRow(self.csma)
        af.addRow("Clear-channel hold-off", self.holdoff)
        af.addRow("Random extra hold-off", self.jitter)
        af.addRow("Frames per transmission", self.burst)
        form = QFormLayout()
        form.addRow("Input device (receive)", self.input)
        form.addRow("Output device (transmit)", self.output)
        form.addRow("", refresh)
        form.addRow("IP address / prefix", ip_row)
        form.addRow("Transmit level", self.level)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(advanced)
        layout.addWidget(self.note)
        self._populate(c)

    def _populate(self, c) -> None:
        self.input.clear()
        self.output.clear()
        self.input.addItem("System default", ("", ""))
        self.output.addItem("System default", ("", ""))
        try:
            ins, outs = audio.list_devices()
        except Exception as e:
            self.note.setText(f"Audio devices unavailable: {e}")
            return
        self.note.setText("Connect the radio's audio in/out (or a sound-card interface) and pick the matching devices.")
        for d in ins:
            self.input.addItem(d.label, (d.name, d.host_api))
        for d in outs:
            self.output.addItem(d.label, (d.name, d.host_api))
        i = self.input.findData((c.input_name, c.input_api))
        o = self.output.findData((c.output_name, c.output_api))
        self.input.setCurrentIndex(max(i, 0))
        self.output.setCurrentIndex(max(o, 0))

    def apply(self, settings: Settings) -> Optional[str]:
        if parse_address(self.address.text().strip()) is None:
            return "Enter a valid IP address, for example 10.115.4.7/16 (prefix 1-30)."
        c = settings.afsk
        c.input_name, c.input_api = self.input.currentData()
        c.output_name, c.output_api = self.output.currentData()
        c.address = self.address.text().strip()
        c.tx_level = self.level.value()
        c.tx_delay_ms, c.tx_tail_ms = self.tx_delay.value(), self.tx_tail.value()
        c.csma, c.clear_holdoff_ms, c.clear_jitter_ms = self.csma.isChecked(), self.holdoff.value(), self.jitter.value()
        c.max_burst_frames = self.burst.value()
        return None


class KissPage(QWidget):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        c = settings.kiss
        self.host = QLineEdit(c.host)
        self.port = _spin(1, 65535, c.tcp_port)
        self.kiss_port = _spin(0, 15, c.kiss_port)
        self.address = QLineEdit(c.address)
        rnd = QPushButton("Random")
        rnd.clicked.connect(lambda: self.address.setText(random_ip()))
        self.mtu = _spin(68, 1500, c.mtu)
        ip_row = QHBoxLayout()
        ip_row.addWidget(self.address, 1)
        ip_row.addWidget(rnd)
        form = QFormLayout(self)
        form.addRow("TNC host", self.host)
        form.addRow("TCP port", self.port)
        form.addRow("KISS port (0-15)", self.kiss_port)
        form.addRow("IP address / prefix", ip_row)
        form.addRow("MTU", self.mtu)
        note = QLabel("The TNC (for example Direwolf's KISS TCP port) carries raw IPv4 packets in KISS data frames, "
                      "like pytap3's kisstun.")
        note.setWordWrap(True)
        form.addRow(note)

    def apply(self, settings: Settings) -> Optional[str]:
        if not self.host.text().strip():
            return "Enter the TNC host name or address."
        if parse_address(self.address.text().strip()) is None:
            return "Enter a valid IP address, for example 10.115.4.7/16 (prefix 1-30)."
        c = settings.kiss
        c.host, c.tcp_port, c.kiss_port = self.host.text().strip(), self.port.value(), self.kiss_port.value()
        c.address, c.mtu = self.address.text().strip(), self.mtu.value()
        return None


class NetworkPage(QWidget):
    changed = Signal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._saved = settings.network.interface
        self.interface = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row = QHBoxLayout()
        row.addWidget(self.interface, 1)
        row.addWidget(refresh)
        form = QFormLayout(self)
        form.addRow("Interface", row)
        note = QLabel("Uses plain UDP over an interface that is already configured in the operating system "
                      "(Ethernet, Wi-Fi, VPN). CQta messages are broadcast to that interface's broadcast address on "
                      "UDP port 37373, and file transfers use UDP port 69. No elevated privileges and no tunnel "
                      "interface are needed. Ping is not available.")
        note.setWordWrap(True)
        form.addRow(note)
        self.interface.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.refresh()

    def refresh(self) -> None:
        keep = self.interface.currentData() or self._saved
        self.interface.blockSignals(True)
        self.interface.clear()
        for i in list_interfaces():
            self.interface.addItem(i.label, i.name)
        self.interface.setCurrentIndex(max(self.interface.findData(keep), 0))
        self.interface.blockSignals(False)
        self.changed.emit()

    def can_connect(self) -> bool:
        return self.interface.currentData() is not None

    def apply(self, settings: Settings) -> Optional[str]:
        name = self.interface.currentData()
        if not name:
            return "No network interface with an IPv4 address is available."
        settings.network.interface = name
        return None


class ConnectionDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connection")
        self.setMinimumWidth(520)
        self.settings = copy.deepcopy(settings)
        self.transport = QComboBox()
        for k, label in TRANSPORTS:
            self.transport.addItem(label, k)
        self.transport.setCurrentIndex(max(self.transport.findData(settings.transport), 0))
        self.pages = QStackedWidget()
        self.mesh, self.afsk, self.kiss = MeshPage(settings), AfskPage(settings), KissPage(settings)
        self.network = NetworkPage(settings)
        for p in (self.mesh, self.afsk, self.kiss, self.network):
            self.pages.addWidget(p)
        self.pages.setCurrentIndex(self.transport.currentIndex())
        self.transport.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.transport.currentIndexChanged.connect(self._page_changed)
        self.mesh.changed.connect(self._update_connect)
        self.network.changed.connect(self._update_connect)

        self.tun = QCheckBox("Also create a system network interface (needs administrator rights)")
        self.tun.setChecked(settings.system_interface)
        self.tun_note = tun_note = QLabel("CQta chat, ping and file transfer work without it and without elevated privileges. "
                          "Enable this only to route other IP traffic through the radio; you will be asked for "
                          "permission when connecting.")
        tun_note.setWordWrap(True)
        tun_note.setStyleSheet("color: gray")

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.connect_btn = buttons.addButton("Connect", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        top = QHBoxLayout()
        top.addWidget(QLabel("Radio"))
        top.addWidget(self.transport, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.pages)
        layout.addWidget(self.tun)
        layout.addWidget(tun_note)
        layout.addWidget(buttons)
        self._update_connect()

    def _update_connect(self) -> None:
        kind = self.transport.currentData()
        self.connect_btn.setEnabled({"meshtastic": self.mesh.can_connect, "network": self.network.can_connect}
                                    .get(kind, lambda: True)())
        self.tun.setVisible(kind != "network")
        self.tun_note.setVisible(kind != "network")

    def _page_changed(self) -> None:
        self.mesh.set_active(self.isVisible() and self.transport.currentData() == "meshtastic")
        self._update_connect()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.mesh.set_active(self.transport.currentData() == "meshtastic")

    def done(self, result: int) -> None:
        self.mesh.set_active(False)
        super().done(result)

    def _accept(self) -> None:
        kind = self.transport.currentData()
        page = {"meshtastic": self.mesh, "afsk": self.afsk, "kiss": self.kiss,
                "network": self.network}[kind]
        error = page.apply(self.settings)
        if error:
            dialogs.warning(self, "Connection", error)
            return
        self.settings.transport = kind
        self.settings.system_interface = self.tun.isChecked() and kind != "network"
        self.accept()
