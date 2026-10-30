from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..app.controller import Controller
from ..app.settings import Settings
from .bridge import CoreBridge
from .main_window import MainWindow


ICON_PATH = Path(__file__).resolve().parent.parent / "cqta.png"


def make_icon() -> QIcon:
    return QIcon(str(ICON_PATH))


# Qt's macOS style draws cramped, unpadded text fields and spin boxes on current macOS releases; Fusion with a
# little padding is consistent and readable. Windows and Linux keep their native styles.
FIELD_STYLE = """
QLineEdit { padding: 3px 6px; min-height: 20px; }
QSpinBox, QDoubleSpinBox { padding: 2px 4px; min-height: 22px; }
QComboBox { padding: 3px 8px; min-height: 20px; }
QPushButton { padding: 3px 12px; min-height: 20px; }
"""


def apply_style(app: QApplication) -> None:
    if sys.platform == "darwin":
        app.setStyle("Fusion")
        app.setStyleSheet(FIELD_STYLE)


def run(argv: list[str]) -> int:
    # The native macOS menu bar does not draw check marks on current macOS releases; Qt-drawn menus always do
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    app = QApplication(argv[:1])
    if sys.platform == "win32":
        import ctypes

        # Own taskbar identity so Windows shows our icon instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("org.cqta.app")
    apply_style(app)
    app.setApplicationName("CQta")
    app.setDesktopFileName("cqta")
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
