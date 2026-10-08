import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from stock_guide_agent.execution import (
    ApprovalGate,
    ExecutionPolicy,
    ExecutionState,
    OrderRejected,
)
from stock_guide_agent.guidance import TradeGuide


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
OWNER = 900
REQUESTER = 30


def guide(
    action: str = "buy",
    generated_at: datetime = NOW,
    max_daily_loss_pct: float = 1.5,
) -> TradeGuide:
    return TradeGuide(
        stock_code="005930",
        action=action,  # type: ignore[arg-type]
        suggested_fraction=0.25,
        confidence=0.8,
        reasons=("approved",),
        loss_limit_pct=0.5,
        invalidation_condition="price_below_70000",
        strategy_id="approved_strategy",
        generated_at=generated_at,
        max_daily_loss_pct=max_daily_loss_pct,
    )


class ApprovalGateTests(unittest.TestCase):
    def test_user_loss_limit_is_rechecked_after_proposal_before_owner_approval(self):
        state = ExecutionState(daily_pnl_pct=0, max_daily_loss_pct=5)
        gate = ApprovalGate(
            ExecutionPolicy(), state, owner_user_id=OWNER
        )
        challenge = gate.propose(
            guide(max_daily_loss_pct=1.0),
            quantity=1,
            limit_price=1000,
            now=NOW,
            requester_user_id=REQUESTER,
            approval_code="4821",
        )
        state.daily_pnl_pct = -1.1

        with self.assertRaisesRegex(OrderRejected, "after proposal"):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW,
                approver_user_id=OWNER,
            )

    def test_mock_is_default_and_one_time_approval_authorizes(self) -> None:
        state = ExecutionState()
        gate = ApprovalGate(ExecutionPolicy(), state, owner_user_id=OWNER)
        challenge = gate.propose(
            guide(), quantity=10, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821"
        )
        order = gate.authorize(
            challenge.proposal.proposal_id, "4821", now=NOW, approver_user_id=OWNER
        )

        self.assertEqual(order.proposal.environment, "mock")
        self.assertEqual(order.proposal.side, "buy")
        self.assertEqual(order.proposal.notional, 700000)
        self.assertEqual(state.orders_today, 1)
        with self.assertRaises(OrderRejected):
            gate.authorize(
                challenge.proposal.proposal_id, "4821", now=NOW, approver_user_id=OWNER
            )

    def test_order_count_resets_on_next_korean_trading_date(self) -> None:
        state = ExecutionState()
        gate = ApprovalGate(
            ExecutionPolicy(max_orders_per_day=1), state, owner_user_id=OWNER
        )
        first = gate.propose(
            guide(),
            quantity=1,
            limit_price=1_000,
            now=NOW,
            requester_user_id=REQUESTER,
            approval_code="4821",
        )
        gate.authorize(
            first.proposal.proposal_id, "4821", now=NOW, approver_user_id=OWNER
        )
        next_day = NOW + timedelta(days=1)

        second = gate.propose(
            guide(generated_at=next_day),
            quantity=1,
            limit_price=1_000,
            now=next_day,
            requester_user_id=REQUESTER,
            approval_code="5932",
        )

        self.assertEqual(state.orders_today, 0)
        self.assertEqual(
            state.orders_trading_date, next_day.astimezone(timezone(timedelta(hours=9))).date()
        )
        self.assertIsNotNone(second)

    def test_live_environment_requires_explicit_enable(self) -> None:
        with self.assertRaises(OrderRejected):
            ApprovalGate(
                ExecutionPolicy(environment="live"), ExecutionState(), owner_user_id=OWNER
            )

    def test_non_order_guide_and_stale_guide_are_rejected(self) -> None:
        gate = ApprovalGate(ExecutionPolicy(), ExecutionState(), owner_user_id=OWNER)
        with self.assertRaises(OrderRejected):
            gate.propose(
                guide("hold"), quantity=1, limit_price=70000, now=NOW,
                requester_user_id=REQUESTER,
            )
        with self.assertRaises(OrderRejected):
            gate.propose(
                guide("review_alternative"), quantity=1, limit_price=70000, now=NOW,
                requester_user_id=REQUESTER,
            )
        with self.assertRaises(OrderRejected):
            gate.propose(
                guide(generated_at=NOW - timedelta(minutes=3)),
                quantity=1,
                limit_price=70000,
                now=NOW,
                requester_user_id=REQUESTER,
            )

    def test_order_notional_count_and_expiry_limits(self) -> None:
        state = ExecutionState()
        gate = ApprovalGate(
            ExecutionPolicy(max_order_notional=100000, max_orders_per_day=1), state,
            owner_user_id=OWNER,
        )
        with self.assertRaises(OrderRejected):
            gate.propose(
                guide(), quantity=2, limit_price=70000, now=NOW,
                requester_user_id=REQUESTER,
            )

        challenge = gate.propose(
            guide(), quantity=1, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821"
        )
        with self.assertRaises(OrderRejected):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW + timedelta(minutes=4),
                approver_user_id=OWNER,
            )

    def test_kill_switch_api_failure_and_loss_limit_fail_closed(self) -> None:
        states = [
            ExecutionState(kill_switch_active=True),
            ExecutionState(api_healthy=False),
            ExecutionState(daily_pnl_pct=-1.5),
        ]
        for state in states:
            with self.subTest(state=state):
                gate = ApprovalGate(
                    ExecutionPolicy(), state, owner_user_id=OWNER
                )
                with self.assertRaises(OrderRejected):
                    gate.propose(
                        guide(), quantity=1, limit_price=70000, now=NOW,
                        requester_user_id=REQUESTER,
                    )

    def test_only_owner_can_approve_another_users_order(self) -> None:
        gate = ApprovalGate(ExecutionPolicy(), ExecutionState(), owner_user_id=OWNER)
        challenge = gate.propose(
            guide(), quantity=1, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821",
        )

        self.assertEqual(challenge.approver_user_id, OWNER)
        self.assertEqual(challenge.proposal.requester_user_id, REQUESTER)
        with self.assertRaisesRegex(OrderRejected, "only the configured owner"):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW,
                approver_user_id=REQUESTER,
            )
        order = gate.authorize(
            challenge.proposal.proposal_id,
            "4821",
            now=NOW,
            approver_user_id=OWNER,
        )
        self.assertEqual(order.proposal.requester_user_id, REQUESTER)

    def test_only_owner_can_reject_and_control_kill_switch(self) -> None:
        state = ExecutionState()
        gate = ApprovalGate(ExecutionPolicy(), state, owner_user_id=OWNER)
        challenge = gate.propose(
            guide(), quantity=1, limit_price=70000, now=NOW,
            requester_user_id=REQUESTER, approval_code="4821",
        )
        with self.assertRaises(OrderRejected):
            gate.reject(
                challenge.proposal.proposal_id,
                now=NOW,
                approver_user_id=REQUESTER,
            )
        gate.reject(
            challenge.proposal.proposal_id,
            now=NOW,
            approver_user_id=OWNER,
        )
        with self.assertRaises(OrderRejected):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW,
                approver_user_id=OWNER,
            )
        with self.assertRaises(OrderRejected):
            gate.activate_kill_switch(actor_user_id=REQUESTER, reason="not owner")
        gate.activate_kill_switch(actor_user_id=OWNER, reason="owner stop")
        self.assertTrue(state.kill_switch_active)
        with self.assertRaises(OrderRejected):
            gate.reset_kill_switch(
                actor_user_id=REQUESTER, confirmation="RESET KILL SWITCH"
            )
        gate.reset_kill_switch(
            actor_user_id=OWNER, confirmation="RESET KILL SWITCH"
        )
        self.assertFalse(state.kill_switch_active)

    def test_kill_switch_reset_requires_exact_phrase(self) -> None:
        state = ExecutionState()
        state.activate_kill_switch("manual emergency")
        with self.assertRaises(OrderRejected):
            state.reset_kill_switch("reset")
        state.reset_kill_switch("RESET KILL SWITCH")
        self.assertFalse(state.kill_switch_active)

    def test_live_buy_requires_equity_numeric_stop_and_respects_allocation(self) -> None:
        policy = ExecutionPolicy(environment="live", live_trading_enabled=True)
        without_equity = ApprovalGate(policy, ExecutionState(), owner_user_id=OWNER)
        with self.assertRaisesRegex(OrderRejected, "account equity"):
            without_equity.propose(
                guide(), quantity=1, limit_price=10_000, now=NOW,
                requester_user_id=REQUESTER,
            )

        state = ExecutionState(account_equity=1_000_000)
        gate = ApprovalGate(policy, state, owner_user_id=OWNER)
        no_stop = guide()
        with self.assertRaisesRegex(OrderRejected, "numeric invalidation"):
            gate.propose(
                no_stop, quantity=1, limit_price=10_000, now=NOW,
                requester_user_id=REQUESTER,
            )
        safe = replace(no_stop, invalidation_price=9_900)
        with self.assertRaisesRegex(OrderRejected, "guided allocation"):
            gate.propose(
                safe, quantity=26, limit_price=10_000, now=NOW,
                requester_user_id=REQUESTER,
            )

        with self.assertRaisesRegex(OrderRejected, "transaction cost"):
            gate.propose(
                safe, quantity=1, limit_price=10_000, now=NOW,
                requester_user_id=REQUESTER,
            )

    def test_buy_loss_budget_is_checked_again_when_equity_falls(self) -> None:
        state = ExecutionState(account_equity=1_000_000)
        gate = ApprovalGate(ExecutionPolicy(), state, owner_user_id=OWNER)
        safe = replace(guide(), invalidation_price=9_000)
        challenge = gate.propose(
            safe,
            quantity=5,
            limit_price=10_000,
            now=NOW,
            requester_user_id=REQUESTER,
            approval_code="4821",
        )
        state.account_equity = 500_000

        with self.assertRaisesRegex(OrderRejected, "loss budget"):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW,
                approver_user_id=OWNER,
            )

    def test_sell_cannot_exceed_holding_or_guided_partial_fraction(self) -> None:
        gate = ApprovalGate(ExecutionPolicy(), ExecutionState(), owner_user_id=OWNER)
        partial = replace(guide("partial_sell"), suggested_fraction=0.5)
        with self.assertRaisesRegex(OrderRejected, "recorded holding"):
            gate.propose(
                partial, quantity=1, limit_price=70_000, now=NOW,
                requester_user_id=REQUESTER,
            )
        with self.assertRaisesRegex(OrderRejected, "partial reduction"):
            gate.propose(
                partial, quantity=6, limit_price=70_000, now=NOW,
                requester_user_id=REQUESTER, owned_quantity=10,
            )
        challenge = gate.propose(
            partial, quantity=5, limit_price=70_000, now=NOW,
            requester_user_id=REQUESTER, owned_quantity=10,
        )
        self.assertEqual(challenge.proposal.owned_quantity_at_proposal, 10)

    def test_account_equity_source_is_user_scoped_and_rechecked(self) -> None:
        balances = {30: 1_000_000.0, 40: 10_000_000.0}
        gate = ApprovalGate(
            ExecutionPolicy(),
            ExecutionState(),
            owner_user_id=OWNER,
            account_equity_source=balances.get,
        )
        safe = replace(guide(), invalidation_price=9_900)
        with self.assertRaisesRegex(OrderRejected, "guided allocation"):
            gate.propose(
                safe, quantity=26, limit_price=10_000, now=NOW,
                requester_user_id=30,
            )
        challenge = gate.propose(
            safe, quantity=26, limit_price=10_000, now=NOW,
            requester_user_id=40, approval_code="4821",
        )
        self.assertEqual(challenge.proposal.account_equity_at_proposal, 10_000_000)
        balances[40] = 500_000
        with self.assertRaisesRegex(OrderRejected, "guided allocation"):
            gate.authorize(
                challenge.proposal.proposal_id,
                "4821",
                now=NOW,
                approver_user_id=OWNER,
            )


if __name__ == "__main__":
    unittest.main()
