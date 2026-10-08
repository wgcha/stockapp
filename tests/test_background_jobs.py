import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.background_jobs import BackgroundJobRunner
from stock_guide_agent.scheduler import JobDefinition, ScheduledJobRunner, SQLiteJobStateStore


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class BackgroundJobRunnerTests(unittest.TestCase):
    def test_long_handler_is_async_and_is_not_started_twice(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []
        definition = JobDefinition("job", timedelta(minutes=1), "api_only")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJobStateStore(Path(directory) / "jobs.sqlite3", start=NOW, definitions=(definition,))

            def handler(now):
                calls.append(now)
                started.set()
                release.wait(5)
                finished.set()
                return "ok"

            background = BackgroundJobRunner(ScheduledJobRunner(store, {"job": handler}))
            try:
                self.assertEqual(background.run_due(NOW), ())
                self.assertTrue(started.wait(2))
                self.assertTrue(background.active)
                self.assertEqual(background.run_due(NOW + timedelta(minutes=1)), ())
                self.assertEqual(len(calls), 1)
                release.set()
                self.assertTrue(finished.wait(2))
                background.close(wait=True)
                self.assertEqual(len(background.completed_results), 1)
            finally:
                background.close(wait=True)

    def test_close_without_wait_is_nonblocking(self):
        definition = JobDefinition("job", timedelta(minutes=1), "api_only")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteJobStateStore(Path(directory) / "jobs.sqlite3", start=NOW, definitions=(definition,))
            background = BackgroundJobRunner(ScheduledJobRunner(store, {"job": lambda now: "ok"}))
            background.close(wait=False)
            self.assertFalse(background.active)


if __name__ == "__main__":
    unittest.main()
