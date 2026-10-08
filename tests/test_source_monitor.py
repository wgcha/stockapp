import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_guide_agent.source_monitor import (
    SQLiteSourceFingerprintStore,
    StrategySourceMonitor,
)
from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, StrategyRecord


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class StrategySourceMonitorTests(unittest.TestCase):
    def test_initial_and_changed_content_queue_review_but_unchanged_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSourceFingerprintStore(Path(directory) / "source.sqlite3")
            content = {"value": b"version-one"}
            monitor = StrategySourceMonitor(
                store, fetcher=lambda url: content["value"]
            )
            records = [StrategyRecord(PUBLIC_STRATEGY_CATALOG[0])]

            first = monitor.scan(records, now=NOW)
            second = monitor.scan(records, now=NOW)
            content["value"] = b"version-two"
            third = monitor.scan(records, now=NOW)

            self.assertEqual(len(first.changed), 1)
            self.assertEqual(len(second.unchanged), 1)
            self.assertEqual(len(third.changed), 1)
            self.assertEqual(third.failures, ())

    def test_one_failed_source_does_not_hide_other_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSourceFingerprintStore(Path(directory) / "source.sqlite3")

            def fetch(url):
                if "ssrn" in url:
                    raise RuntimeError("provider unavailable")
                return b"document"

            monitor = StrategySourceMonitor(store, fetcher=fetch)
            records = [StrategyRecord(item) for item in PUBLIC_STRATEGY_CATALOG[:2]]

            result = monitor.scan(records, now=NOW)

            self.assertEqual(len(result.changed), 1)
            self.assertEqual(len(result.failures), 1)
            self.assertIn("time_series_momentum", result.failures[0])


if __name__ == "__main__":
    unittest.main()
