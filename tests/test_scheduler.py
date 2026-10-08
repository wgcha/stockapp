import unittest
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path

from stock_guide_agent.scheduler import (
    JobDefinition,
    ScheduledJobRunner,
    SQLiteJobStateStore,
    due_jobs,
    initialize_jobs,
)


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class SchedulerTests(unittest.TestCase):
    def test_all_jobs_begin_due_and_only_research_uses_llm(self) -> None:
        states = initialize_jobs(NOW)
        due = due_jobs(states, NOW)

        self.assertEqual(len(due), len(states))
        self.assertIn("daily_user_digest", states)
        llm_jobs = [item for item in due if item.definition.execution_mode == "llm_research"]
        self.assertEqual([item.definition.job_id for item in llm_jobs], ["external_strategy_research"])

    def test_success_reschedules_using_job_interval(self) -> None:
        states = initialize_jobs(NOW)
        quote = states["toss_invest_market_data"]
        quote.mark_success(NOW)

        self.assertFalse(quote.due(NOW + timedelta(seconds=59)))
        self.assertTrue(quote.due(NOW + timedelta(minutes=1)))

    def test_repeated_failure_opens_circuit_and_prevents_request_storm(self) -> None:
        state = initialize_jobs(NOW)["open_dart_disclosures"]
        state.mark_failure(NOW)
        state.mark_failure(NOW + timedelta(minutes=5))
        state.mark_failure(NOW + timedelta(minutes=15))

        self.assertTrue(state.circuit_open)
        cooldown_end = NOW + timedelta(minutes=30)
        self.assertTrue(state.due(cooldown_end))
        state.begin_probe(cooldown_end)
        self.assertFalse(state.due(cooldown_end))
        state.mark_success(cooldown_end)
        self.assertTrue(state.due(cooldown_end + state.definition.interval))

    def test_naive_time_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            initialize_jobs(datetime(2026, 7, 17))

    def test_persistent_runner_does_not_repeat_success_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = JobDefinition("job", timedelta(minutes=5), "api_only")
            path = Path(directory) / "jobs.sqlite3"
            calls = []
            runner = ScheduledJobRunner(
                SQLiteJobStateStore(path, start=NOW, definitions=(definition,)),
                {"job": lambda now: calls.append(now) or "ok"},
            )
            self.assertEqual(len(runner.run_due(NOW)), 1)

            restarted = ScheduledJobRunner(
                SQLiteJobStateStore(path, start=NOW, definitions=(definition,)),
                {"job": lambda now: calls.append(now) or "ok"},
            )
            self.assertEqual(restarted.run_due(NOW + timedelta(minutes=4)), ())
            self.assertEqual(len(restarted.run_due(NOW + timedelta(minutes=5, seconds=1))), 1)
            self.assertEqual(len(calls), 2)

    def test_runner_persists_failure_and_opens_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition = JobDefinition(
                "job", timedelta(minutes=1), "api_only", max_consecutive_failures=3
            )
            store = SQLiteJobStateStore(
                Path(directory) / "jobs.sqlite3", start=NOW, definitions=(definition,)
            )

            def fail(now):
                raise RuntimeError("provider down")

            runner = ScheduledJobRunner(store, {"job": fail})
            runner.run_due(NOW)
            runner.run_due(NOW + timedelta(minutes=2))
            runner.run_due(NOW + timedelta(minutes=6))

            state = store.load()["job"]
            self.assertTrue(state.circuit_open)
            self.assertEqual(state.consecutive_failures, 3)


if __name__ == "__main__":
    unittest.main()
