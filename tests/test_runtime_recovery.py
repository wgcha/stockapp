import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_guide_agent.brokers import BrokerRouter
from stock_guide_agent.execution import ApprovalGate, ExecutionPolicy, ExecutionState
from stock_guide_agent.http import HttpResponse
from stock_guide_agent.messaging import SQLiteMessageStore
from stock_guide_agent.runtime import AgentRuntime
from stock_guide_agent.service import StockGuideService
from stock_guide_agent.state import SQLiteUserStateStore
from stock_guide_agent.telegram import TelegramBotClient


def update(update_id, text):
    return {"update_id": update_id, "message": {"text": text,
        "chat": {"id": 30, "type": "private"}, "from": {"id": 30}}}


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.updates = []
        self.requests = []
        self.fail_send = False

    def transport(self, request):
        self.requests.append(request)
        if request.url.endswith("getUpdates"):
            return HttpResponse(200, {}, {"ok": True, "result": self.updates})
        if self.fail_send:
            raise RuntimeError("credential ACCOUNT_SECRET in failed response")
        return HttpResponse(200, {}, {"ok": True})

    def make_runtime(self):
        messages = SQLiteMessageStore(self.root / "messages.sqlite3")
        bot = TelegramBotClient("TOKEN", 900, allowed_user_ids={30},
                                transport=self.transport, message_store=messages)
        service = StockGuideService(
            bot=bot, store=SQLiteUserStateStore(self.root / "users.sqlite3"),
            approval_gate=ApprovalGate(ExecutionPolicy(), ExecutionState(), owner_user_id=900),
            broker_router=BrokerRouter([]),
            guide_input_factory=lambda *args: self.fail("guide provider should not be called"),
        )
        return AgentRuntime(bot, service, message_store=messages)

    def test_failed_reply_retries_after_restart_without_reapplying_saved_settings(self):
        self.updates = [update(100, "이번 한주 장기 100%, 연간 8%")]
        runtime = self.make_runtime()
        self.fail_send = True
        result = runtime.poll_once(timeout_seconds=0)
        self.assertEqual(result.next_offset, 101)
        before = runtime.service.store.load_intent(30).to_dict()
        self.fail_send = False
        restarted = self.make_runtime()
        later = datetime.now(timezone.utc) + timedelta(seconds=6)
        with patch.object(restarted.service, "handle_update", side_effect=AssertionError("replay")):
            result = restarted.poll_once(now=later, timeout_seconds=0)
        self.assertEqual(result.events, ())
        self.assertEqual(restarted.service.store.load_intent(30).to_dict(), before)
        self.assertEqual(restarted.message_store.due_messages(now=later), [])
        get_requests = [item for item in self.requests if item.url.endswith("getUpdates")]
        self.assertEqual(get_requests[-1].query["offset"], "101")

    def test_interrupted_update_is_reported_once_and_not_replayed(self):
        runtime = self.make_runtime()
        now = datetime.now(timezone.utc)
        runtime.message_store.claim_update(200, now=now)
        self.updates = [update(200, "보유 005930 10주 평단 70000원")]
        with patch.object(runtime.service, "handle_update", side_effect=AssertionError("replay")):
            runtime.poll_once(now=now + timedelta(seconds=1), timeout_seconds=0)
        alerts = [item for item in self.requests if item.url.endswith("sendMessage")]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].json_body["chat_id"], 900)
        self.assertIn("결과가 불명확", alerts[0].json_body["text"])
        self.requests.clear()
        self.make_runtime().poll_once(now=now + timedelta(seconds=2), timeout_seconds=0)
        self.assertFalse(any(item.url.endswith("sendMessage") for item in self.requests))

    def test_partial_service_failure_does_not_replay_or_expose_error(self):
        runtime = self.make_runtime()
        self.updates = [update(300, "private command")]
        now = datetime.now(timezone.utc) + timedelta(seconds=1)

        def save_then_fail(payload, *, now):
            runtime.service.store.set_account_equity(30, 1000000, updated_at=now)
            raise RuntimeError("ACCOUNT_SECRET credentials and private text")

        with patch.object(runtime.service, "handle_update", side_effect=save_then_fail):
            result = runtime.poll_once(now=now, timeout_seconds=0)
        self.assertEqual(result.failures, 1)
        restarted = self.make_runtime()
        with patch.object(restarted.service, "handle_update", side_effect=AssertionError("replay")):
            restarted.poll_once(now=now + timedelta(seconds=1), timeout_seconds=0)
        self.assertEqual(restarted.service.store.get_account_equity(30), 1000000)
        text = " ".join(item.json_body["text"] for item in self.requests if item.url.endswith("sendMessage"))
        self.assertNotIn("ACCOUNT_SECRET", text)
        self.assertIn("요청을 다시", text)

    def test_one_poll_flushes_its_new_reply_and_rejects_naive_time(self):
        runtime = self.make_runtime()
        self.updates = [update(400, "도움말")]
        runtime.poll_once(timeout_seconds=0)
        self.assertTrue(any(item.url.endswith("sendMessage") for item in self.requests))
        with self.assertRaises(ValueError):
            runtime.poll_once(now=datetime(2026, 9, 10), timeout_seconds=0)

    def test_week_of_inactivity_accepts_a_new_lower_update_number(self):
        runtime = self.make_runtime()
        now = datetime.now(timezone.utc)
        self.updates = [update(10000, "도움말")]
        runtime.poll_once(now=now, timeout_seconds=0)
        self.updates = [update(5, "내 투자금은 1,000만원")]
        later = now + timedelta(days=7, seconds=1)
        result = runtime.poll_once(now=later, timeout_seconds=0)
        self.assertEqual(result.next_offset, 6)
        self.assertEqual(runtime.service.store.get_account_equity(30), 10000000)
        get_requests = [item for item in self.requests if item.url.endswith("getUpdates")]
        self.assertNotIn("offset", get_requests[-1].query)


if __name__ == "__main__":
    unittest.main()
