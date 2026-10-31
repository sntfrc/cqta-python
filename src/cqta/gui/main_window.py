from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon
from PySide6.QtWidgets import (QApplication, QInputDialog, QLabel, QMainWindow, QProgressBar, QSplitter,
                               QTabWidget, QWidget)

from ..app.chat import ConvInfo
from ..core.ipv4 import bytes_to_ip
from ..core.protocol import DEFAULT_CALLSIGN
from ..transports.base import LinkState
from . import dialogs
from .bridge import CoreBridge
from .connection_dialog import ConnectionDialog
from .files_window import FilesWindow
from .notifier import Notifier
from .nodes_window import LogWindow, NodesWindow
from .panels import BroadcastPanel, QsoTab, StationsPanel

NOTIFY_LABELS = [("direct", "Direct messages"), ("broadcast", "Broadcast messages"), ("mention", "Mentions of my callsign"),
                 ("cq", "CQ calls"), ("file_start", "Incoming file started"), ("file", "File received")]


class MainWindow(QMainWindow):
    def __init__(self, bridge: CoreBridge, icon: Optional[QIcon] = None) -> None:
        super().__init__()
        self.bridge = bridge
        self.ctrl = bridge.ctrl
        self.setWindowTitle("CQta")
        self._mode = self.ctrl.settings
        self._call = self.ctrl.settings.callsign
        self._disconnected = True
        self.resize(1000, 760)
        if icon:
            self.setWindowIcon(icon)

        self.broadcast = BroadcastPanel(bridge)
        self.stations = StationsPanel(bridge)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.empty = QLabel("Double-click a station or a CQ call to open a QSO.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet("color: gray")
        self.tabs.setCornerWidget(None)
        lower = QSplitter(Qt.Horizontal)
        lower.addWidget(self.stations)
        self.right = QSplitter(Qt.Vertical)
        self.right.addWidget(self.empty)
        self.right.addWidget(self.tabs)
        self.tabs.hide()
        lower.addWidget(self.right)
        lower.setStretchFactor(0, 0)
        lower.setStretchFactor(1, 1)
        lower.setSizes([380, 620])
        main = QSplitter(Qt.Vertical)
        main.addWidget(self.broadcast)
        main.addWidget(lower)
        main.setSizes([300, 460])
        self.setCentralWidget(main)

        self.qsos: dict[str, QsoTab] = {}
        self.infos: dict[str, ConvInfo] = {}
        self.focus_peer: Optional[str] = None
        self.files_window = FilesWindow(bridge)
        self.nodes_window = NodesWindow(bridge)
        self.log_window = LogWindow(bridge)
        self.notifier = Notifier(icon or QIcon(), self)
        self.notifier.clicked.connect(self._notification_clicked)

        self._build_menus()
        self._build_status()
        self._wire()
        self._load_snapshot()

    # Construction

    def _build_menus(self) -> None:
        s = self.ctrl.settings
        m = self.menuBar().addMenu("&Connection")
        self.connect_action = m.addAction("Connect…", self.connect_dialog)
        self.disconnect_action = m.addAction("Disconnect", lambda: self.bridge.post(self.ctrl.disconnect))
        self.disconnect_action.setEnabled(False)
        m.addSeparator()
        self.tun_action = self._check(m, "System network interface (needs admin)", s.system_interface, self._tun_toggled)
        self.tun_action.setVisible(s.transport != "network")
        self.duty_action = m.addAction("Override duty cycle")
        self.duty_action.setCheckable(True)
        self.duty_action.setVisible(False)
        self.duty_action.triggered.connect(self._duty_toggled)
        self._duty_override = False
        m.addSeparator()
        m.addAction("Quit", self.close).setMenuRole(QAction.QuitRole)

        m = self.menuBar().addMenu("&Station")
        m.addAction("Callsign…", self.edit_callsign)
        m.addAction("Beacon…", self.edit_beacon)
        self.monitor_action = self._check(m, "Show all traffic (monitor)", s.monitor, self._monitor_toggled)
        self.sound_action = self._check(m, "Notification sound", s.sound, self._sound_toggled)
        nm = m.addMenu("Notifications")
        self.notify_actions: dict[str, QAction] = {}
        for key, label in NOTIFY_LABELS:
            a = self._check(nm, label, bool(s.notify.get(key, key in ("direct", "file_start", "file"))),
                            lambda on, k=key: self._notify_toggled(k, on))
            self.notify_actions[key] = a
        m.addSeparator()
        m.addAction("Clear broadcast", lambda: self.bridge.post(self.ctrl.chat.clear_broadcast))
        m.addAction("Clear last heard", lambda: self.bridge.post(self.ctrl.chat.clear_stations))
        m.addAction("Clear all QSOs", self._clear_qsos)

        m = self.menuBar().addMenu("&Tools")
        m.addAction("Files", lambda: self._show(self.files_window))
        m.addAction("Nodes and Ping", lambda: self._show(self.nodes_window))
        m.addAction("Log", lambda: self._show(self.log_window))
        m = self.menuBar().addMenu("&Help")
        m.addAction("About CQta", self.about).setMenuRole(QAction.AboutRole)

    @staticmethod
    def _check(menu, text: str, checked: bool, slot) -> QAction:
        a = menu.addAction(text)
        a.setCheckable(True)
        a.setChecked(checked)
        a.toggled.connect(slot)
        return a

    def _build_status(self) -> None:
        sb = self.statusBar()
        self.link_label = QLabel("Disconnected")
        self.ip_label = QLabel("")
        self.stats_label = QLabel("")
        self.tun_label = QLabel("")
        self.beacon_label = QLabel("")
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setFixedWidth(90)
        self.level.setTextVisible(False)
        self.level.setToolTip("Receive audio level")
        self.level.hide()
        self.message = QLabel("")
        sb.addWidget(self.link_label)
        sb.addWidget(self.ip_label)
        sb.addWidget(self.message, 1)
        for w in (self.level, self.stats_label, self.tun_label, self.beacon_label):
            sb.addPermanentWidget(w)

    def _wire(self) -> None:
        b = self.bridge
        b.link.connect(self._link)
        b.info.connect(self._info)
        b.stats.connect(self._stats)
        b.tun.connect(self._tun)
        b.beacon.connect(self._beacon)
        b.status.connect(self.message.setText)
        b.notice.connect(self.message.setText)
        b.duty.connect(self._duty)
        b.duty_failed.connect(lambda m: dialogs.warning(self, "Duty cycle", m))
        b.conversation.connect(self._conversation)
        b.conversation_removed.connect(self._removed)
        b.conversation_reset.connect(self._reset)
        b.line.connect(self._line)
        b.notification.connect(self._notification)
        b.callsign.connect(self._callsign)
        b.file_received.connect(lambda name, _n: self.files_window.reveal_file(name))
        self.stations.open_qso.connect(self.open_qso)
        self.stations.ping.connect(self.ping)
        self.stations.send_file.connect(self.send_file)
        self.broadcast.view.cq_clicked.connect(self.open_qso)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._active_changed)
        QApplication.instance().applicationStateChanged.connect(lambda _s: self._active_changed())

    def _load_snapshot(self) -> None:
        chat = self.ctrl.chat
        broadcast, convs, stations, lines = self.bridge.query(chat.snapshot)
        self.broadcast.view.set_lines(broadcast)
        self.stations.set_stations(stations)
        for info in convs:
            self._conversation(info)
            self.qsos[info.peer].view.set_lines(lines.get(info.peer, []))
        self.setWindowTitle(self._title(self.ctrl.settings.callsign))

    def _title(self, callsign: str) -> str:
        self._call = callsign
        state = "Disconnected" if self._disconnected else self._transport_label(self._mode)
        return f"CQta - {callsign} ({state})"

    @staticmethod
    def _transport_label(s) -> str:
        if s.transport == "meshtastic":
            return f"Meshtastic - {s.mesh.address}" if s.mesh.address else "Meshtastic"
        if s.transport == "kiss":
            return f"KISS TNC - {s.kiss.host}:{s.kiss.tcp_port}"
        if s.transport == "network":
            return f"Network interface - {s.network.interface}" if s.network.interface else "Network interface"
        return "AFSK modem"

    def _refresh_mode(self, settings) -> None:
        self._mode = settings
        self.tun_action.setVisible(settings.transport != "network")
        self.files_window.set_available(settings.transport != "network")
        self.setWindowTitle(self._title(self._call))

    # Helpers

    def _show(self, w: QWidget) -> None:
        w.show()
        w.raise_()
        w.activateWindow()

    def open_qso(self, peer: str) -> None:
        chat = self.ctrl.chat
        self.focus_peer = peer.strip().upper()
        opened = self.bridge.query(lambda: chat.open_qso(peer))
        if not opened:
            self.focus_peer = None
            dialogs.warning(self, "Open QSO", f"'{peer}' is not a valid callsign to talk to.")
            return
        peer = opened
        self.focus_peer = peer
        self._focus_tab(peer)

    def _focus_tab(self, peer: str) -> None:
        tab = self.qsos.get(peer)
        if tab is not None:
            self.tabs.setCurrentWidget(tab)
            self.focus_peer = None
            tab.entry.setFocus()

    def ping(self, target: str) -> None:
        self._show(self.nodes_window)
        self.nodes_window.ping_target(target)

    def send_file(self, target: str) -> None:
        self._show(self.files_window)
        self.files_window.set_target(target)

    # Conversations

    def _conversation(self, info: ConvInfo) -> None:
        self.infos[info.peer] = info
        tab = self.qsos.get(info.peer)
        if tab is None:
            tab = QsoTab(self.bridge, info.peer)
            tab.view.cq_clicked.connect(self.open_qso)
            self.qsos[info.peer] = tab
            self.tabs.addTab(tab, info.peer)
            self.empty.hide()
            self.tabs.show()
            tab.set_info(info)
            if self.focus_peer == info.peer:
                self._focus_tab(info.peer)
        tab.set_info(info)
        self._badge(info.peer)

    def _badge(self, peer: str) -> None:
        tab, info = self.qsos.get(peer), self.infos.get(peer)
        if tab is None or info is None:
            return
        unread = info.unread if self.tabs.currentWidget() is not tab or not self._is_active() else 0
        text = f"{peer} ({unread})" if unread else peer
        if info.closed:
            text += " ✕"
        self.tabs.setTabText(self.tabs.indexOf(tab), text)

    def _is_active(self) -> bool:
        return self.isActiveWindow()

    def _removed(self, peer: str) -> None:
        self.infos.pop(peer, None)
        tab = self.qsos.pop(peer, None)
        if tab is not None:
            self.tabs.removeTab(self.tabs.indexOf(tab))
            tab.deleteLater()
        if not self.qsos:
            self.tabs.hide()
            self.empty.show()

    def _reset(self, peer: str) -> None:
        tab = self.qsos.get(peer)
        if tab is not None:
            tab.view.clear()

    def _line(self, peer, line) -> None:
        tab = self.qsos.get(peer) if peer else None
        if tab is not None:
            tab.view.append_line(line)

    def _close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if isinstance(tab, QsoTab):
            peer = tab.peer
            info = self.infos.get(peer)
            if info is not None and not info.closed:
                if not dialogs.question(self, "Close QSO", f"Send QRT to {peer} and close this QSO?"):
                    return
            self.bridge.post(lambda: self.ctrl.chat.close_qso(peer))
            if info is not None and info.closed:
                return

    def _active_changed(self, *_: object) -> None:
        tab = self.tabs.currentWidget()
        peer = tab.peer if isinstance(tab, QsoTab) and self._is_active() else None
        self.bridge.post(lambda: self.ctrl.chat.set_active_peer(peer))
        for p in list(self.qsos):
            self._badge(p)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type().name == "ActivationChange":
            self._active_changed()

    # Status

    def _link(self, state: LinkState, detail: str) -> None:
        text = CoreBridgeText.state(state)
        self._disconnected = state is LinkState.STOPPED
        self.setWindowTitle(self._title(self._call))
        self.link_label.setText(f"{text}: {detail}" if detail and state is not LinkState.READY else text)
        self.connect_action.setEnabled(state is LinkState.STOPPED)  # disconnect first to change the connection
        self.disconnect_action.setEnabled(state is not LinkState.STOPPED)
        if state is not LinkState.READY:
            self.duty_action.setVisible(False)
        if state is LinkState.STOPPED:
            self.ip_label.setText("")
            self.level.hide()

    def _info(self, info) -> None:
        self.ip_label.setText(bytes_to_ip(info.my_ip) if info else "")

    def _stats(self, st: dict) -> None:
        self.stats_label.setText(f"TX {st.get('tx', 0)}  RX {st.get('rx', 0)}")
        if "level" in st:
            self.level.show()
            self.level.setValue(int(min(1.0, st["level"]) * 100))
            parts = [p for p, k in (("TX", "transmitting"), ("busy", "busy")) if st.get(k)]
            self.level.setToolTip("Receive audio level" + (f" ({', '.join(parts)})" if parts else ""))
        else:
            self.level.hide()

    def _tun(self, state: str, detail: str) -> None:
        self.tun_label.setText({"off": "", "starting": "Interface: starting…", "on": f"Interface: {detail}",
                                "error": f"Interface error: {detail}"}.get(state, ""))
        self.tun_action.blockSignals(True)
        self.tun_action.setChecked(state in ("starting", "on"))
        self.tun_action.blockSignals(False)
        if state == "error":
            dialogs.warning(self, "System network interface", detail)

    def _beacon(self, countdown: int, tx: bool) -> None:
        self.beacon_label.setText("Beacon: sending" if tx else f"Beacon in {countdown}s" if countdown > 0 else "")

    def _duty(self, limited: bool, override: bool, pending: bool) -> None:
        """Mirror the node's real state: only offered while connected to a duty-cycle limited Meshtastic node."""
        self._duty_override = override
        a = self.duty_action
        a.setVisible(limited)
        a.setEnabled(not pending)
        a.setChecked(override)
        a.setText("Override duty cycle (waiting for node…)" if pending else "Override duty cycle")

    def _callsign(self, call: str) -> None:
        self.setWindowTitle(self._title(call))

    # Notifications

    def _notification(self, kind: str, title: str, body: str, peer) -> None:
        self.notifier.show(kind, title, body, peer)
        if self.ctrl.settings.sound:
            QApplication.beep()

    def _notification_clicked(self, kind: str, target) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if not kind:
            return
        if kind == "direct" and target:
            self.open_qso(str(target))
        elif kind in ("file", "file_start"):
            self._show(self.files_window)
            if target:
                self.files_window.reveal_file(str(target))

    # Actions

    def connect_dialog(self) -> None:
        dlg = ConnectionDialog(self.ctrl.settings, self)
        if dlg.exec():
            s = dlg.settings
            self._refresh_mode(s)
            self.bridge.post(lambda: self._apply_and_connect(s))

    def _apply_and_connect(self, s) -> None:
        # Settings objects are replaced wholesale; keep live chat values rather than the dialog copy's
        s.callsign, s.monitor, s.notify = self.ctrl.chat.callsign, self.ctrl.chat.monitor, dict(self.ctrl.chat.notify)
        s.beacon_enabled, s.beacon_interval = self.ctrl.chat.beacon_enabled, self.ctrl.chat.beacon_interval
        s.sound, s.tftp_server, s.download_dir, s.file_target = (self.ctrl.settings.sound, self.ctrl.settings.tftp_server,
                                                                self.ctrl.settings.download_dir, self.ctrl.settings.file_target)
        self.ctrl.settings = s
        s.save()
        self.ctrl.connect()

    def _tun_toggled(self, on: bool) -> None:
        self.ctrl.settings.system_interface = on
        self.bridge.post(lambda: (self.ctrl.set_system_interface(on), self.ctrl.sync_settings()))

    def _duty_toggled(self, on: bool) -> None:
        # Qt flips the check immediately; put it back until the node confirms the change
        self.duty_action.setChecked(self._duty_override)
        self.bridge.post(lambda: self.ctrl.set_duty_cycle_override(on))

    def _monitor_toggled(self, on: bool) -> None:
        def apply() -> None:
            self.ctrl.chat.monitor = on
            self.ctrl.sync_settings()

        self.bridge.post(apply)

    def _sound_toggled(self, on: bool) -> None:
        self.ctrl.settings.sound = on
        self.bridge.post(self.ctrl.sync_settings)

    def _notify_toggled(self, key: str, on: bool) -> None:
        def apply() -> None:
            self.ctrl.chat.notify[key] = on
            self.ctrl.sync_settings()

        self.bridge.post(apply)

    def edit_callsign(self, first_run: bool = False) -> None:
        current = self.bridge.query(lambda: self.ctrl.chat.callsign)
        while True:
            text, ok = QInputDialog.getText(
                self, "Callsign", "Your callsign (letters, digits, / and -). N0CALL listens without transmitting:",
                text=current if current != DEFAULT_CALLSIGN else "")
            if not ok:
                return
            error = self.bridge.query(lambda: self._set_callsign(text))
            if not error:
                return
            dialogs.warning(self, "Callsign", error)
            current = text

    def _set_callsign(self, text: str) -> Optional[str]:
        error = self.ctrl.chat.set_callsign(text)
        if not error:
            self.ctrl.sync_settings()
        return error

    def edit_beacon(self) -> None:
        chat = self.ctrl.chat
        enabled, interval = self.bridge.query(lambda: (chat.beacon_enabled, chat.beacon_interval))
        value, ok = QInputDialog.getInt(self, "Beacon", "Beacon interval in seconds (0 disables):",
                                        interval if enabled else 0, 0, 3600)
        if ok:
            def apply() -> None:
                chat.set_beacon(value > 0, value if value > 0 else interval)
                self.ctrl.sync_settings()

            self.bridge.post(apply)

    def _clear_qsos(self) -> None:
        if dialogs.question(self, "Clear QSOs", "Forget all conversations?"):
            self.bridge.post(self.ctrl.chat.clear_qsos)

    def about(self) -> None:
        from .. import __version__

        dialogs.about(self, "About CQta",
                      f'CQta {__version__} - by Federico Santandrea IU4AAJ<br>'
                      '<a href="https://santandrea.net/">https://santandrea.net/</a>')

    def prompt_first_run(self) -> None:
        if self.bridge.query(lambda: self.ctrl.chat.callsign) == DEFAULT_CALLSIGN:
            self.edit_callsign(first_run=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        for w in (self.files_window, self.nodes_window, self.log_window):
            w.close()
        event.accept()
        QApplication.quit()


class CoreBridgeText:
    @staticmethod
    def state(state: LinkState) -> str:
        return CoreBridge.state_text(state)
