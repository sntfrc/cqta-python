from __future__ import annotations

import copy
import threading
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..app.settings import Settings
from ..core.ipv4 import parse_address, random_ip
from ..transports import audio
from ..transports.mesh_links import MeshDevice, list_serial_ports, scan_ble

TRANSPORTS = [("meshtastic", "Meshtastic"), ("afsk", "AFSK sound modem"), ("kiss", "KISS TCP TNC")]
MESH_KINDS = [("serial", "USB serial"), ("ble", "Bluetooth LE"), ("tcp", "TCP / Wi-Fi")]


def _spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


class MeshPage(QWidget):
    scanned = Signal(object, str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        c = settings.mesh
        self.kind = QComboBox()
        for k, label in MESH_KINDS:
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(max(self.kind.findData(c.kind), 0))
        self.device = QComboBox()
        self.device.setEditable(True)
        self.scan = QPushButton("Scan")
        self.host = QLineEdit(c.address if c.kind == "tcp" else "")
        self.host.setPlaceholderText("meshtastic.local or 192.168.1.20")
        self.port = _spin(1, 65535, c.port)
        self._saved = {"serial": c.address if c.kind == "serial" else "", "ble": c.address if c.kind == "ble" else ""}
        self._devices: list[MeshDevice] = []

        dev_row = QHBoxLayout()
        dev_row.addWidget(self.device, 1)
        dev_row.addWidget(self.scan)
        self.dev_widget = QWidget()
        self.dev_widget.setLayout(dev_row)
        dev_row.setContentsMargins(0, 0, 0, 0)
        tcp_row = QHBoxLayout()
        tcp_row.setContentsMargins(0, 0, 0, 0)
        tcp_row.addWidget(self.host, 1)
        tcp_row.addWidget(QLabel("Port"))
        tcp_row.addWidget(self.port)
        self.tcp_widget = QWidget()
        self.tcp_widget.setLayout(tcp_row)

        self.note = QLabel()
        self.note.setWordWrap(True)
        form = QFormLayout(self)
        form.addRow("Connection", self.kind)
        self.dev_label = QLabel("Device")
        form.addRow(self.dev_label, self.dev_widget)
        self.tcp_label = QLabel("Host")
        form.addRow(self.tcp_label, self.tcp_widget)
        form.addRow(self.note)

        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.scan.clicked.connect(self.refresh)
        self.scanned.connect(self._scan_done)
        self._kind_changed()

    def _kind_changed(self) -> None:
        k = self.kind.currentData()
        self.dev_widget.setVisible(k != "tcp")
        self.dev_label.setVisible(k != "tcp")
        self.tcp_widget.setVisible(k == "tcp")
        self.tcp_label.setVisible(k == "tcp")
        self.scan.setText("Scan" if k == "ble" else "Refresh")
        self.note.setText({"ble": "Pair the node with this computer first if your system requires it.",
                           "serial": "", "tcp": "Meshtastic nodes serve their API on TCP port 4403."}[k])
        self.device.clear()
        self.device.setEditText(self._saved.get(k, ""))
        if k == "serial":
            self.refresh()

    def refresh(self) -> None:
        k = self.kind.currentData()
        if k == "serial":
            self._fill(list_serial_ports())
        elif k == "ble":
            self.scan.setEnabled(False)
            self.scan.setText("Scanning…")

            def work() -> None:
                try:
                    self.scanned.emit(scan_ble(), "")
                except Exception as e:
                    self.scanned.emit([], f"Bluetooth scan failed: {e}")

            threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, devices: list[MeshDevice], error: str) -> None:
        self.scan.setEnabled(True)
        self.scan.setText("Scan")
        if error:
            QMessageBox.warning(self, "Bluetooth", error)
        self._fill(devices)

    def _fill(self, devices: list[MeshDevice]) -> None:
        current = self.device.currentText() or self._saved.get(self.kind.currentData(), "")
        self._devices = devices
        self.device.clear()
        for d in devices:
            self.device.addItem(d.label, d.address)
        idx = self.device.findData(current)
        if idx >= 0:
            self.device.setCurrentIndex(idx)
        else:
            self.device.setEditText(current)

    def apply(self, settings: Settings) -> Optional[str]:
        k = self.kind.currentData()
        if k == "tcp":
            address = self.host.text().strip()
        else:
            idx = self.device.currentIndex()
            text = self.device.currentText()
            address = self.device.itemData(idx) if idx >= 0 and self.device.itemText(idx) == text else text.strip()
        if not address:
            return "Choose or enter a Meshtastic device."
        settings.mesh.kind, settings.mesh.address, settings.mesh.port = k, address, self.port.value()
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
        for p in (self.mesh, self.afsk, self.kiss):
            self.pages.addWidget(p)
        self.pages.setCurrentIndex(self.transport.currentIndex())
        self.transport.currentIndexChanged.connect(self.pages.setCurrentIndex)

        self.tun = QCheckBox("Also create a system network interface (needs administrator rights)")
        self.tun.setChecked(settings.system_interface)
        tun_note = QLabel("CQta chat, ping and file transfer work without it and without elevated privileges. "
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

    def _accept(self) -> None:
        kind = self.transport.currentData()
        page = {"meshtastic": self.mesh, "afsk": self.afsk, "kiss": self.kiss}[kind]
        error = page.apply(self.settings)
        if error:
            QMessageBox.warning(self, "Connection", error)
            return
        self.settings.transport = kind
        self.settings.system_interface = self.tun.isChecked()
        self.accept()
