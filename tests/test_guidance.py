import unittest
from datetime import datetime, timezone

from stock_guide_agent.guidance import (
    GuideInput,
    Position,
    format_trade_guide,
    generate_trade_guide,
)
from stock_guide_agent.market import MarketSnapshot


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def market(regime: str = "neutral", score: float = 0.2, confidence: float = 0.8) -> MarketSnapshot:
    return MarketSnapshot(
        as_of=NOW,
        regime=regime,  # type: ignore[arg-type]
        score=score,
        confidence=confidence,
        category_scores={},
        evidence=[],
    )


def guide_input(**changes: object) -> GuideInput:
    values: dict[str, object] = {
        "stock_code": "005930",
        "market": market(),
        "strategy_id": "approved_strategy",
        "strategy_approved": True,
        "signal_score": 0.8,
        "signal_confidence": 0.9,
        "max_trade_loss_pct": 0.5,
        "max_daily_loss_pct": 1.5,
        "daily_pnl_pct": 0.0,
        "invalidation_price": 70000.0,
    }
    values.update(changes)
    return GuideInput(**values)  # type: ignore[arg-type]


class TradeGuideTests(unittest.TestCase):
    def test_zero_personalized_allocation_blocks_new_buy(self) -> None:
        guide = generate_trade_guide(
            guide_input(strategy_allocation_pct=0), now=NOW
        )

        self.assertEqual(guide.action, "hold")
        self.assertEqual(guide.suggested_fraction, 0)
        self.assertIn("no_effective_allocation_for_strategy", guide.reasons)

    def test_first_tranche_scales_to_effective_personalized_allocation(self) -> None:
        guide = generate_trade_guide(
            guide_input(strategy_allocation_pct=40), now=NOW
        )

        self.assertEqual(guide.action, "buy")
        self.assertEqual(guide.suggested_fraction, 0.1)
        self.assertIn("effective_strategy_allocation=40.0000pct", guide.reasons)

    def test_position_exit_is_not_blocked_by_zero_target_allocation(self) -> None:
        guide = generate_trade_guide(
            guide_input(
                position=Position("005930", 10, 75000, 69500),
                strategy_allocation_pct=0,
            ),
            now=NOW,
        )

        self.assertEqual(guide.action, "full_sell")

    def test_support_reference_can_suggest_small_first_tranche(self) -> None:
        guide = generate_trade_guide(
            guide_input(
                market=market(score=0.2),
                signal_score=0.3,
                current_price=70000,
                entry_reference_price=71000,
                take_profit_reference_price=80000,
            ),
            now=NOW,
        )

        self.assertEqual(guide.action, "buy")
        self.assertEqual(guide.suggested_fraction, 0.15)
        self.assertIn("저가 진입 참고선", format_trade_guide(guide))
        self.assertIn("보장하지 않음", format_trade_guide(guide))

    def test_resistance_reference_with_weak_momentum_reduces_partially(self) -> None:
        position = Position("005930", 10, 70000, 80000)
        guide = generate_trade_guide(
            guide_input(
                position=position,
                signal_score=0.1,
                current_price=80000,
                entry_reference_price=70000,
                take_profit_reference_price=79000,
            ),
            now=NOW,
        )

        self.assertEqual(guide.action, "partial_sell")
        self.assertEqual(guide.suggested_fraction, 0.25)

    def test_materially_stronger_alternative_identifies_the_candidate(self) -> None:
        position = Position("005930", 10, 70000, 75000)
        guide = generate_trade_guide(
            guide_input(
                position=position,
                signal_score=0.0,
                alternative_score=0.5,
                alternative_stock_code="000660",
                alternative_confidence=0.8,
            ),
            now=NOW,
        )

        self.assertEqual(guide.action, "review_alternative")
        self.assertEqual(guide.alternative_stock_code, "000660")
        rendered = format_trade_guide(guide)
        self.assertIn("비교 후보: 000660", rendered)
        self.assertIn("현재 +0.00 / 후보 +0.50", rendered)
        self.assertIn("후보 신뢰 80%", rendered)
        self.assertIn("별도 가이드", rendered)

    def test_strong_approved_signal_only_suggests_first_buy_tranche(self) -> None:
        guide = generate_trade_guide(guide_input(), now=NOW)

        self.assertEqual(guide.action, "buy")
        self.assertEqual(guide.suggested_fraction, 0.25)
        self.assertTrue(guide.requires_confirmation)
        self.assertIn("target_return_not_used_as_trade_trigger", guide.reasons)

    def test_invalidation_breach_allows_full_sell(self) -> None:
        position = Position("005930", 10, 75000, 69500)
        guide = generate_trade_guide(guide_input(position=position), now=NOW)

        self.assertEqual(guide.action, "full_sell")
        self.assertEqual(guide.suggested_fraction, 1.0)
        self.assertIn("explicit_invalidation_price_breached", guide.reasons)

    def test_negative_but_not_extreme_signal_reduces_partially(self) -> None:
        position = Position("005930", 10, 75000, 74000)
        guide = generate_trade_guide(
            guide_input(position=position, signal_score=-0.5, invalidation_price=70000),
            now=NOW,
        )

        self.assertEqual(guide.action, "partial_sell")
        self.assertEqual(guide.suggested_fraction, 0.5)

    def test_kill_switch_api_failure_and_daily_limit_stop_trading(self) -> None:
        for changes in (
            {"kill_switch_active": True},
            {"api_healthy": False},
            {"daily_pnl_pct": -1.5},
        ):
            with self.subTest(changes=changes):
                guide = generate_trade_guide(guide_input(**changes), now=NOW)
                self.assertEqual(guide.action, "stop_trading")

    def test_low_confidence_or_unapproved_strategy_cannot_buy(self) -> None:
        low_confidence = generate_trade_guide(
            guide_input(market=market(confidence=0.2)), now=NOW
        )
        unapproved = generate_trade_guide(
            guide_input(strategy_approved=False), now=NOW
        )

        self.assertEqual(low_confidence.action, "hold")
        self.assertEqual(unapproved.action, "hold")

    def test_telegram_text_contains_required_decision_fields(self) -> None:
        text = format_trade_guide(generate_trade_guide(guide_input(), now=NOW))

        self.assertIn("\uc2e0\ub8b0\ub3c4", text)
        self.assertIn("\uc190\uc2e4\ud55c\ub3c4", text)
        self.assertIn("\ubb34\ud6a8\ud654", text)
        self.assertIn("\uadfc\uac70", text)
        self.assertIn("\ud22c\uc790 \uac00\uc774\ub4dc", text)
        self.assertIn("하루 1.5%", text)
        self.assertIn("현재가 70,000원 이하", text)
        self.assertIn("첫 분할진입 조건 충족", text)
        self.assertIn("증권사 앱에서 직접 진행", text)
        self.assertIn("보유정보를 갱신", text)
        self.assertNotIn("주문은 별도 확인", text)
        self.assertNotIn("approved_signal_supports", text)


if __name__ == "__main__":
    unittest.main()
