from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..app.files import ReceivedFile
from ..core.tftp import TransferInfo, TransferState
from . import dialogs, timefmt
from .bridge import CoreBridge


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def reveal(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


class FilesWindow(QWidget):
    """Separate window for TFTP uploads, the incoming-file server and received files."""

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self.setWindowTitle("CQta Files")
        self.resize(720, 560)
        self.bridge = bridge
        files = bridge.ctrl.files

        send = QGroupBox("Send a file")
        self.target = QLineEdit()
        self.target.setPlaceholderText("Callsign or IPv4 address")
        self.target.setText(bridge.ctrl.settings.file_target)
        self.contacts = QToolButton()
        self.contacts.setText("Known stations")
        self.contacts.setToolTip("Choose from known stations")
        self.contacts.setPopupMode(QToolButton.InstantPopup)
        self.contacts_menu = QMenu(self.contacts)
        self.contacts_menu.aboutToShow.connect(self._fill_contacts)
        self.contacts.setMenu(self.contacts_menu)
        self.pick = QPushButton("Choose file and send…")
        row = QHBoxLayout(send)
        row.addWidget(QLabel("To:"))
        row.addWidget(self.target, 1)
        row.addWidget(self.contacts)
        row.addWidget(self.pick)

        server = QGroupBox("Receiving")
        self.server = QCheckBox("Accept incoming files (TFTP server on UDP 69)")
        self.server.setChecked(bridge.ctrl.settings.tftp_server)
        self.folder_label = QLabel()
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        change = QPushButton("Change folder…")
        open_folder = QPushButton("Open folder")
        h = QHBoxLayout()
        h.addWidget(self.folder_label, 1)
        h.addWidget(change)
        h.addWidget(open_folder)
        v = QVBoxLayout(server)
        v.addWidget(self.server)
        v.addLayout(h)

        self.transfers = QTableWidget(0, 5)
        self.transfers.setHorizontalHeaderLabels(["Direction", "Peer", "File", "Progress", "Status"])
        self.received = QTableWidget(0, 3)
        self.received.setHorizontalHeaderLabels(["File", "Size", "Received (UTC)"])
        for t in (self.transfers, self.received):
            t.setEditTriggers(QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QAbstractItemView.SelectRows)
            t.verticalHeader().hide()
            t.horizontalHeader().setStretchLastSection(True)
            t.setContextMenuPolicy(Qt.CustomContextMenu)
        self.transfers.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.received.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        layout = QVBoxLayout(self)
        layout.addWidget(send)
        layout.addWidget(server)
        layout.addWidget(QLabel("<b>Transfers</b>"))
        layout.addWidget(self.transfers, 1)
        layout.addWidget(QLabel("<b>Received files</b> (double-click to open)"))
        layout.addWidget(self.received, 2)
        self._files: list[ReceivedFile] = []
        self._transfers: list[TransferInfo] = []

        self.pick.clicked.connect(self._send)
        self.target.returnPressed.connect(self._send)
        self.server.toggled.connect(self._server_toggled)
        change.clicked.connect(self._change_folder)
        open_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(files.folder))))
        self.received.cellDoubleClicked.connect(lambda r, _c: self._open(r))
        self.received.customContextMenuRequested.connect(self._received_menu)
        self.transfers.customContextMenuRequested.connect(self._transfer_menu)
        bridge.transfers.connect(self.set_transfers)
        bridge.files.connect(self.set_files)
        self._update_folder()

    def set_target(self, target: str) -> None:
        self.target.setText(target)

    def _fill_contacts(self) -> None:
        """Callsigns with a known IP, then tunnel nodes: the desktop version of the iOS address book menu."""
        self.contacts_menu.clear()
        contacts = self.bridge.query(self.bridge.ctrl.contacts)
        if not contacts:
            self.contacts_menu.addAction("No known contacts yet").setEnabled(False)
        for label, value in contacts:
            self.contacts_menu.addAction(label, lambda v=value: self.target.setText(v))

    def _update_folder(self) -> None:
        self.folder_label.setText(str(self.bridge.ctrl.files.folder))

    def _server_toggled(self, on: bool) -> None:
        files = self.bridge.ctrl.files
        self.bridge.ctrl.settings.tftp_server = on

        def apply() -> None:
            files.set_server(on)
            self.bridge.ctrl.sync_settings()

        self.bridge.post(apply)

    def _change_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder for received files",
                                                  str(self.bridge.ctrl.files.folder))
        if folder:
            self.bridge.ctrl.settings.download_dir = folder

            def apply() -> None:
                self.bridge.ctrl.files.set_folder(folder)
                self.bridge.ctrl.sync_settings()

            self.bridge.post(apply)
            self.folder_label.setText(folder)

    def _send(self) -> None:
        target = self.target.text().strip()
        if not target:
            dialogs.information(self, "Send a file", "Enter a callsign or IPv4 address first.")
            return
        self.bridge.ctrl.settings.file_target = target
        self.bridge.post(self.bridge.ctrl.settings.save)
        path, _ = QFileDialog.getOpenFileName(self, "Send file")
        if not path:
            return
        files = self.bridge.ctrl.files
        error = self.bridge.query(lambda: files.send_file(path, target))
        if error:
            dialogs.warning(self, "Send a file", error)

    def set_transfers(self, transfers: list[TransferInfo]) -> None:
        self._transfers = transfers
        t = self.transfers
        t.setRowCount(len(transfers))
        for i, x in enumerate(reversed(transfers)):
            t.setItem(i, 0, QTableWidgetItem("Sending" if x.is_upload else "Receiving"))
            t.setItem(i, 1, QTableWidgetItem(self.bridge.ctrl._display_name(x.peer)))
            t.setItem(i, 2, QTableWidgetItem(x.filename))
            bar = QProgressBar()
            bar.setRange(0, max(x.total or 0, 1) if x.total is not None else 0)
            bar.setValue(min(x.bytes, bar.maximum()) if x.total is not None else 0)
            bar.setFormat(f"{human_size(x.bytes)}" + (f" / {human_size(x.total)}" if x.total is not None else ""))
            t.setCellWidget(i, 3, bar)
            status = {TransferState.ACTIVE: "In progress", TransferState.DONE: "Done"}.get(x.state, x.error or "Failed")
            t.setItem(i, 4, QTableWidgetItem(status))
            t.item(i, 0).setData(Qt.UserRole, x.id)

    def _transfer_menu(self, pos) -> None:
        row = self.transfers.rowAt(pos.y())
        if row < 0:
            return
        tid = self.transfers.item(row, 0).data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Cancel transfer", lambda: self.bridge.post(lambda: self.bridge.ctrl.files.cancel(tid)))
        menu.exec(self.transfers.viewport().mapToGlobal(pos))

    def set_files(self, files: list[ReceivedFile]) -> None:
        self._files = files
        self.received.setRowCount(len(files))
        for i, f in enumerate(files):
            self.received.setItem(i, 0, QTableWidgetItem(f.name))
            self.received.setItem(i, 1, QTableWidgetItem(human_size(f.size)))
            self.received.setItem(i, 2, QTableWidgetItem(timefmt.date_hm(f.modified)))
        self._update_folder()

    def reveal_file(self, name: str) -> None:
        for i, f in enumerate(self._files):
            if f.name == name:
                self.received.selectRow(i)
                self.received.scrollToItem(self.received.item(i, 0))

    def _open(self, row: int) -> None:
        if 0 <= row < len(self._files):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._files[row].path))

    def _received_menu(self, pos) -> None:
        row = self.received.rowAt(pos.y())
        if not 0 <= row < len(self._files):
            return
        f = self._files[row]
        menu = QMenu(self)
        menu.addAction("Open", lambda: self._open(row))
        menu.addAction("Show in folder", lambda: reveal(f.path))
        menu.addAction("Delete…", lambda: self._delete(f))
        menu.exec(self.received.viewport().mapToGlobal(pos))

    def _delete(self, f: ReceivedFile) -> None:
        if dialogs.question(self, "Delete file", f"Delete {f.name}?"):
            self.bridge.post(lambda: self.bridge.ctrl.files.delete(f.path))
