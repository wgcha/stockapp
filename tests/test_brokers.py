import unittest
from datetime import datetime, timezone

from stock_guide_agent.brokers import BrokerRouter, MiraeAssetBrokerGateway
from stock_guide_agent.execution import (
    ApprovalGate,
    ExecutionPolicy,
    ExecutionState,
    OrderRejected,
)
from stock_guide_agent.guidance import TradeGuide


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def authorized_order(environment="mock"):
    policy = ExecutionPolicy(
        environment=environment,
        live_trading_enabled=environment == "live",
    )
    gate = ApprovalGate(
        policy,
        ExecutionState(account_equity=10_000_000 if environment == "live" else None),
        owner_user_id=900,
    )
    guide = TradeGuide(
        "005930", "buy", 0.25, 0.8, ("approved",), 0.5, "below_70000",
        "approved_strategy", NOW, invalidation_price=65_000,
    )
    challenge = gate.propose(
        guide,
        quantity=1,
        limit_price=70000,
        now=NOW,
        requester_user_id=30,
        broker_provider="mirae_asset",
        approval_code="4821",
        estimated_transaction_cost=45.5,
        transaction_costs_complete=True,
    )
    return gate.authorize(
        challenge.proposal.proposal_id,
        "4821",
        now=NOW,
        approver_user_id=900,
    )


class BrokerGatewayTests(unittest.TestCase):
    def test_mirae_asset_mock_order_is_supported(self) -> None:
        receipt = BrokerRouter([MiraeAssetBrokerGateway()]).submit_authorized_order(
            authorized_order()
        )
        self.assertEqual(receipt.provider, "mirae_asset")
        self.assertTrue(receipt.simulated)

    def test_mirae_asset_live_order_fails_closed_without_contract_adapter(self) -> None:
        with self.assertRaisesRegex(OrderRejected, "guidance only"):
            BrokerRouter([MiraeAssetBrokerGateway()]).submit_authorized_order(
                authorized_order("live")
            )

    def test_mirae_asset_live_order_is_rejected_even_with_adapter(self) -> None:
        calls = []

        class Adapter:
            def submit_authorized_order(self, order):
                calls.append(order)
                return {"status": "SENT"}

        with self.assertRaisesRegex(OrderRejected, "guidance only"):
            BrokerRouter([MiraeAssetBrokerGateway(live_adapter=Adapter())]).submit_authorized_order(
                authorized_order("live")
            )
        self.assertEqual(calls, [])

    def test_unknown_provider_is_rejected_at_proposal_creation(self) -> None:
        gate = ApprovalGate(ExecutionPolicy(), ExecutionState(), owner_user_id=900)
        guide = TradeGuide(
            "005930", "buy", 0.25, 0.8, ("approved",), 0.5, "below_70000",
            "approved_strategy", NOW,
        )
        with self.assertRaisesRegex(OrderRejected, "unsupported broker"):
            gate.propose(
                guide,
                quantity=1,
                limit_price=70000,
                now=NOW,
                requester_user_id=30,
                broker_provider="unknown",
            )


if __name__ == "__main__":
    unittest.main()
