import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_guide_agent.backup import backup_databases, restore_databases
from stock_guide_agent.data_lock import RuntimeDataLock
from stock_guide_agent.messaging import SQLiteMessageStore
from stock_guide_agent.state import SQLiteUserStateStore
from stock_guide_agent.scheduler import JobDefinition, SQLiteJobStateStore, ScheduledJobRunner
from stock_guide_agent.background_jobs import BackgroundJobRunner
from stock_guide_agent.runtime_settings import validate_runtime_settings
from stock_guide_agent.doctor import run_preflight
from stock_guide_agent.cli import main
from stock_guide_agent.runtime import AgentRuntime, build_runtime_from_env
from stock_guide_agent.telegram import TelegramBotClient
from stock_guide_agent.http import HttpResponse
from stock_guide_agent.service import ServiceEvent

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class OperationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        SQLiteUserStateStore(self.data / "users.sqlite3").set_account_equity(30, 10000000, updated_at=NOW)
        messages = SQLiteMessageStore(self.data / "messages.sqlite3")
        messages.claim_update(10, now=NOW)
        messages.finish_update(10, status="completed", now=NOW)

    def test_multiple_database_backup_restores_real_data_and_offset(self):
        (self.data / ".env").write_text("SECRET=never-copy", encoding="utf-8")
        backup = self.root / "snapshot"
        manifest = backup_databases(self.data, backup)
        self.assertEqual(set(manifest["files"]), {"users.sqlite3", "messages.sqlite3"})
        self.assertFalse((backup / ".env").exists())
        restored = self.root / "restored"
        restore_databases(backup, restored)
        self.assertEqual(SQLiteUserStateStore(restored / "users.sqlite3").get_account_equity(30), 10000000)
        self.assertEqual(SQLiteMessageStore(restored / "messages.sqlite3").get_next_offset(), 11)
        self.assertFalse(SQLiteMessageStore(restored / "messages.sqlite3").claim_update(10, now=NOW))

    def test_bot_lock_blocks_second_owner_and_backup_until_released(self):
        with RuntimeDataLock(self.data):
            with self.assertRaises(RuntimeError):
                with RuntimeDataLock(self.data):
                    pass
            with self.assertRaises(RuntimeError):
                backup_databases(self.data, self.root / "blocked")
        backup_databases(self.data, self.root / "allowed")

    def test_tampering_existing_target_and_unknown_database_are_rejected(self):
        snapshot = self.root / "snapshot"
        backup_databases(self.data, snapshot)
        with self.assertRaises(ValueError):
            restore_databases(snapshot, self.data)
        with (snapshot / "users.sqlite3").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(ValueError):
            restore_databases(snapshot, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        self.assertEqual(list(self.root.glob(".stock-guide-stage-*")), [])
        (self.data / "unregistered.sqlite3").write_bytes(b"unknown")
        with self.assertRaises(ValueError):
            backup_databases(self.data, self.root / "incomplete")

    def test_manifest_cannot_traverse_outside_backup(self):
        snapshot = self.root / "snapshot"
        backup_databases(self.data, snapshot)
        (snapshot / "manifest.json").write_text(json.dumps({"version": 1, "files": {"../users.sqlite3": {}}}))
        with self.assertRaises(ValueError):
            restore_databases(snapshot, self.root / "escape")

    def test_backup_restore_cli_operates_without_api_credentials(self):
        snapshot, target = self.root / "cli-copy", self.root / "cli-restore"
        for arguments, key in ((["--backup-to", str(snapshot), "--data-dir", str(self.data)], "backed_up_databases"),
                               (["--restore-from", str(snapshot), "--restore-to", str(target)], "restored_databases")):
            output = io.StringIO()
            with patch("sys.argv", ["agent", *arguments]), redirect_stdout(output):
                main()
            self.assertEqual(json.loads(output.getvalue())[key], 2)

    def test_runtime_preflight_rejects_nonfinite_and_invalid_limits(self):
        for name, value in (("AGENT_ACCOUNT_EQUITY_KRW", "nan"), ("PAPER_INITIAL_CAPITAL_KRW", "inf"),
                            ("AGENT_MAX_DAILY_LOSS_PCT", "-1"), ("NEWS_MAX_SYMBOLS", "0")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_runtime_settings({name: value})
                report = run_preflight({name: value})
                self.assertTrue(any(item.check_id == "runtime_settings" and item.status == "error" for item in report.checks))

    def test_overdue_job_status_recovery_and_active_reset_guard(self):
        definition = JobDefinition("job", timedelta(minutes=1), "api_only", execution_timeout=timedelta(seconds=1))
        store = SQLiteJobStateStore(self.data / "jobs.sqlite3", start=NOW, definitions=(definition,))
        state = store.load()["job"]
        state.begin_probe(NOW)
        store.save(state)
        self.assertTrue(store.status("job", now=NOW + timedelta(seconds=2)).execution_overdue)
        with self.assertRaises(RuntimeError):
            store.reset("job", NOW)
        self.assertEqual(store.recover_interrupted(NOW), ("job",))
        self.assertIsNone(store.load()["job"].execution_started)
        store.reset("job", NOW)
        runner = ScheduledJobRunner(store, {"job": lambda now: "ok"})
        with patch("stock_guide_agent.scheduler.time.monotonic", side_effect=[0, 0, 0, 2]):
            runner.run_due(NOW)
        self.assertTrue(store.load()["job"].execution_overdue)

    def test_later_jobs_receive_current_time_after_a_slow_predecessor(self):
        definitions = (JobDefinition("a", timedelta(minutes=1), "api_only"),
                       JobDefinition("b", timedelta(minutes=1), "api_only"))
        store = SQLiteJobStateStore(self.data / "jobs.sqlite3", start=NOW, definitions=definitions)
        seen = []
        runner = ScheduledJobRunner(store, {"a": lambda now: "ok", "b": lambda now: seen.append(now)})
        with patch("stock_guide_agent.scheduler.time.monotonic", side_effect=[0, 0, 0, 600, 600, 600, 600]):
            runner.run_due(NOW)
        self.assertEqual(seen, [NOW + timedelta(minutes=10)])

    def test_background_worker_survives_dispatch_failure_and_can_run_again(self):
        class BrokenOnce:
            def __init__(self):
                self.calls = 0
                self.called = threading.Event()
            def run_due(self, now):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("SECRET failure")
                self.called.set()
                return ()
        runner = BrokenOnce()
        background = BackgroundJobRunner(runner)
        try:
            # Condition wait releases the lock so the worker can publish its result.
            background.run_due(NOW)
            with background._condition:
                self.assertTrue(background._condition.wait_for(lambda: bool(background._completed), timeout=2))
            self.assertFalse(background.completed_results[0].success)
            self.assertNotIn("SECRET", background.completed_results[0].detail)
            background.run_due(NOW)
            self.assertTrue(runner.called.wait(2))
        finally:
            background.close(wait=True)

    def test_runtime_answers_while_a_scheduled_handler_is_blocked(self):
        started, release = threading.Event(), threading.Event()
        definition = JobDefinition("slow", timedelta(minutes=1), "api_only")
        store = SQLiteJobStateStore(self.data / "jobs.sqlite3", start=NOW, definitions=(definition,))
        def slow(now):
            started.set()
            release.wait(5)
        background = BackgroundJobRunner(ScheduledJobRunner(store, {"slow": slow}))
        class Service:
            def __init__(self):
                self.seen = []
            def handle_update(self, payload, *, now):
                self.seen.append(payload["update_id"])
                return ServiceEvent("ignored")
        service = Service()
        bot = TelegramBotClient("TOKEN", 900, transport=lambda request:
            HttpResponse(200, {}, {"ok": True, "result": [{"update_id": 1}]}))
        runtime = AgentRuntime(bot, service, scheduler=background)
        try:
            background.run_due(NOW)
            self.assertTrue(started.wait(2))
            runtime.poll_once(now=NOW, timeout_seconds=0)
            self.assertEqual(service.seen, [1])
            self.assertTrue(background.active)
        finally:
            release.set()
            runtime.close()

    def test_production_builder_wires_one_calendar_and_background_runner(self):
        with patch.dict("os.environ", {
            "AGENT_DATA_DIR": str(self.data), "AGENT_EXECUTION_ENV": "mock",
            "TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_OWNER_USER_ID": "900",
            "TOSSINVEST_CLIENT_ID": "CLIENT", "TOSSINVEST_CLIENT_SECRET": "SECRET"
        }, clear=True):
            with RuntimeDataLock(self.data):
                runtime = build_runtime_from_env()
                try:
                    self.assertIsInstance(runtime.scheduler, BackgroundJobRunner)
                    self.assertIsNotNone(runtime.service.trading_calendar)
                finally:
                    runtime.close()


if __name__ == "__main__":
    unittest.main()
