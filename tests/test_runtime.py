import unittest
import tempfile
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.runtime import AgentRuntime, build_runtime_from_env
from stock_guide_agent.execution_safety import SQLiteExecutionSafetyStore
from stock_guide_agent.toss import TossInvestClient
from stock_guide_agent.service import ServiceEvent
from stock_guide_agent.telegram import TelegramBotClient


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, updates):
        self.updates = updates
        self.requests: list[HttpRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.endswith("/getUpdates"):
            return HttpResponse(200, {}, {"ok": True, "result": self.updates})
        return HttpResponse(200, {}, {"ok": True, "result": {}})


class FakeService:
    def __init__(self):
        self.seen = []

    def handle_update(self, payload, *, now):
        self.seen.append(payload["update_id"])
        if payload["update_id"] == 2:
            raise ValueError("bad update")
        return ServiceEvent("ignored")


class FakeMessageStore:
    def __init__(self, *, claim_error=False):
        self.offset = None
        self.claim_error = claim_error
        self.claimed = []
        self.finished = []

    def get_next_offset(self):
        return self.offset

    def recover_interrupted(self, *, now):
        return ()

    def reset_after_idle(self, *, now):
        return False

    def claim_update(self, update_id, *, now):
        if self.claim_error:
            raise RuntimeError("storage unavailable")
        if update_id in self.claimed:
            return False
        self.claimed.append(update_id)
        self.offset = update_id + 1
        return True

    def finish_update(self, update_id, *, status, now):
        self.finished.append((update_id, status))


class RaisingScheduler:
    def run_due(self, now):
        raise RuntimeError("scheduler detail must stay private")


class RaisingTransport(FakeTransport):
    def __init__(self, updates):
        super().__init__(updates)
        self.update_calls = 0

    def __call__(self, request):
        if request.url.endswith("/getUpdates"):
            self.update_calls += 1
            raise RuntimeError("network detail must stay private")
        return super().__call__(request)


class RuntimeTests(unittest.TestCase):
    def test_live_environment_is_rejected_before_runtime_starts(self) -> None:
        with patch.dict("os.environ", {"AGENT_EXECUTION_ENV": "live"}, clear=True):
            with self.assertRaisesRegex(ValueError, "guide-only runtime"):
                build_runtime_from_env()

    def test_builder_restores_shared_kill_switch_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            safety_store = SQLiteExecutionSafetyStore(data_dir / "users.sqlite3")
            safety_store.save(True, "persisted owner stop")
            bot = TelegramBotClient(
                "TOKEN", 900, transport=lambda request: self.fail("network called")
            )
            toss = TossInvestClient(
                "CLIENT", "SECRET", transport=lambda request: self.fail("network called")
            )
            with patch.dict(
                "os.environ",
                {
                    "AGENT_DATA_DIR": str(data_dir),
                    "AGENT_EXECUTION_ENV": "mock",
                    "AGENT_MAX_DAILY_LOSS_PCT": "2.5",
                    "AGENT_ACCOUNT_EQUITY_KRW": "12345",
                    "TELEGRAM_BOT_TOKEN": "TOKEN",
                    "TELEGRAM_OWNER_USER_ID": "900",
                    "TOSSINVEST_CLIENT_ID": "CLIENT",
                    "TOSSINVEST_CLIENT_SECRET": "SECRET",
                },
                clear=True,
            ), patch(
                "stock_guide_agent.runtime.TelegramBotClient.from_env",
                return_value=bot,
            ), patch(
                "stock_guide_agent.runtime.TossInvestClient.from_env",
                return_value=toss,
            ):
                runtime = build_runtime_from_env()
                try:
                    state = runtime.service.approval_gate.state
                    self.assertTrue(state.kill_switch_active)
                    self.assertEqual(state.kill_switch_reason, "persisted owner stop")
                    self.assertEqual(state.max_daily_loss_pct, 2.5)
                    self.assertEqual(state.account_equity, 12345)
                    state.reset_kill_switch("RESET KILL SWITCH")
                    self.assertEqual(safety_store.load(), (False, None))
                finally:
                    runtime.close()

    def test_builder_rejects_corrupt_kill_switch_before_creating_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            connection = sqlite3.connect(data_dir / "users.sqlite3")
            try:
                connection.execute(
                    """
                    CREATE TABLE execution_safety_state (
                        singleton_id INTEGER,
                        kill_switch_active,
                        kill_switch_reason
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO execution_safety_state VALUES (1, 4, 'SECRET')"
                )
                connection.commit()
            finally:
                connection.close()
            with patch.dict(
                "os.environ",
                {"AGENT_DATA_DIR": str(data_dir), "AGENT_EXECUTION_ENV": "mock"},
                clear=True,
            ), patch(
                "stock_guide_agent.runtime.TelegramBotClient.from_env"
            ) as bot_factory:
                with self.assertRaisesRegex(RuntimeError, "execution safety state is invalid") as caught:
                    build_runtime_from_env()
            self.assertNotIn("SECRET", str(caught.exception))
            bot_factory.assert_not_called()

    def test_polling_isolates_bad_update_and_advances_offset(self) -> None:
        transport = FakeTransport(
            [
                {"update_id": 1},
                {"update_id": 2},
                {"update_id": 3},
            ]
        )
        bot = TelegramBotClient("TOKEN", 900, transport=transport)
        service = FakeService()
        runtime = AgentRuntime(bot, service)  # type: ignore[arg-type]

        result = runtime.poll_once(now=NOW, timeout_seconds=0)

        self.assertEqual(service.seen, [1, 2, 3])
        self.assertEqual(result.failures, 1)
        self.assertEqual(result.next_offset, 4)
        self.assertEqual(len(result.events), 2)
        error_message = next(
            request for request in transport.requests if request.url.endswith("/sendMessage")
        )
        self.assertEqual(error_message.json_body["chat_id"], 900)
        self.assertIn("업데이트 2", error_message.json_body["text"])

    def test_next_poll_sends_saved_offset(self) -> None:
        transport = FakeTransport([{"update_id": 7}])
        runtime = AgentRuntime(
            TelegramBotClient("TOKEN", 900, transport=transport),
            FakeService(),  # type: ignore[arg-type]
        )
        runtime.poll_once(now=NOW, timeout_seconds=0)
        transport.updates = []
        runtime.poll_once(now=NOW, timeout_seconds=0)

        get_requests = [
            request for request in transport.requests if request.url.endswith("/getUpdates")
        ]
        self.assertEqual(get_requests[1].query["offset"], "8")

    def test_malformed_updates_are_ignored_and_valid_ids_are_sorted(self) -> None:
        transport = FakeTransport(
            [{"update_id": "2"}, ["bad"], {"update_id": True}, {"update_id": 1}]
        )
        service = FakeService()
        runtime = AgentRuntime(
            TelegramBotClient("TOKEN", 900, transport=transport), service,  # type: ignore[arg-type]
            message_store=FakeMessageStore(),
        )
        result = runtime.poll_once(now=NOW, timeout_seconds=0)
        self.assertEqual(service.seen, [1])
        self.assertEqual(result.failures, 3)

    def test_message_store_error_stops_ordered_processing(self) -> None:
        transport = FakeTransport([{"update_id": 1}, {"update_id": 2}])
        service = FakeService()
        runtime = AgentRuntime(
            TelegramBotClient("TOKEN", 900, transport=transport), service,  # type: ignore[arg-type]
            message_store=FakeMessageStore(claim_error=True),
        )
        result = runtime.poll_once(now=NOW, timeout_seconds=0)
        self.assertEqual(service.seen, [])
        self.assertIsNone(result.next_offset)
        self.assertEqual(result.failures, 1)

    def test_poll_failure_uses_time_based_backoff_and_hides_error(self) -> None:
        transport = RaisingTransport([])
        runtime = AgentRuntime(
            TelegramBotClient("TOKEN", 900, transport=transport), FakeService()  # type: ignore[arg-type]
        )
        first = runtime.poll_once(now=NOW, timeout_seconds=0)
        second = runtime.poll_once(now=NOW, timeout_seconds=0)
        self.assertEqual(first.failures, 1)
        self.assertEqual(second.failures, 0)
        self.assertEqual(transport.update_calls, 1)

    def test_scheduler_and_outbox_failures_are_isolated_and_counted(self) -> None:
        transport = FakeTransport([])
        bot = TelegramBotClient("TOKEN", 900, transport=transport)
        bot.flush_outbox = lambda **kwargs: 2  # type: ignore[method-assign]
        runtime = AgentRuntime(bot, FakeService(), scheduler=RaisingScheduler())  # type: ignore[arg-type]
        result = runtime.poll_once(now=NOW, timeout_seconds=0)
        self.assertEqual(result.failures, 5)
        self.assertEqual(result.events, ())

    def test_backoff_paths_retain_scheduler_and_outbox_failure_counts(self) -> None:
        transport = RaisingTransport([])
        bot = TelegramBotClient("TOKEN", 900, transport=transport)
        bot.flush_outbox = lambda **kwargs: 2  # type: ignore[method-assign]
        runtime = AgentRuntime(bot, FakeService(), scheduler=RaisingScheduler())  # type: ignore[arg-type]
        first = runtime.poll_once(now=NOW, timeout_seconds=0)
        second = runtime.poll_once(now=NOW, timeout_seconds=0)
        self.assertEqual(first.failures, 4)  # scheduler + flush + polling
        self.assertEqual(second.failures, 3)  # scheduler + flush while backing off


if __name__ == "__main__":
    unittest.main()
