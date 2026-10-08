import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from stock_guide_agent.execution import ApprovalGate, ExecutionPolicy, ExecutionState
from stock_guide_agent.guidance import TradeGuide
from stock_guide_agent.intent import parse_investment_intent
from stock_guide_agent.telegram import (
    TelegramBotClient,
    TelegramConfigurationError,
    build_send_message,
    format_intent_confirmation,
    parse_telegram_update,
)
from stock_guide_agent.http import HttpRequest, HttpResponse


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class TelegramAdapterTests(unittest.TestCase):
    def test_parses_private_text_update(self) -> None:
        message = parse_telegram_update(
            {
                "update_id": 10,
                "message": {
                    "text": "  \uc7a5\uae30\uc548\uc815 60% \ub2e8\ud0c0 40%  ",
                    "chat": {"id": 30, "type": "private"},
                    "from": {"id": 30},
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.chat_id, 30)
        self.assertEqual(message.text, "\uc7a5\uae30\uc548\uc815 60% \ub2e8\ud0c0 40%")

    def test_ignores_group_and_non_text_updates(self) -> None:
        self.assertIsNone(
            parse_telegram_update(
                {
                    "update_id": 1,
                    "message": {
                        "text": "hello",
                        "chat": {"id": 2, "type": "group"},
                        "from": {"id": 3},
                    },
                }
            )
        )
        self.assertIsNone(parse_telegram_update({"update_id": 2, "message": {"photo": []}}))

    def test_formats_short_confirmation_and_safe_limits(self) -> None:
        intent = parse_investment_intent(
            "\uc774\ubc88\uc8fc \ub9e4\uc77c 3% \ubaa9\ud45c, \uc7a5\uae30\uc548\uc815 60% \ub2e8\ud0c0 40%"
        )
        text = format_intent_confirmation(intent)
        payload = build_send_message(20, text)

        self.assertIn("\uc7a5\uae30\uc548\uc815 60%", text)
        self.assertIn("\ub2e8\ud0c0 40%", text)
        self.assertIn("\uc77c\uc77c 3%", text)
        self.assertIn("\uc77c\uc77c \uc911\ub2e8\uc120: 1.5%", text)
        self.assertIn("위험 설명", text)
        self.assertIn("안전 대안", text)
        self.assertIn("연간 6~12%", text)
        self.assertIn("장기 배분 60%", text)
        self.assertIn("이 봇은 투자 가이드만 제공합니다", text)
        self.assertIn("증권사 앱에서 직접 진행", text)
        self.assertIn("보유정보", text)
        self.assertNotIn("주문 권한", text)
        self.assertTrue(text.endswith("추가하거나 바꿀 조건만 이어서 말해도 됩니다."))
        self.assertEqual(payload["chat_id"], 20)
        self.assertTrue(payload["disable_web_page_preview"])

    def test_bot_polling_masks_token_in_url(self) -> None:
        transport = FakeTransport(
            [HttpResponse(200, {}, {"ok": True, "result": [{"update_id": 1}]})]
        )
        bot = TelegramBotClient("SECRET_TOKEN", 30, transport=transport)
        updates = bot.get_updates(offset=10, timeout_seconds=20)

        self.assertEqual(updates[0]["update_id"], 1)
        request = transport.requests[0]
        self.assertEqual(request.query["offset"], "10")
        self.assertNotIn("SECRET_TOKEN", request.redacted()["url"])

    def test_end_to_end_private_intent_sends_confirmation(self) -> None:
        transport = FakeTransport([HttpResponse(200, {}, {"ok": True, "result": {}})])
        bot = TelegramBotClient("TOKEN", 30, transport=transport)
        handled = bot.handle_intent_update(
            {
                "update_id": 10,
                "message": {
                    "text": "\uc7a5\uae30\uc548\uc815 60% \ub2e8\ud0c0 40%, \ub9e4\uc77c 3% \ubaa9\ud45c",
                    "chat": {"id": 30, "type": "private"},
                    "from": {"id": 30},
                },
            }
        )

        self.assertIsNotNone(handled)
        request = transport.requests[0]
        self.assertTrue(request.url.endswith("/sendMessage"))
        self.assertIn("\uc77c\uc77c \uc911\ub2e8\uc120", request.json_body["text"])

    def test_multiple_members_can_use_bot_but_are_not_owners(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(200, {}, {"ok": True, "result": {}}),
                HttpResponse(200, {}, {"ok": True, "result": {}}),
            ]
        )
        bot = TelegramBotClient(
            "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
        )

        for update_id, user_id in enumerate((30, 40), start=1):
            handled = bot.handle_intent_update(
                {
                    "update_id": update_id,
                    "message": {
                        "text": "\uc7a5\uae30 100%",
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": user_id},
                    },
                }
            )
            self.assertIsNotNone(handled)

        self.assertTrue(bot.is_allowed_user(30))
        self.assertTrue(bot.is_allowed_user(40))
        self.assertTrue(bot.is_owner(900))
        self.assertFalse(bot.is_owner(30))
        self.assertEqual(len(transport.requests), 2)

    def test_order_approval_request_is_sent_only_to_owner_chat(self) -> None:
        now = datetime(2026, 7, 17, tzinfo=timezone.utc)
        gate = ApprovalGate(
            ExecutionPolicy(), ExecutionState(), owner_user_id=900
        )
        guide = TradeGuide(
            "005930", "buy", 0.25, 0.8, ("approved",), 0.5,
            "below_70000", "approved_strategy", now,
        )
        challenge = gate.propose(
            guide, quantity=1, limit_price=70000, now=now,
            requester_user_id=30, approval_code="4821",
            estimated_transaction_cost=45.5,
            transaction_costs_complete=True,
            transaction_cost_assumption="fees, tax and slippage",
        )
        transport = FakeTransport(
            [HttpResponse(200, {}, {"ok": True, "result": {}})]
        )
        bot = TelegramBotClient(
            "TOKEN", 900, allowed_user_ids={30, 40}, owner_chat_id=900,
            transport=transport,
        )

        bot.send_owner_approval_request(challenge)

        request = transport.requests[0]
        self.assertEqual(request.json_body["chat_id"], 900)
        self.assertIn("30", request.json_body["text"])
        self.assertIn("4821", request.json_body["text"])
        self.assertIn("46원", request.json_body["text"])
        self.assertIn("fees, tax and slippage", request.json_body["text"])
        self.assertIn("가이드 비중/손실한도", request.json_body["text"])
        self.assertIn("가격 무효화선", request.json_body["text"])
        self.assertIn("모의검증용", request.json_body["text"])
        self.assertIn("실제 주문 전송 없음", request.json_body["text"])

    def test_only_owner_message_can_complete_approval(self) -> None:
        now = datetime(2026, 7, 17, tzinfo=timezone.utc)
        gate = ApprovalGate(
            ExecutionPolicy(), ExecutionState(), owner_user_id=900
        )
        guide = TradeGuide(
            "005930", "buy", 0.25, 0.8, ("approved",), 0.5,
            "below_70000", "approved_strategy", now,
        )
        challenge = gate.propose(
            guide, quantity=1, limit_price=70000, now=now,
            requester_user_id=30, approval_code="4821",
        )
        bot = TelegramBotClient(
            "TOKEN", 900, allowed_user_ids={30},
            transport=FakeTransport(
                [HttpResponse(200, {}, {"ok": True, "result": {}})]
            ),
        )

        member_result = bot.handle_owner_command(
            {
                "update_id": 1,
                "message": {
                    "text": f"승인 {challenge.proposal.proposal_id} 4821",
                    "chat": {"id": 30, "type": "private"},
                    "from": {"id": 30},
                },
            },
            gate,
            now=now,
        )
        self.assertIsNone(member_result)

        owner_result = bot.handle_owner_command(
            {
                "update_id": 2,
                "message": {
                    "text": f"승인 {challenge.proposal.proposal_id} 4821",
                    "chat": {"id": 900, "type": "private"},
                    "from": {"id": 900},
                },
            },
            gate,
            now=now,
        )
        self.assertIsNotNone(owner_result)
        assert owner_result is not None
        self.assertEqual(owner_result.status, "approved")
        self.assertEqual(owner_result.authorized_order.proposal.requester_user_id, 30)

    def test_unapproved_user_is_silently_ignored(self) -> None:
        transport = FakeTransport([])
        bot = TelegramBotClient("TOKEN", 999, transport=transport)
        handled = bot.handle_intent_update(
            {
                "update_id": 10,
                "message": {
                    "text": "hello",
                    "chat": {"id": 30, "type": "private"},
                    "from": {"id": 30},
                },
            }
        )

        self.assertIsNone(handled)
        self.assertEqual(transport.requests, [])

    def test_missing_bot_security_configuration_fails_closed(self) -> None:
        with self.assertRaises(TelegramConfigurationError):
            TelegramBotClient("", 30)
        with self.assertRaises(TelegramConfigurationError):
            TelegramBotClient("TOKEN", 0)

    def test_multi_user_environment_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "TOKEN",
                "TELEGRAM_OWNER_USER_ID": "900",
                "TELEGRAM_ALLOWED_USER_IDS": "30, 40",
                "TELEGRAM_OWNER_CHAT_ID": "900",
            },
            clear=True,
        ):
            bot = TelegramBotClient.from_env(transport=FakeTransport([]))

        self.assertEqual(bot.owner_user_id, 900)
        self.assertEqual(bot.owner_chat_id, 900)
        self.assertEqual(bot.allowed_user_ids, frozenset({30, 40, 900}))


if __name__ == "__main__":
    unittest.main()

