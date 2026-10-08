"""Single-worker asynchronous wrapper for scheduled jobs.

The worker is deliberately cooperative: Python handlers are never force-killed.
Execution timeout is recorded by the scheduler as an overdue status, while the
active marker prevents a second invocation until the original handler returns.
"""
from __future__ import annotations

from datetime import datetime
import threading
from typing import Optional

from .scheduler import JobRunResult, ScheduledJobRunner, _require_aware


class BackgroundJobRunner:
    def __init__(self, runner: ScheduledJobRunner) -> None:
        self.runner = runner
        self._condition = threading.Condition()
        self._scheduled: Optional[datetime] = None
        self._active = False
        self._closed = False
        self._completed: list[JobRunResult] = []
        self._worker = threading.Thread(
            target=self._run_worker,
            name="scheduled-jobs",
            daemon=True,
        )
        self._worker.start()

    @property
    def active(self) -> bool:
        with self._condition:
            return self._active or self._scheduled is not None

    @property
    def completed_results(self) -> tuple[JobRunResult, ...]:
        with self._condition:
            return tuple(self._completed)

    def drain_results(self) -> tuple[JobRunResult, ...]:
        with self._condition:
            results = tuple(self._completed)
            self._completed.clear()
            return results

    def run_due(self, now: datetime) -> tuple[JobRunResult, ...]:
        """Schedule one run and return already-completed results immediately.

        While a run is active or queued, another request is ignored. This is a
        deliberate single-worker guarantee, including for long-running handlers.
        """
        _require_aware(now)
        with self._condition:
            completed = tuple(self._completed)
            self._completed.clear()
            if not self._closed and not self._active and self._scheduled is None:
                self._scheduled = now
                self._condition.notify()
            return completed

    def close(self, wait: bool = False) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if wait and threading.current_thread() is not self._worker:
            self._worker.join()

    def _run_worker(self) -> None:
        while True:
            with self._condition:
                while self._scheduled is None and not self._closed:
                    self._condition.wait()
                if self._scheduled is None and self._closed:
                    return
                now = self._scheduled
                self._scheduled = None
                self._active = True
            results: tuple[JobRunResult, ...] = ()
            try:
                results = self.runner.run_due(now)
            except Exception:
                # Store or dispatcher failures must not terminate the worker or
                # print provider/credential-bearing tracebacks to stderr.
                results = (JobRunResult("scheduler_dispatch", False, "scheduled dispatch failed"),)
            finally:
                with self._condition:
                    self._completed.extend(results)
                    self._active = False
                    self._condition.notify_all()
