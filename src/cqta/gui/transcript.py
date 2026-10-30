from __future__ import annotations

import html
import time

from PySide6.QtCore import Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser

from ..app.chat import Line

COLORS = {"me": "#4a90d9", "peer": "#2fa84f", "system": "#8a8a8a"}
MAX_LINES = 1000


class TranscriptView(QTextBrowser):
    """Chat log. CQ calls are rendered as links; clicking one emits `cq_clicked(callsign)`."""

    cq_clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.document().setMaximumBlockCount(MAX_LINES)
        self.anchorClicked.connect(self._clicked)

    def _clicked(self, url) -> None:
        s = url.toString()
        if s.startswith("cq:"):
            self.cq_clicked.emit(s[3:])

    def append_line(self, line: Line) -> None:
        bar = self.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        color = COLORS.get(line.kind, "#888")
        stamp = time.strftime("%H:%M:%S", time.localtime(line.time))
        label = html.escape(line.label)
        text = html.escape(line.text)
        if line.kind == "system":
            body = f'<span style="color:{color}"><i>{label}: {text}</i></span>'
        else:
            who = f"<b>{label}</b>: " if label else ""
            body = f'<span style="color:{color}">{who}</span>{text}'
            if line.cq_from:
                body = f'<a href="cq:{html.escape(line.cq_from)}" style="text-decoration:none">{body}</a>'
        self.append(f'<span style="color:#999">{stamp}</span> {body}')
        if at_end:
            self.moveCursor(QTextCursor.End)
            bar.setValue(bar.maximum())

    def set_lines(self, lines: list[Line]) -> None:
        self.clear()
        for line in lines:
            self.append_line(line)
