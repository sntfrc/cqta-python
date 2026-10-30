from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView)

from ..app.chat import ConvInfo, Line, Station
from . import timefmt
from .bridge import CoreBridge
from .transcript import TranscriptView


class BroadcastPanel(QWidget):
    """Upper panel: the QST broadcast feed plus the message entry."""

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.view = TranscriptView()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Broadcast a message to everyone on the channel…")
        self.send_btn = QPushButton("Send")
        self.cq_btn = QPushButton("Call CQ")
        self.clear_btn = QPushButton("Clear")
        row = QHBoxLayout()
        row.addWidget(self.entry, 1)
        for b in (self.send_btn, self.cq_btn, self.clear_btn):
            row.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.addWidget(QLabel("<b>Broadcast</b> (QST)"))
        layout.addWidget(self.view, 1)
        layout.addLayout(row)

        self.entry.returnPressed.connect(self.send)
        self.send_btn.clicked.connect(self.send)
        self.cq_btn.clicked.connect(lambda: bridge.post(bridge.ctrl.chat.call_cq))
        self.clear_btn.clicked.connect(lambda: bridge.post(bridge.ctrl.chat.clear_broadcast))
        bridge.line.connect(self._line)
        bridge.broadcast_cleared.connect(self.view.clear)

    def _line(self, peer: Optional[str], line: Line) -> None:
        if peer is None:
            self.view.append_line(line)

    def send(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        chat = self.bridge.ctrl.chat
        if self.bridge.query(lambda: chat.send_broadcast(text)):
            self.entry.clear()


class StationsPanel(QWidget):
    """Last heard stations. Double-click opens a QSO."""

    open_qso = Signal(str)
    ping = Signal(str)
    send_file = Signal(str)

    HEADERS = ("Station", "Heard (UTC)", "IP")

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_qso.emit(self._call(r)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(QLabel("<b>Last heard</b>"))
        layout.addWidget(self.table, 1)
        self._stations: list[Station] = []
        bridge.stations.connect(self.set_stations)
        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_ages)
        self._timer.start(15000)

    def _call(self, row: int) -> str:
        return self.table.item(row, 0).text()

    @staticmethod
    def _age(t: float) -> str:
        minutes = max(int(time.time() - t), 0) // 60
        if minutes < 1:
            return "<1 min ago"
        if minutes < 60:
            return f"{minutes} min ago"
        return f"{minutes // 60} h ago"

    @classmethod
    def _heard(cls, t: float) -> str:
        return f"{timefmt.hm(t)} ({cls._age(t)})"

    def set_stations(self, stations: list[Station]) -> None:
        self._stations = stations
        selected = self._call(self.table.currentRow()) if self.table.currentRow() >= 0 else None
        self.table.setRowCount(len(stations))
        for i, s in enumerate(stations):
            self.table.setItem(i, 0, QTableWidgetItem(s.callsign))
            self.table.setItem(i, 1, QTableWidgetItem(self._heard(s.last_heard)))
            self.table.setItem(i, 2, QTableWidgetItem(s.ip or ""))
            if s.callsign == selected:
                self.table.selectRow(i)

    def _refresh_ages(self) -> None:
        for i, s in enumerate(self._stations):
            item = self.table.item(i, 1)
            if item:
                item.setText(self._heard(s.last_heard))

    def _menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        call = self._call(row)
        menu = QMenu(self)
        menu.addAction(f"Open QSO with {call}", lambda: self.open_qso.emit(call))
        menu.addAction("Ping", lambda: self.ping.emit(call))
        menu.addAction("Send file…", lambda: self.send_file.emit(call))
        menu.exec(self.table.viewport().mapToGlobal(pos))


class QsoTab(QWidget):
    def __init__(self, bridge: CoreBridge, peer: str) -> None:
        super().__init__()
        self.bridge = bridge
        self.peer = peer
        self.view = TranscriptView()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(f"Message to {peer}…")
        self.send_btn = QPushButton("Send")
        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.missing_btn = QPushButton("Request missing")
        self.restart_btn = QPushButton("Restart")
        self.close_btn = QPushButton("Close (QRT)")
        row = QHBoxLayout()
        row.addWidget(self.entry, 1)
        row.addWidget(self.send_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        for b in (self.missing_btn, self.restart_btn, self.close_btn):
            bottom.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.view, 1)
        layout.addLayout(row)
        layout.addLayout(bottom)

        self.entry.returnPressed.connect(self.send)
        self.send_btn.clicked.connect(self.send)
        chat = bridge.ctrl.chat
        self.missing_btn.clicked.connect(lambda: bridge.post(lambda: chat.request_missing(peer)))
        self.restart_btn.clicked.connect(self._restart)
        self.close_btn.clicked.connect(lambda: bridge.post(lambda: chat.close_qso(peer)))
        self.view.cq_clicked.connect(lambda _c: None)
        self.set_info(None)

    def send(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        chat = self.bridge.ctrl.chat
        if self.bridge.query(lambda: chat.send_text(self.peer, text)):
            self.entry.clear()

    def _restart(self) -> None:
        text = self.entry.text().strip()
        chat = self.bridge.ctrl.chat
        if self.bridge.query(lambda: chat.restart_qso(self.peer, text)):
            self.entry.clear()

    def set_info(self, info: Optional[ConvInfo]) -> None:
        if info is None:
            return
        self.status.setText(info.status)
        self.missing_btn.setEnabled(info.has_missing)
        self.entry.setEnabled(True)
        self.send_btn.setEnabled(not info.closed)
        self.close_btn.setText("Forget" if info.closed else "Close (QRT)")
        self.restart_btn.setText("Reopen" if info.closed else "Restart")
        if info.closed:
            self.entry.setPlaceholderText(f"{self.peer} closed the QSO. Reopen to start over.")
        else:
            self.entry.setPlaceholderText(f"Message to {self.peer}…")
