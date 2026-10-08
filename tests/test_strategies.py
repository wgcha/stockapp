import unittest
from dataclasses import replace
from datetime import date
import math

from stock_guide_agent.strategies import (
    PUBLIC_STRATEGY_CATALOG,
    StrategyRecord,
    ValidationReport,
    apply_report,
    evaluate_for_promotion,
)


DEFINITION = PUBLIC_STRATEGY_CATALOG[0]


def passing_report() -> ValidationReport:
    return ValidationReport(
        strategy_id=DEFINITION.strategy_id,
        version=DEFINITION.version,
        market="korean_equities",
        sample_years=8.0,
        out_of_sample=True,
        walk_forward_windows=6,
        trades=300,
        cost_adjusted_sharpe=0.8,
        max_drawdown=0.18,
        parameter_stability=0.75,
        paper_trading_days=60,
        includes_fees_taxes_slippage=True,
        data_leakage_check_passed=True,
        signal_engine="time_series_momentum",
    )


class StrategyRegistryTests(unittest.TestCase):
    def test_long_term_strategy_uses_lower_but_nonzero_turnover_gate(self) -> None:
        definition = next(
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.strategy_id == "long_term_absolute_momentum"
        )
        base = replace(
            passing_report(),
            strategy_id=definition.strategy_id,
            version=definition.version,
            trades=10,
        )

        passing = evaluate_for_promotion(definition, base)
        failing = evaluate_for_promotion(definition, replace(base, trades=9))

        self.assertTrue(passing.eligible)
        self.assertEqual(passing.status, "validated")
        self.assertFalse(failing.eligible)
        self.assertIn("insufficient_trade_count", failing.failures)

    def test_well_validated_strategy_waits_for_owner_approval(self) -> None:
        decision = evaluate_for_promotion(DEFINITION, passing_report())

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.status, "validated")
        self.assertEqual(decision.failures, ())

    def test_backtest_alone_cannot_be_approved(self) -> None:
        report = replace(
            passing_report(),
            out_of_sample=False,
            walk_forward_windows=0,
            paper_trading_days=0,
            includes_fees_taxes_slippage=False,
        )
        decision = evaluate_for_promotion(DEFINITION, report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "paper_trading")
        self.assertIn("missing_out_of_sample_test", decision.failures)
        self.assertIn("insufficient_paper_trading", decision.failures)
        self.assertIn("missing_realistic_costs", decision.failures)

    def test_data_leakage_rejects_strategy(self) -> None:
        report = replace(passing_report(), data_leakage_check_passed=False)
        decision = evaluate_for_promotion(DEFINITION, report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "rejected")
        self.assertIn("data_leakage_risk", decision.failures)

    def test_report_updates_record_and_research_schedule(self) -> None:
        record = StrategyRecord(DEFINITION)
        record.schedule_next_research(date(2026, 7, 17), cadence_days=90)
        decision = apply_report(record, passing_report())

        self.assertEqual(record.status, "validated")
        self.assertTrue(decision.eligible)
        self.assertFalse(record.research_due(date(2026, 10, 14)))
        self.assertTrue(record.research_due(date(2026, 10, 15)))

    def test_catalog_keeps_sources_and_limitations(self) -> None:
        for definition in PUBLIC_STRATEGY_CATALOG:
            self.assertTrue(definition.source.url.startswith("https://"))
            self.assertGreaterEqual(len(definition.limitations), 2)

    def test_famous_investor_principles_are_cited_but_never_auto_executable(self):
        principles = [
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.source.kind == "public_principle"
        ]
        self.assertGreaterEqual(len(principles), 2)
        for definition in principles:
            report = replace(
                passing_report(),
                strategy_id=definition.strategy_id,
                version=definition.version,
            )
            decision = evaluate_for_promotion(definition, report)
            self.assertFalse(decision.eligible)
            self.assertIn("public_principle_requires_testable_rules", decision.failures)
            self.assertIn("strategy_signal_engine_not_implemented", decision.failures)

    def test_unimplemented_academic_signal_cannot_be_promoted_by_generic_report(self):
        definition = next(
            item for item in PUBLIC_STRATEGY_CATALOG if item.strategy_id == "factor_momentum"
        )
        report = replace(
            passing_report(),
            strategy_id=definition.strategy_id,
            version=definition.version,
        )

        decision = evaluate_for_promotion(definition, report)

        self.assertFalse(decision.eligible)
        self.assertIn("strategy_signal_engine_not_implemented", decision.failures)

    def test_momentum_report_cannot_be_relabelled_as_volatility_strategy(self):
        definition = next(
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.strategy_id == "volatility_managed_exposure"
        )
        relabelled = replace(
            passing_report(),
            strategy_id=definition.strategy_id,
            version=definition.version,
        )

        decision = evaluate_for_promotion(definition, relabelled)

        self.assertFalse(decision.eligible)
        self.assertIn("report_signal_engine_mismatch", decision.failures)

    def test_non_finite_or_malformed_metrics_are_rejected_before_thresholds(self):
        for field in (
            "sample_years",
            "cost_adjusted_sharpe",
            "max_drawdown",
            "parameter_stability",
        ):
            for invalid in (math.nan, math.inf, -math.inf, True, "bad", None):
                with self.subTest(field=field, invalid=invalid):
                    decision = evaluate_for_promotion(
                        DEFINITION, replace(passing_report(), **{field: invalid})
                    )
                    self.assertEqual(decision.status, "rejected")
                    self.assertEqual(decision.failures, ("invalid_validation_metrics",))

    def test_numeric_metric_ranges_and_counts_are_validated(self):
        for field, invalid in (
            ("sample_years", -0.1),
            ("sample_years", 10**400),
            ("max_drawdown", -0.01),
            ("max_drawdown", 1.01),
            ("parameter_stability", -0.01),
            ("parameter_stability", 1.01),
            ("walk_forward_windows", True),
            ("walk_forward_windows", 1.5),
            ("walk_forward_windows", -1),
            ("trades", False),
            ("trades", 2.5),
            ("trades", -1),
            ("paper_trading_days", True),
            ("paper_trading_days", 1.5),
            ("paper_trading_days", -1),
            ("out_of_sample", 1),
            ("includes_fees_taxes_slippage", None),
            ("data_leakage_check_passed", "yes"),
        ):
            with self.subTest(field=field, invalid=invalid):
                decision = evaluate_for_promotion(
                    DEFINITION, replace(passing_report(), **{field: invalid})
                )
                self.assertEqual(decision.status, "rejected")
                self.assertEqual(decision.failures, ("invalid_validation_metrics",))


if __name__ == "__main__":
    unittest.main()
