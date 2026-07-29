# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""worker.py — run the blocking upload off Blender's UI thread.

`urllib` has no async mode and Blender's UI thread must not block on a socket,
so the operator exports on the main thread and hands **bytes plus config
strings** to a `SendJob`; a modal timer drains it. No `bpy` here — not for
testing convenience this time but as the actual thread-safety contract: the
worker callable runs on another thread, where touching `bpy` data is undefined
behaviour, and the only way to guarantee it does not is for this module (and the
closure it is given) to have no access to it.

`poll()` never blocks. It returns `None` while the job is in flight and the
`Outcome` exactly once when it is done — the modal operator calls it on every
timer tick, so a blocking drain would reintroduce the freeze this exists to
avoid.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

__all__ = ["Outcome", "SendJob"]


class Outcome(NamedTuple):
    """The single result of a job. A raised exception is an outcome, not a crash."""

    ok: bool
    value: Any = None
    error: str = ""
    elapsed_s: float = 0.0


class SendJob:
    """One-shot background call with a non-blocking, exactly-once drain."""

    def __init__(self, work: Callable[[], Any], *, name: str = "showcade-send"):
        self._work = work
        self._name = name
        self._queue: queue.Queue[Outcome] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._outcome: Outcome | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> SendJob:
        """Spawn the worker. Calling it twice is a bug, not a retry."""
        if self._thread is not None:
            raise RuntimeError("this job has already been started")
        # Daemon: a hung socket must never keep Blender from quitting. The
        # timeout in `transport.request` is the real bound; this is the backstop.
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        started = time.monotonic()
        try:
            value = self._work()
        except BaseException as exc:
            # Nothing above this frame can catch it: an escaping exception in a
            # worker thread would print to the console and leave the modal
            # operator waiting forever.
            self._queue.put(
                Outcome(False, None, str(exc) or exc.__class__.__name__, time.monotonic() - started)
            )
        else:
            self._queue.put(Outcome(True, value, "", time.monotonic() - started))

    # ── drain ────────────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def finished(self) -> bool:
        """True once `poll()` has handed the outcome over."""
        return self._outcome is not None

    def poll(self) -> Outcome | None:
        """The outcome if it is ready, else `None`. Never blocks, never repeats."""
        if self._outcome is not None:
            return None
        try:
            self._outcome = self._queue.get_nowait()
        except queue.Empty:
            return None
        return self._outcome

    def result(self) -> Outcome | None:
        """The already-drained outcome (idempotent, unlike `poll()`)."""
        return self._outcome

    def wait(self, timeout: float | None = None) -> Outcome | None:
        """Block for the outcome — for scripts and `execute()`, never for modal."""
        if self._outcome is None and self._thread is not None:
            self._thread.join(timeout)
            self.poll()
        return self._outcome
