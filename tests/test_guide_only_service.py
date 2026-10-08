import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from stock_guide_agent.brokers import BrokerRouter, TossBrokerGateway
from stock_guide_agent.execution import ApprovalGate, ExecutionPolicy, ExecutionState
from stock_guide_agent.guidance import GuideInput
from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.market import MarketSnapshot
from stock_guide_agent.service import StockGuideService
from stock_guide_agent.state import SQLiteUserStateStore
from stock_guide_agent.telegram import TelegramBotClient
from stock_guide_agent.toss import TossInvestClient, TossInvestToken


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(200, {}, {"ok": True, "result": {}})


def update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id},
        },
    }


def guide_factory(user_id, stock_code, intent, holding, now):
    return GuideInput(
        stock_code=stock_code,
        market=MarketSnapshot(
            as_of=now,
            regime="risk_on",
            score=0.6,
            confidence=0.8,
            category_scores={"price": 0.7},
            evidence=[],
        ),
        strategy_id="approved_strategy",
        strategy_approved=True,
        signal_score=0.8,
        signal_confidence=0.8,
        max_trade_loss_pct=0.5,
        max_daily_loss_pct=1.5,
        daily_pnl_pct=0,
    )


class GuideOnlyServiceTests(unittest.TestCase):
    def make_service(self):
        transport = FakeTransport()
        service = StockGuideService(
            bot=TelegramBotClient(
                "TOKEN", 900, allowed_user_ids={30, 900}, transport=transport
            ),
            store=SQLiteUserStateStore(Path(self.directory) / "state.sqlite3"),
            approval_gate=ApprovalGate(
                ExecutionPolicy(), ExecutionState(), owner_user_id=900
            ),
            broker_router=BrokerRouter(
                [
                    TossBrokerGateway(
                        TossInvestClient("CLIENT", "SECRET"),
                        TossInvestToken("TOKEN", "Bearer", None),
                    )
                ]
            ),
            guide_input_factory=guide_factory,
        )
        return service, transport

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = self.tempdir.name

    def tearDown(self):
        self.tempdir.cleanup()

    def test_concrete_trade_commands_return_guidance_without_proposal_or_broker_call(self):
        service, transport = self.make_service()

        with patch.object(service.approval_gate, "propose", side_effect=AssertionError), \
             patch.object(service.broker_router, "submit_authorized_order", side_effect=AssertionError):
            for index, text in enumerate(("삼성전자 사줘", "주문 취소해줘", "005930 사 줘"), start=1):
                result = service.handle_update(update(index, 30, text), now=NOW)
                self.assertEqual(result.detail, "guide_only_order")

        self.assertEqual(len(transport.requests), 3)
        self.assertTrue(
            all("증권사 앱에서 직접" in request.json_body["text"]
                for request in transport.requests)
        )

    def test_owner_approval_and_rejection_are_guidance_only(self):
        service, transport = self.make_service()

        approved = service.handle_update(update(1, 900, "승인 proposal-1 1234"), now=NOW)
        rejected = service.handle_update(update(2, 900, "거절 proposal-1"), now=NOW)

        self.assertEqual(approved.detail, "guide_only_order")
        self.assertEqual(rejected.detail, "guide_only_order")
        self.assertEqual(len(transport.requests), 2)

    def test_questions_and_settings_are_not_misclassified_as_orders(self):
        service, transport = self.make_service()

        question = service.handle_update(update(1, 30, "매수해도 돼?"), now=NOW)
        setting = service.handle_update(
            update(2, 30, "장기 100%, 연간 8%, 주문마다 승인"), now=NOW
        )

        self.assertNotEqual(question.detail, "guide_only_order")
        self.assertEqual(setting.kind, "intent_saved")
        self.assertEqual(len(transport.requests), 2)

    def test_orders_after_a_guide_preserve_user_state_and_never_reach_execution(self):
        service, transport = self.make_service()
        service.handle_update(update(1, 30, "장기 100%, 연간 8%"), now=NOW)
        service.handle_update(update(2, 30, "보유 005930 10주 평단 70000원"), now=NOW)
        service.handle_update(update(3, 30, "가이드 005930"), now=NOW)
        self.assertIn((30, "005930"), service._latest_guides)
        intent_before = service.store.load_intent(30, as_of=NOW).to_dict()
        holdings_before = service.store.list_holdings(30)
        transport.requests.clear()
        commands = (
            "주문 005930 1주 70000원",
            "005930 1주를 7만원에 사줘",
            "005930 1주를 7만원에 팔아줘",
            "주문 정정해줘",
        )
        with patch.object(service.approval_gate, "propose", side_effect=AssertionError), \
             patch.object(service.approval_gate, "authorize", side_effect=AssertionError), \
             patch.object(service.broker_router, "submit_authorized_order", side_effect=AssertionError):
            for index, command in enumerate(commands, start=4):
                with self.subTest(command=command):
                    result = service.handle_update(update(index, 30, command), now=NOW)
                    self.assertEqual(result.detail, "guide_only_order")
        self.assertEqual(service.store.load_intent(30, as_of=NOW).to_dict(), intent_before)
        self.assertEqual(service.store.list_holdings(30), holdings_before)
        self.assertEqual(service.approval_gate._pending, {})
        self.assertEqual(service.store.count_all_order_executions(NOW.date()), 0)
        self.assertEqual(len(transport.requests), len(commands))
        self.assertTrue(all(request.json_body["chat_id"] == 30 for request in transport.requests))

    def test_bare_and_member_approval_commands_are_also_guidance_only(self):
        service, transport = self.make_service()
        with patch.object(service.approval_gate, "authorize", side_effect=AssertionError), \
             patch.object(service.approval_gate, "reject", side_effect=AssertionError):
            for user_id in (30, 900):
                for command in ("승인", "거절", "승인 old 1234", "거절 old"):
                    with self.subTest(user=user_id, command=command):
                        event = service.handle_update(update(1, user_id, command), now=NOW)
                        self.assertEqual(event.detail, "guide_only_order")
        self.assertEqual(len(transport.requests), 8)

    def test_unallowed_user_gets_no_response_or_state_change(self):
        service, transport = self.make_service()
        for command in ("도움말", "삼성전자 사줘", "승인 old 1234"):
            event = service.handle_update(update(1, 777, command), now=NOW)
            self.assertEqual(event.kind, "ignored")
        self.assertEqual(transport.requests, [])
        self.assertIsNone(service.store.load_intent(777, as_of=NOW))

    def test_help_and_guide_outputs_do_not_request_owner_order_approval(self):
        service, transport = self.make_service()
        for index, command in enumerate((
            "도움말", "장기 100%, 연간 8%", "관심 005930", "가이드 005930", "오늘 가이드"
        ), start=1):
            service.handle_update(update(index, 30, command), now=NOW)
        text = "\n".join(request.json_body["text"] for request in transport.requests)
        self.assertIn("증권사 앱에서 직접", text)
        self.assertNotIn("소유자 승인이 필요", text)
        self.assertNotIn("가이드 후:", text)
        self.assertNotIn("주문 권한:", text)


if __name__ == "__main__":
    unittest.main()
