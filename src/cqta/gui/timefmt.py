"""All times shown by the app are in the computer's local time zone."""

from __future__ import annotations

import time


def hms(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def hm(ts: float | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def date_hm(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
