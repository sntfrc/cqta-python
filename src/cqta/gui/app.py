from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_ID
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


def register_windows_identity() -> None:
    """Gives the AppUserModelID a display name and icon so toast notifications read "CQta" instead of the raw id."""
    import winreg

    icon_path = Path(os.environ.get("APPDATA", Path.home())) / "cqta" / "cqta.png"
    try:
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        make_icon().pixmap(256, 256).save(str(icon_path))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "CQta")
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path))
    except OSError:
        pass


def _quiet_qt_noise() -> None:
    """Qt's X11 backend prints 'Trying to disable X but Y is focused' while menus open and close; it is harmless."""
    def handler(mode, context, message):
        if "is focused" not in message:
            print(message, file=sys.stderr)

    qInstallMessageHandler(handler)


def run(argv: list[str]) -> int:
    # Same Qt-drawn menu bar on every platform, so check marks render consistently
    if sys.platform == "darwin":
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, True)
    _quiet_qt_noise()
    app = QApplication(argv[:1])
    if sys.platform == "win32":
        import ctypes

        # Own taskbar identity so Windows shows our icon instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        register_windows_identity()
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
    # Python only handles SIGINT between bytecodes, so wake the Qt loop regularly to let Ctrl+C through
    def interrupt(*_) -> None:
        app.closeAllWindows()  # also ends any modal dialog's nested loop
        QTimer.singleShot(0, app.quit)

    signal.signal(signal.SIGINT, interrupt)
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(200)
    window.prompt_first_run()
    def finish() -> None:
        ctrl.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        # Skip interpreter teardown: PySide destroys widgets after Qt is gone and segfaults when the app is quit
        # through the OS (Cmd+Q, Dock menu), which calls exit() directly
        os._exit(0)

    app.aboutToQuit.connect(finish)
    code = app.exec()
    finish()
    return code


if __name__ == "__main__":
    sys.exit(run(sys.argv))
