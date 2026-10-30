"""Desktop notifications and the macOS menu bar icon.

macOS keeps an outline (template) icon in the menu bar whose click raises the window, and delivers notifications
through the notification center when running as the packaged app (so they carry the CQta icon and clicking one opens
the app); from source they go through the menu bar icon. Windows and Linux briefly show a system tray message and
remove the icon again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QSystemTrayIcon

TRAY_GLYPH = Path(__file__).resolve().parent.parent / "cqta-tray.png"

TRAY_LIFETIME_MS = 10_000


class Notifier(QObject):
    clicked = Signal(str, object)  # kind, peer or file name; kind is "" for a click on the menu bar icon

    def __init__(self, icon: QIcon, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._icon = icon
        self._tray: Optional[QSystemTrayIcon] = None
        self._pending: tuple[str, object] = ("", None)
        self._un = self._make_user_notifications() if sys.platform == "darwin" else None
        if sys.platform == "darwin" and QSystemTrayIcon.isSystemTrayAvailable():
            glyph = QIcon(str(TRAY_GLYPH))
            glyph.setIsMask(True)  # template image: the menu bar tints it for light/dark mode
            self._make_tray(glyph)
            self._tray.show()

    def show(self, kind: str, title: str, body: str, target: object = None) -> None:
        if self._un is not None:
            self._un(kind, title, body, target)
        else:
            self._tray_message(kind, title, body, target)

    def _make_tray(self, icon: QIcon) -> None:
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("CQta")
        self._tray.messageClicked.connect(lambda: self.clicked.emit(*self._pending))
        self._tray.activated.connect(lambda _r: self.clicked.emit("", None))
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._tray.hide)

    # Windows / Linux

    def _tray_message(self, kind: str, title: str, body: str, target: object) -> None:
        if sys.platform.startswith("linux") and self._dbus_notify(kind, title, body, target):
            return
        if not QSystemTrayIcon.isSystemTrayAvailable() and sys.platform != "linux":
            return
        if self._tray is None:
            self._make_tray(self._icon)
        self._pending = (kind, target)
        self._tray.show()
        self._tray.showMessage(title, body, self._icon, TRAY_LIFETIME_MS)
        if sys.platform != "darwin":
            self._hide_timer.start(TRAY_LIFETIME_MS + 1000)

    def _dbus_notify(self, kind: str, title: str, body: str, target: object) -> bool:
        """Sends the notification straight to the desktop's notification service, which (unlike the tray message
        path) makes GNOME show a pop-up banner instead of only filing it in the notification panel."""
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusMessage
        except ImportError:
            return False
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        if not getattr(self, "_dbus_hooked", False):
            self._dbus_hooked = True
            self._dbus_targets: dict[int, tuple[str, object]] = {}
            bus.connect("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                        "org.freedesktop.Notifications", "ActionInvoked", self, self._dbus_action)
        msg = QDBusMessage.createMethodCall("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                                            "org.freedesktop.Notifications", "Notify")
        hints = {"desktop-entry": "cqta", "category": "im.received"}
        msg.setArguments(["CQta", 0, "cqta", title, body, ["default", "Open"], hints, 10_000])
        reply = bus.call(msg)
        if reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments():
            return False
        self._dbus_targets[int(reply.arguments()[0])] = (kind, target)
        if len(self._dbus_targets) > 50:
            self._dbus_targets.pop(next(iter(self._dbus_targets)))
        return True

    @Slot("uint", str)
    def _dbus_action(self, nid: int, _action: str) -> None:
        if nid in self._dbus_targets:
            self.clicked.emit(*self._dbus_targets[nid])

    # macOS

    def _make_user_notifications(self):
        """Returns a sender backed by UNUserNotificationCenter, or None outside an app bundle or without PyObjC."""
        if not getattr(sys, "frozen", False):
            return None  # the notification center needs our own app bundle, not the Python interpreter's
        try:
            from Foundation import NSBundle, NSObject
            from UserNotifications import (UNMutableNotificationContent, UNNotificationRequest,
                                           UNUserNotificationCenter)
        except ImportError:
            return None
        if not NSBundle.mainBundle().bundleIdentifier():
            return None

        notifier = self
        banner_and_list = (1 << 4) | (1 << 3)  # sound is left to the app's own setting

        class Delegate(NSObject):
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, _c, _n, handler):
                handler(banner_and_list)

            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, _c, response, handler):
                info = response.notification().request().content().userInfo()
                notifier.clicked.emit(str(info.get("kind", "")), info.get("target") or None)
                handler()

        center = UNUserNotificationCenter.currentNotificationCenter()
        self._delegate = Delegate.alloc().init()
        center.setDelegate_(self._delegate)
        center.requestAuthorizationWithOptions_completionHandler_((1 << 1) | (1 << 2), lambda granted, error: None)
        counter = [0]

        def send(kind: str, title: str, body: str, target: object) -> None:
            content = UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            content.setUserInfo_({"kind": kind, "target": str(target) if target else ""})
            counter[0] += 1
            request = UNNotificationRequest.requestWithIdentifier_content_trigger_(f"cqta-{counter[0]}", content, None)
            center.addNotificationRequest_withCompletionHandler_(request, lambda error: None)

        return send
