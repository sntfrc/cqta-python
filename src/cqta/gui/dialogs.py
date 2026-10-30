"""Message boxes that avoid the native macOS NSAlert, which can throw while laying out its icon and abort the app."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget


def _box(parent: QWidget | None, icon: QMessageBox.Icon, title: str, text: str,
         buttons: QMessageBox.StandardButton) -> QMessageBox:
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    return box


def warning(parent: QWidget | None, title: str, text: str) -> None:
    _box(parent, QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok).exec()


def information(parent: QWidget | None, title: str, text: str) -> None:
    _box(parent, QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok).exec()


def question(parent: QWidget | None, title: str, text: str) -> bool:
    box = _box(parent, QMessageBox.Icon.Question, title, text,
               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def about(parent: QWidget | None, title: str, text: str) -> None:
    box = _box(parent, QMessageBox.Icon.NoIcon, title, text, QMessageBox.StandardButton.Ok)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)  # makes the link clickable
    box.exec()
