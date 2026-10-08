import unittest

from stock_guide_agent.execution import (
    ApprovalGate,
    ExecutionPolicy,
    ExecutionState,
    OrderRejected,
)
from stock_guide_agent.guidance import TradeGuide
from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.toss import (
    TossInvestApiError,
    TossInvestClient,
    TossInvestConfigurationError,
    TossInvestToken,
)
from datetime import datetime, timezone


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
OWNER = 900
REQUESTER = 30


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class TossInvestClientTests(unittest.TestCase):
    def test_candle_history_follows_cursor_deduplicates_and_sorts_oldest_first(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    {
                        "result": {
                            "candles": [
                                {"timestamp": "2026-07-17T09:00:00+09:00"},
                                {"timestamp": "2026-07-16T09:00:00+09:00"},
                            ],
                            "nextBefore": "2026-07-16T09:00:00+09:00",
                        }
                    },
                ),
                HttpResponse(
                    200,
                    {},
                    {
                        "result": {
                            "candles": [
                                {"timestamp": "2026-07-16T09:00:00+09:00"},
                                {"timestamp": "2026-07-15T09:00:00+09:00"},
                            ],
                            "nextBefore": None,
                        }
                    },
                ),
            ]
        )
        client = TossInvestClient("CLIENT", "SECRET", transport=transport)

        candles = client.get_candle_history(
            "KOSPI", TossInvestToken("TOKEN", "Bearer", 3600), max_count=10
        )

        self.assertEqual(
            [item["timestamp"] for item in candles],
            [
                "2026-07-15T09:00:00+09:00",
                "2026-07-16T09:00:00+09:00",
                "2026-07-17T09:00:00+09:00",
            ],
        )
        self.assertNotIn("before", transport.requests[0].query)
        self.assertEqual(
            transport.requests[1].query["before"],
            "2026-07-16T09:00:00+09:00",
        )

    def test_candle_history_rejects_non_advancing_cursor(self) -> None:
        response = HttpResponse(
            200,
            {},
            {
                "result": {
                    "candles": [{"timestamp": "2026-07-17T09:00:00+09:00"}],
                    "nextBefore": "2026-07-17T09:00:00+09:00",
                }
            },
        )
        client = TossInvestClient(
            "CLIENT", "SECRET", transport=FakeTransport([response, response])
        )

        with self.assertRaisesRegex(TossInvestApiError, "cursor did not advance"):
            client.get_candle_history(
                "KOSPI", TossInvestToken("TOKEN", "Bearer", 3600), max_count=10
            )

    def test_oauth_uses_official_client_credentials_form(self) -> None:
        transport = FakeTransport(
            [HttpResponse(200, {}, {"access_token": "TOKEN", "token_type": "Bearer", "expires_in": 3600})]
        )
        client = TossInvestClient("CLIENT", "SECRET", transport=transport)
        token = client.issue_token()

        self.assertEqual(token.access_token, "TOKEN")
        request = transport.requests[0]
        self.assertEqual(request.url, "https://openapi.tossinvest.com/oauth2/token")
        self.assertEqual(request.form_body["grant_type"], "client_credentials")
        self.assertEqual(request.redacted()["form_body"]["client_secret"], "***")

    def test_market_data_and_investor_trading_use_toss_endpoints(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(200, {}, {"result": []}),
                HttpResponse(200, {}, {"result": {"records": []}}),
                HttpResponse(200, {}, {"result": {"candles": []}}),
            ]
        )
        client = TossInvestClient("CLIENT", "SECRET", transport=transport)
        token = TossInvestToken("TOKEN", "Bearer", 3600)
        client.get_prices(["005930"], token)
        client.get_investor_trading("KOSPI", token)
        client.get_candles("005930", token, count=60)

        self.assertEqual(transport.requests[0].query["symbols"], "005930")
        self.assertTrue(transport.requests[0].url.endswith("/api/v1/prices"))
        self.assertTrue(transport.requests[1].url.endswith("/KOSPI/investor-trading"))
        self.assertEqual(transport.requests[1].query["interval"], "1d")
        self.assertEqual(transport.requests[1].query["count"], "10")
        self.assertEqual(transport.requests[2].query["adjusted"], "true")
        self.assertIn("Bearer TOKEN", transport.requests[0].headers["Authorization"])

    def test_holdings_require_account_header(self) -> None:
        client = TossInvestClient("CLIENT", "SECRET", transport=FakeTransport([]))
        with self.assertRaises(TossInvestConfigurationError):
            client.get_holdings(TossInvestToken("TOKEN", "Bearer", None))

    def test_mock_authorized_order_is_simulated_without_network(self) -> None:
        guide = TradeGuide(
            stock_code="005930",
            action="buy",
            suggested_fraction=0.25,
            confidence=0.8,
            reasons=("approved",),
            loss_limit_pct=0.5,
            invalidation_condition="below_70000",
            strategy_id="approved_strategy",
            generated_at=NOW,
        )
        gate = ApprovalGate(
            ExecutionPolicy(), ExecutionState(), owner_user_id=OWNER
        )
        challenge = gate.propose(
            guide, quantity=1, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821",
            estimated_transaction_cost=45.5, transaction_costs_complete=True,
        )
        order = gate.authorize(
            challenge.proposal.proposal_id, "4821", now=NOW,
            approver_user_id=OWNER,
        )
        transport = FakeTransport([])
        client = TossInvestClient("CLIENT", "SECRET", transport=transport)
        receipt = client.submit_authorized_order(order, TossInvestToken("TOKEN", "Bearer", None))

        self.assertTrue(receipt.simulated)
        self.assertEqual(receipt.payload["status"], "SIMULATED")
        self.assertEqual(transport.requests, [])

    def test_live_order_is_rejected_even_when_enabled(self) -> None:
        policy = ExecutionPolicy(environment="live", live_trading_enabled=True)
        gate = ApprovalGate(
            policy, ExecutionState(account_equity=10_000_000), owner_user_id=OWNER
        )
        guide = TradeGuide(
            "005930", "buy", 0.25, 0.8, ("approved",), 0.5, "below_70000",
            "approved_strategy", NOW, invalidation_price=65_000
        )
        challenge = gate.propose(
            guide, quantity=1, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821",
            estimated_transaction_cost=45.5, transaction_costs_complete=True,
        )
        order = gate.authorize(
            challenge.proposal.proposal_id, "4821", now=NOW,
            approver_user_id=OWNER,
        )
        client = TossInvestClient(
            "CLIENT", "SECRET", account_sequence="1", allow_live_orders=False
        )
        with self.assertRaises(OrderRejected):
            client.submit_authorized_order(order, TossInvestToken("TOKEN", "Bearer", None))

    def test_live_order_never_calls_external_order_endpoint(self) -> None:
        policy = ExecutionPolicy(environment="live", live_trading_enabled=True)
        gate = ApprovalGate(
            policy, ExecutionState(account_equity=10_000_000), owner_user_id=OWNER
        )
        guide = TradeGuide(
            "005930", "buy", 0.25, 0.8, ("approved",), 0.5, "below_70000",
            "approved_strategy", NOW, invalidation_price=65_000,
        )
        challenge = gate.propose(
            guide, quantity=2, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821",
            estimated_transaction_cost=91.0, transaction_costs_complete=True,
        )
        order = gate.authorize(
            challenge.proposal.proposal_id, "4821", now=NOW,
            approver_user_id=OWNER,
        )
        transport = FakeTransport([])
        client = TossInvestClient(
            "CLIENT", "SECRET", account_sequence="1", allow_live_orders=True,
            transport=transport,
        )

        with self.assertRaises(OrderRejected):
            client.submit_authorized_order(
                order, TossInvestToken("TOKEN", "Bearer", None)
            )
        self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
