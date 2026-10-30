from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..app.controller import NodeRow, PingEntry
from . import timefmt
from .bridge import CoreBridge


class SortItem(QTableWidgetItem):
    """Table item that sorts by an explicit key instead of its text."""

    def __init__(self, text: str, key=None) -> None:
        super().__init__(text)
        self.key = text.lower() if key is None else key

    def __lt__(self, other) -> bool:
        if isinstance(other, SortItem):
            return self.key < other.key
        return super().__lt__(other)


def ip_key(ip: str) -> tuple:
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999,)


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
        self.nodes.setHorizontalHeaderLabels(["Name", "Short", "IP", "Heard (UTC)"])
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["Target", "Seq", "Result", "Time (UTC)"])
        for t in (self.nodes, self.results):
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.verticalHeader().hide()
            t.horizontalHeader().setStretchLastSection(True)
        self.nodes.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.nodes.setSortingEnabled(True)
        self.nodes.horizontalHeader().setSortIndicator(3, Qt.DescendingOrder)
        self.nodes.cellDoubleClicked.connect(lambda r, _c: self._ping_row(r))

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(QLabel("<b>Results</b>"))
        layout.addWidget(self.results, 1)
        self.nodes_title = QLabel()
        layout.addWidget(self.nodes_title)
        layout.addWidget(self.nodes, 1)

        self._rows: dict[int, int] = {}
        self.set_nodes([], "")
        self.ping_btn.clicked.connect(self._ping)
        self.target.returnPressed.connect(self._ping)
        bridge.nodes.connect(lambda kind, rows: self.set_nodes(rows, kind))
        bridge.ping.connect(self.on_ping)

    def ping_target(self, target: str) -> None:
        self.target.setText(target)
        self._ping()

    def _ping(self) -> None:
        target = self.target.text().strip()
        if target:
            self.bridge.post(lambda: self.bridge.ctrl.ping(target))

    def _ping_row(self, row: int) -> None:
        self.target.setText(self.nodes.item(row, 2).text() or self.nodes.item(row, 0).text())
        self._ping()

    def set_nodes(self, rows: list[NodeRow], kind: str = "meshtastic") -> None:
        title = {"meshtastic": "Meshtastic nodes", "": "Nodes"}.get(kind, "Heard stations")
        self.nodes_title.setText(f"<b>{title}</b>" + ("" if kind == "network" else " (double-click to ping)"))
        self.nodes.setColumnHidden(1, kind != "meshtastic")  # short names exist only on Meshtastic
        self.nodes.setHorizontalHeaderLabels(["Name" if kind == "meshtastic" else "Callsign" if kind else "Name",
                                              "Short", "IP", "Heard (UTC)"])
        t = self.nodes
        t.setSortingEnabled(False)  # rows would move while being filled
        t.setRowCount(len(rows))
        for i, n in enumerate(rows):
            t.setItem(i, 0, SortItem(n.long_name or f"!{n.num:08x}"))
            t.setItem(i, 1, SortItem(n.short_name))
            t.setItem(i, 2, SortItem(n.ip, ip_key(n.ip)))
            heard = timefmt.hm(n.last_heard) if n.last_heard else ""
            t.setItem(i, 3, SortItem(heard, n.last_heard or 0))
        t.setSortingEnabled(True)

    def on_ping(self, p: PingEntry) -> None:
        row = self._rows.get(p.key)
        if row is None:
            row = self.results.rowCount()
            self.results.insertRow(row)
            self._rows[p.key] = row
            self.results.setItem(row, 0, QTableWidgetItem(p.target))
            self.results.setItem(row, 1, QTableWidgetItem(str(p.seq)))
            self.results.setItem(row, 3, QTableWidgetItem(timefmt.hms()))
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
        self.text.appendPlainText(f"{timefmt.hms()}  {message}")
