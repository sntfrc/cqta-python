from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..app.controller import NodeRow, PingEntry
from .bridge import CoreBridge


class NodesWindow(QWidget):
    """Meshtastic nodes and ICMP ping."""

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.setWindowTitle("CQta Nodes and Ping")
        self.resize(680, 520)
        self.bridge = bridge
        self.target = QLineEdit()
        self.target.setPlaceholderText("Callsign or IPv4 address")
        self.ping_btn = QPushButton("Ping")
        row = QHBoxLayout()
        row.addWidget(QLabel("Ping:"))
        row.addWidget(self.target, 1)
        row.addWidget(self.ping_btn)

        self.nodes = QTableWidget(0, 4)
        self.nodes.setHorizontalHeaderLabels(["Name", "Short", "IP", "Heard"])
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["Target", "Seq", "Result", "Time"])
        for t in (self.nodes, self.results):
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.verticalHeader().hide()
            t.horizontalHeader().setStretchLastSection(True)
        self.nodes.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.nodes.cellDoubleClicked.connect(lambda r, _c: self._ping_row(r))

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(QLabel("<b>Results</b>"))
        layout.addWidget(self.results, 1)
        layout.addWidget(QLabel("<b>Meshtastic nodes</b> (double-click to ping)"))
        layout.addWidget(self.nodes, 1)

        self._rows: dict[int, int] = {}
        self.ping_btn.clicked.connect(self._ping)
        self.target.returnPressed.connect(self._ping)
        bridge.nodes.connect(self.set_nodes)
        bridge.ping.connect(self.on_ping)

    def ping_target(self, target: str) -> None:
        self.target.setText(target)
        self._ping()

    def _ping(self) -> None:
        target = self.target.text().strip()
        if target:
            self.bridge.post(lambda: self.bridge.ctrl.ping(target))

    def _ping_row(self, row: int) -> None:
        self.target.setText(self.nodes.item(row, 2).text())
        self._ping()

    def set_nodes(self, rows: list[NodeRow]) -> None:
        t = self.nodes
        t.setRowCount(len(rows))
        for i, n in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(n.long_name or f"!{n.num:08x}"))
            t.setItem(i, 1, QTableWidgetItem(n.short_name))
            t.setItem(i, 2, QTableWidgetItem(n.ip))
            heard = time.strftime("%H:%M:%S", time.localtime(n.last_heard)) if n.last_heard else ""
            t.setItem(i, 3, QTableWidgetItem(heard))

    def on_ping(self, p: PingEntry) -> None:
        row = self._rows.get(p.key)
        if row is None:
            row = self.results.rowCount()
            self.results.insertRow(row)
            self._rows[p.key] = row
            self.results.setItem(row, 0, QTableWidgetItem(p.target))
            self.results.setItem(row, 1, QTableWidgetItem(str(p.seq)))
            self.results.setItem(row, 3, QTableWidgetItem(time.strftime("%H:%M:%S")))
            self.results.scrollToBottom()
        text = {"pending": "Waiting…", "timeout": "No reply", "failed": p.detail or "Failed",
                "reply": f"Reply in {p.rtt_ms:.0f} ms" if p.rtt_ms is not None else "Reply"}[p.state]
        self.results.setItem(row, 2, QTableWidgetItem(text))


class LogWindow(QWidget):
    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.setWindowTitle("CQta Log")
        self.resize(640, 400)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(2000)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.text.clear)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text, 1)
        layout.addWidget(clear, 0, Qt.AlignRight)
        bridge.log.connect(self.add)
        bridge.status.connect(self.add)

    def add(self, message: str) -> None:
        self.text.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")
