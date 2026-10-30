"""A single worker thread with a task queue and timers.

All protocol state (engines, transports, chat, TFTP) is confined to one Dispatcher thread, so none of it
needs locks. Other threads (audio callbacks, BLE loop, sockets, the GUI) hand work over with `post`.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)


class Timer:
    __slots__ = ("when", "fn", "cancelled", "interval")

    def __init__(self, when: float, fn: Callable[[], None], interval: Optional[float] = None) -> None:
        self.when = when
        self.fn = fn
        self.cancelled = False
        self.interval = interval

    def cancel(self) -> None:
        self.cancelled = True


class Dispatcher:
    def __init__(self, name: str = "cqta-core") -> None:
        self._cv = threading.Condition()
        self._tasks: list[Callable[[], None]] = []
        self._timers: list[tuple[float, int, Timer]] = []
        self._counter = itertools.count()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._name = name

    def start(self) -> None:
        with self._cv:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._running = False
            self._cv.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    def in_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def post(self, fn: Callable[[], None]) -> None:
        with self._cv:
            self._tasks.append(fn)
            self._cv.notify()

    def call_later(self, delay: float, fn: Callable[[], None]) -> Timer:
        t = Timer(time.monotonic() + delay, fn)
        self._push(t)
        return t

    def call_every(self, interval: float, fn: Callable[[], None], first: Optional[float] = None) -> Timer:
        t = Timer(time.monotonic() + (interval if first is None else first), fn, interval)
        self._push(t)
        return t

    def _push(self, t: Timer) -> None:
        with self._cv:
            heapq.heappush(self._timers, (t.when, next(self._counter), t))
            self._cv.notify()

    def _run(self) -> None:
        while True:
            due: list[Timer] = []
            with self._cv:
                while self._running:
                    now = time.monotonic()
                    while self._timers and (self._timers[0][2].cancelled or self._timers[0][0] <= now):
                        _, _, t = heapq.heappop(self._timers)
                        if not t.cancelled:
                            due.append(t)
                    if self._tasks or due:
                        break
                    timeout = self._timers[0][0] - now if self._timers else None
                    self._cv.wait(timeout)
                if not self._running:
                    return
                batch, self._tasks = self._tasks, []
            for fn in batch:
                self._safe(fn)
            for t in due:
                if t.cancelled:
                    continue
                self._safe(t.fn)
                if t.interval is not None and not t.cancelled:
                    t.when = time.monotonic() + t.interval
                    self._push(t)

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Unhandled error in dispatcher task")
