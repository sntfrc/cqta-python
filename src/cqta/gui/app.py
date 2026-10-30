from __future__ import annotations

import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ..app.controller import Controller
from ..app.settings import Settings
from .bridge import CoreBridge
from .main_window import MainWindow


def make_pixmap(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#2a7de1"))
    grad.setColorAt(1, QColor("#14407a"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(size * 0.04, size * 0.04, size * 0.92, size * 0.92), size * 0.2, size * 0.2)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * 0.42))
    p.setFont(font)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "CQ")
    p.end()
    return pm


def make_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 32, 64, 128, 256, 512):
        icon.addPixmap(make_pixmap(s))
    return icon


def run(argv: list[str]) -> int:
    app = QApplication(argv[:1])
    app.setApplicationName("CQta")
    app.setOrganizationName("cqta")
    icon = make_icon()
    app.setWindowIcon(icon)
    ctrl = Controller(Settings.load())
    bridge = CoreBridge(ctrl)
    ctrl.start()
    window = MainWindow(bridge, icon)
    window.show()
    window.prompt_first_run()
    code = app.exec()
    ctrl.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv))
