# SPDX-FileCopyrightText: 2026 caderuntime <caderuntime@cade.run>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure-unit tests for the off-thread send (`worker.py`).

The properties asserted here are the ones that keep Blender's UI alive during an
upload, so they are stated as invariants rather than as a recorded sequence:

* `poll()` NEVER blocks — it returns None while the work is still running,
  which is what makes calling it from a modal timer safe;
* the outcome is delivered EXACTLY ONCE, so a modal operator cannot finish twice;
* the work runs on ANOTHER thread — the whole point, and the reason nothing it
  closes over may be `bpy` data;
* a raised exception becomes an outcome, not a lost job.
"""

from __future__ import annotations

import threading
import time
import unittest

import fake_assets  # noqa: F401  (puts the repo root on sys.path)

from showcade_bridge import worker


class Drain(unittest.TestCase):
    def test_poll_does_not_block_while_the_work_is_running(self):
        release = threading.Event()
        job = worker.SendJob(lambda: release.wait(5) or "done").start()
        try:
            # Ten polls against a job that cannot finish yet: if any of them
            # blocked, this would take ~5s instead of microseconds.
            started = time.monotonic()
            for _ in range(10):
                self.assertIsNone(job.poll())
            self.assertLess(time.monotonic() - started, 1.0, "poll() blocked")
        finally:
            release.set()
        self.assertTrue(job.wait(5).ok)

    def test_the_outcome_is_delivered_exactly_once(self):
        job = worker.SendJob(lambda: 42).start()
        outcome = job.wait(5)
        self.assertEqual(outcome.value, 42)
        # A modal operator polls on every timer tick; a second delivery would
        # make it report (and finish) twice.
        for _ in range(5):
            self.assertIsNone(job.poll())
        # …but the already-drained result stays available, idempotently.
        self.assertIs(job.result(), outcome)
        self.assertTrue(job.finished)

    def test_the_work_runs_off_the_calling_thread(self):
        caller = threading.current_thread()
        job = worker.SendJob(lambda: threading.current_thread()).start()
        outcome = job.wait(5)
        self.assertIsNot(outcome.value, caller)
        self.assertTrue(outcome.ok)

    def test_an_exception_becomes_a_failed_outcome(self):
        job = worker.SendJob(lambda: (_ for _ in ()).throw(RuntimeError("catalog said no"))).start()
        outcome = job.wait(5)
        self.assertFalse(outcome.ok)
        self.assertIn("catalog said no", outcome.error)
        self.assertIsNone(outcome.value)

    def test_an_exception_with_no_message_still_produces_one(self):
        # A bare `raise TimeoutError` would otherwise report an empty string,
        # and an empty operator report reads as a silent failure.
        job = worker.SendJob(lambda: (_ for _ in ()).throw(TimeoutError())).start()
        outcome = job.wait(5)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.error.strip())

    def test_a_job_cannot_be_started_twice(self):
        job = worker.SendJob(lambda: None).start()
        with self.assertRaises(RuntimeError):
            job.start()
        job.wait(5)

    def test_an_unstarted_job_polls_empty_forever(self):
        job = worker.SendJob(lambda: None)
        self.assertFalse(job.started)
        self.assertIsNone(job.poll())
        self.assertIsNone(job.result())

    def test_elapsed_time_is_measured_not_zero(self):
        job = worker.SendJob(lambda: time.sleep(0.05)).start()
        outcome = job.wait(5)
        self.assertGreaterEqual(outcome.elapsed_s, 0.04)

    def test_the_worker_thread_never_keeps_blender_alive(self):
        # A hung socket must not block quit; the request timeout is the real
        # bound, this is the backstop.
        release = threading.Event()
        job = worker.SendJob(lambda: release.wait(30)).start()
        self.assertTrue(job._thread.daemon)
        release.set()
        job.wait(5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
