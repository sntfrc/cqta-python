"""All times shown by the app are UTC, as is usual in amateur radio."""

from __future__ import annotations

import time


def hms(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime(ts))


def hm(ts: float | None = None) -> str:
    return time.strftime("%H:%MZ", time.gmtime(ts))


def date_hm(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(ts))
