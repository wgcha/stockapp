import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from stock_guide_agent.intent import parse_investment_intent
from stock_guide_agent.market import MarketSnapshot
from stock_guide_agent.portfolio import (
    RiskOverlaySignal,
    compose_strategy_plan,
    format_strategy_plan,
)
from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, StrategyRecord


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def market(regime: str = "neutral", confidence: float = 0.8) -> MarketSnapshot:
    return MarketSnapshot(
        as_of=NOW,
        regime=regime,  # type: ignore[arg-type]
        score=0.0,
        confidence=confidence,
        category_scores={},
        evidence=[],
    )


class PersonalizedStrategyPlanTests(unittest.TestCase):
    def test_real_long_term_strategy_serves_long_term_bucket_only_after_approval(self) -> None:
        intent = parse_investment_intent("장기안정 60% 단타 40%")
        definition = next(
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.strategy_id == "long_term_absolute_momentum"
        )

        waiting = compose_strategy_plan(
            intent, [StrategyRecord(definition, status="validated")], market()
        )
        active = compose_strategy_plan(
            intent, [StrategyRecord(definition, status="approved")], market()
        )

        self.assertEqual(waiting.invested_weight_pct, 0)
        self.assertEqual(waiting.cash_weight_pct, 100)
        self.assertEqual(active.invested_weight_pct, 60)
        self.assertEqual(active.cash_weight_pct, 40)
        self.assertEqual(active.allocations[0].strategy_id, definition.strategy_id)
        self.assertEqual(active.attributions[0].source_url, definition.source.url)

    def test_plan_formatter_exposes_source_market_limit_and_freshness(self) -> None:
        intent = parse_investment_intent("스윙 100%")
        definition = next(
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.strategy_id == "time_series_momentum"
        )
        record = StrategyRecord(
            definition,
            status="approved",
            last_researched_on=date(2026, 7, 1),
        )

        rendered = format_strategy_plan(
            compose_strategy_plan(intent, [record], market())
        )

        self.assertIn("Time Series Momentum (2012)", rendered)
        self.assertIn("글로벌 선물·주가지수", rendered)
        self.assertIn("최근확인 2026-07-01", rendered)
        self.assertIn("한국 적용 한계", rendered)
        self.assertIn(definition.source.url, rendered)

    def test_approved_risk_overlay_reduces_allocations_and_moves_remainder_to_cash(self) -> None:
        intent = parse_investment_intent("스윙 100%")
        swing = StrategyRecord(PUBLIC_STRATEGY_CATALOG[0], status="approved")
        volatility = StrategyRecord(
            next(
                definition
                for definition in PUBLIC_STRATEGY_CATALOG
                if definition.strategy_id == "volatility_managed_exposure"
            ),
            status="approved",
        )

        plan = compose_strategy_plan(
            intent,
            [swing, volatility],
            market(),
            risk_overlay_signals=(
                RiskOverlaySignal(
                    strategy_id="volatility_managed_exposure",
                    version=volatility.definition.version,
                    exposure_multiplier=0.4,
                ),
            ),
        )

        self.assertEqual(plan.invested_weight_pct, 40.0)
        self.assertEqual(plan.cash_weight_pct, 60.0)
        self.assertIsNotNone(plan.risk_overlay)
        self.assertIn("approved_volatility_risk_overlay_applied", plan.warnings)
        self.assertIn("위험조정 40%", format_strategy_plan(plan))

    def test_unapproved_risk_overlay_has_no_allocation_effect(self) -> None:
        intent = parse_investment_intent("스윙 100%")
        swing = StrategyRecord(PUBLIC_STRATEGY_CATALOG[0], status="approved")
        volatility = StrategyRecord(
            next(
                definition
                for definition in PUBLIC_STRATEGY_CATALOG
                if definition.strategy_id == "volatility_managed_exposure"
            )
        )

        plan = compose_strategy_plan(
            intent,
            [swing, volatility],
            market(),
            risk_overlay_signals=(
                RiskOverlaySignal(
                    strategy_id="volatility_managed_exposure",
                    version=volatility.definition.version,
                    exposure_multiplier=0.4,
                ),
            ),
        )

        self.assertEqual(plan.invested_weight_pct, 100.0)
        self.assertEqual(plan.cash_weight_pct, 0.0)
        self.assertIsNone(plan.risk_overlay)

    def test_plan_formatter_exposes_cash_and_missing_approval(self) -> None:
        intent = parse_investment_intent("장기 100%, 연간 8% 목표")
        plan = compose_strategy_plan(intent, [], market())

        rendered = format_strategy_plan(plan)

        self.assertIn("현금·미배정: 100%", rendered)
        self.assertIn("승인 전략이 없어", rendered)

    def test_unapproved_strategy_is_never_allocated(self) -> None:
        intent = parse_investment_intent("\uc7a5\uae30 60% \ub2e8\ud0c0 40%")
        records = [StrategyRecord(item) for item in PUBLIC_STRATEGY_CATALOG]
        plan = compose_strategy_plan(intent, records, market())

        self.assertEqual(plan.invested_weight_pct, 0.0)
        self.assertEqual(plan.cash_weight_pct, 100.0)
        self.assertIn("no_approved_strategy_for_long_term", plan.warnings)
        self.assertIn("no_approved_strategy_for_intraday", plan.warnings)

    def test_only_matching_approved_strategy_is_allocated(self) -> None:
        intent = parse_investment_intent("\uc7a5\uae30 60% \uc2a4\uc719 40%")
        long_term = StrategyRecord(
            replace(
                PUBLIC_STRATEGY_CATALOG[0],
                strategy_id="long_term_momentum",
                horizon="long_term",
            ),
            status="approved",
        )
        swing = StrategyRecord(PUBLIC_STRATEGY_CATALOG[0], status="approved")
        plan = compose_strategy_plan(intent, [long_term, swing], market())

        self.assertEqual(plan.invested_weight_pct, 100.0)
        self.assertEqual(plan.cash_weight_pct, 0.0)
        self.assertEqual(
            {item.bucket: item.weight_pct for item in plan.allocations},
            {"long_term": 60.0, "swing": 40.0},
        )

    def test_low_market_confidence_forces_cash(self) -> None:
        intent = parse_investment_intent("\uc7a5\uae30 100%")
        record = StrategyRecord(
            replace(PUBLIC_STRATEGY_CATALOG[0], horizon="long_term"),
            status="approved",
        )
        plan = compose_strategy_plan(intent, [record], market(confidence=0.2))

        self.assertEqual(plan.cash_weight_pct, 100.0)
        self.assertIn("insufficient_market_confidence_no_new_positions", plan.warnings)

    def test_risk_off_disables_intraday_bucket(self) -> None:
        intent = parse_investment_intent("\ub2e8\ud0c0 100%")
        intraday_definition = PUBLIC_STRATEGY_CATALOG[0]
        intraday_definition = type(intraday_definition)(
            **{**intraday_definition.__dict__, "horizon": "intraday"}
        )
        record = StrategyRecord(intraday_definition, status="approved")
        plan = compose_strategy_plan(intent, [record], market(regime="risk_off"))

        self.assertEqual(plan.cash_weight_pct, 100.0)
        self.assertIn("intraday_disabled_in_risk_off_regime", plan.warnings)


if __name__ == "__main__":
    unittest.main()
