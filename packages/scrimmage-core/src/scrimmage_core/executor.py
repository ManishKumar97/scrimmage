"""Scrimmage's own threads in a service: claims and async budget asks run here.

The app's default executor is never used for Scrimmage's work, so the SDK can never be the
one that starts a pool thread inside a run, and a slow runner holds none of the app's
threads. A caller takes one of the client's CAP control slots before it hands work here, so
there are as many threads as slots, and a caller that finds no slot waits on its loop.

A run's close waits for a slot on its own thread, so it runs on threads of its own: on the
claims' threads it could hold every one while the slots it waits for belong to claims queued
behind it.
"""

import concurrent.futures
import heapq
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from scrimmage_core import client, threadctx, threads
from scrimmage_core.client import CAP

Work = tuple[Callable[[], Any], "concurrent.futures.Future[Any]"]


class Pool:
    """A fixed set of SDK threads, deaf to SIGINT and SIGTERM, each running one call at once.
    may_wait: its work may wait for a control slot; if not, a request that would wait fails."""

    def __init__(self, size: int, name: str, *, may_wait: bool) -> None:
        self.size = size
        self.name = name
        self.may_wait = may_wait
        self.work: queue.SimpleQueue[Work] = queue.SimpleQueue()
        self._started = False
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[], Any]) -> "concurrent.futures.Future[Any]":
        with self._lock:
            if not self._started:
                self._started = True
                threadctx.pool_spawn(self._start)()  # its threads are a pool's: in no run
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.work.put((fn, future))
        return future

    def _start(self) -> None:
        for n in range(self.size):
            worker = threading.Thread(target=self._loop, name=f"{self.name}-{n}", daemon=True)
            threads.start(worker)

    def _loop(self) -> None:
        if not self.may_wait:
            client.never_wait()
        while True:
            fn, future = self.work.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # the caller decides what it means
                future.set_exception(exc)


class Executor(Pool):
    """The claims' threads, which never wait for a slot; the threads for work that may, a
    run's close or an abandoned claim; and the timers for every close."""

    def __init__(self, size: int = CAP) -> None:
        super().__init__(size, "scrimmage-control", may_wait=False)
        self.waiting = Pool(size, "scrimmage-close", may_wait=True)
        self.timers = Timers(self.waiting)


class Timers:
    """One thread for every run's close: a run's record closes record_grace_s after its reply,
    whatever happens to the loop that served it. Each close is handed to the close threads."""

    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self._due: list[tuple[float, int, Callable[[], Any]]] = []
        self._count = 0
        self._wake = threading.Condition()
        self._started = False

    def later(self, seconds: float, fn: Callable[[], Any]) -> None:
        with self._wake:
            if not self._started:
                self._started = True
                thread = threading.Thread(target=self._loop, name="scrimmage-timers", daemon=True)
                threadctx.pool_spawn(threads.start)(thread)
            self._count += 1
            heapq.heappush(self._due, (time.monotonic() + seconds, self._count, fn))
            self._wake.notify()

    def _loop(self) -> None:
        while True:
            with self._wake:
                while not self._due or self._due[0][0] > time.monotonic():
                    wait = None if not self._due else self._due[0][0] - time.monotonic()
                    self._wake.wait(wait)
                _, _, fn = heapq.heappop(self._due)
            self.pool.submit(fn)


# The one executor of a service process, and its timers, made by start_service.
EXECUTOR: Executor | None = None
