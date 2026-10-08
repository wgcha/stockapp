import math
import unittest
from dataclasses import replace
from datetime import date, timedelta

from stock_guide_agent.backtest import (
    DailyBar,
    TradingCostProfile,
    mirae_krx_2026_costs,
    run_long_cash_momentum,
    run_volatility_managed_exposure,
    toss_krx_2026_costs,
    walk_forward_momentum,
    walk_forward_volatility_managed,
)


def bars(count: int = 2100) -> list[DailyBar]:
    start = date(2017, 1, 2)
    result = []
    price = 100.0
    day = start
    for index in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        drift = 0.0007 if (index // 300) % 2 == 0 else -0.0001
        price *= 1 + drift + 0.004 * math.sin(index / 17)
        result.append(DailyBar(day, price))
        day += timedelta(days=1)
    return result


class BacktestTests(unittest.TestCase):
    def test_daily_bars_reject_non_finite_and_non_real_prices(self) -> None:
        for value in (
            math.nan,
            math.inf,
            -math.inf,
            True,
            "100",
            10**10000,
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "finite positive number") as caught:
                    DailyBar(date(2026, 1, 2), value)
                self.assertEqual(
                    str(caught.exception),
                    "adjusted_close must be a finite positive number",
                )

    def test_all_cost_rates_reject_non_finite_and_non_real_values(self) -> None:
        profile = toss_krx_2026_costs()
        invalid_values = (math.nan, math.inf, -math.inf, True, "0.001", 10**10000)
        fields = (
            "buy_commission_rate",
            "sell_commission_rate",
            "sell_tax_rate",
            "slippage_rate_per_side",
        )
        for field in fields:
            for value in invalid_values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, "finite numbers"):
                        replace(profile, **{field: value})

        zero_costs = replace(
            profile,
            buy_commission_rate=0.0,
            sell_commission_rate=0.0,
            sell_tax_rate=0.0,
            slippage_rate_per_side=0.0,
        )
        self.assertIsInstance(zero_costs, TradingCostProfile)
        zero_cost_result = walk_forward_momentum(
            [
                DailyBar(date(2026, 1, 2) + timedelta(days=index), 100 + index)
                for index in range(40)
            ],
            candidate_lookbacks=(2,),
            costs=zero_costs,
            train_days=10,
            test_days=20,
        )
        self.assertFalse(zero_cost_result.includes_fees_taxes_slippage)

    def test_malformed_price_cannot_reach_a_backtest_engine(self) -> None:
        costs = toss_krx_2026_costs(project_outside_effective_period=True)
        with self.assertRaisesRegex(ValueError, "finite positive number"):
            malformed = DailyBar(date(2026, 1, 2), math.nan)
            run_long_cash_momentum(
                [malformed] * 5, lookback=2, costs=costs
            )

    def test_finite_extreme_prices_cannot_return_infinite_results(self) -> None:
        data = [
            DailyBar(date(2026, 1, 2) + timedelta(days=index), price)
            for index, price in enumerate((1e-301, 1e-300, 1e-299, 1e300, 1e300))
        ]
        with self.assertRaisesRegex(ValueError, "backtest results must be finite"):
            run_long_cash_momentum(
                data,
                lookback=2,
                costs=toss_krx_2026_costs(
                    project_outside_effective_period=True
                ),
            )

    def test_volatility_overlay_is_lagged_unlevered_and_costed(self) -> None:
        data = bars(500)
        costs = toss_krx_2026_costs(project_outside_effective_period=True)
        original = run_volatility_managed_exposure(
            data,
            volatility_lookback=20,
            annual_target_volatility=0.10,
            rebalance_threshold=0.02,
            costs=costs,
        )
        changed = data[:-1] + [
            DailyBar(data[-1].trading_date, data[-1].adjusted_close * 2)
        ]
        mutated = run_volatility_managed_exposure(
            changed,
            volatility_lookback=20,
            annual_target_volatility=0.10,
            rebalance_threshold=0.02,
            costs=costs,
        )

        self.assertTrue(all(0 <= value <= 1 for value in original.exposures))
        self.assertGreater(original.trades, 0)
        self.assertGreater(original.total_cost_rate, 0)
        self.assertEqual(original.daily_returns[:-1], mutated.daily_returns[:-1])
        self.assertEqual(original.exposures, mutated.exposures)
        self.assertNotEqual(original.daily_returns[-1], mutated.daily_returns[-1])

    def test_volatility_walk_forward_selects_only_declared_parameters(self) -> None:
        result = walk_forward_volatility_managed(
            bars(),
            candidate_lookbacks=(20, 40),
            candidate_target_volatilities=(0.10, 0.15),
            rebalance_threshold=0.05,
            costs=toss_krx_2026_costs(project_outside_effective_period=True),
        )

        self.assertGreaterEqual(result.windows, 3)
        self.assertTrue(set(result.selected_lookbacks) <= {20, 40})
        self.assertTrue(
            set(result.selected_target_volatilities) <= {0.10, 0.15}
        )
        self.assertEqual(
            len(result.selected_lookbacks), len(result.selected_target_volatilities)
        )
        self.assertTrue(result.includes_fees_taxes_slippage)

    def test_signal_is_lagged_and_all_cost_components_are_charged(self) -> None:
        data = [
            DailyBar(date(2026, 1, 2) + timedelta(days=i), value)
            for i, value in enumerate((100, 101, 102, 103, 104, 100, 99, 98))
        ]
        cost = toss_krx_2026_costs(slippage_bps=5)
        result = run_long_cash_momentum(data, lookback=2, costs=cost)

        self.assertGreaterEqual(result.trades, 2)
        expected_round_trip = (
            cost.buy_commission_rate
            + cost.sell_commission_rate
            + cost.sell_tax_rate
            + 2 * cost.slippage_rate_per_side
        )
        self.assertGreaterEqual(result.total_cost_rate, expected_round_trip)

    def test_walk_forward_generates_true_oos_report_but_not_paper_days(self) -> None:
        result = walk_forward_momentum(
            bars(),
            candidate_lookbacks=(20, 60, 120),
            costs=toss_krx_2026_costs(project_outside_effective_period=True),
            train_days=756,
            test_days=252,
        )
        report = result.to_validation_report(
            strategy_id="time_series_momentum",
            version="1.0.0",
            market="korean_equities",
            paper_trading_days=0,
            signal_engine="time_series_momentum",
        )

        self.assertGreaterEqual(result.windows, 3)
        self.assertTrue(report.out_of_sample)
        self.assertTrue(report.data_leakage_check_passed)
        self.assertTrue(report.includes_fees_taxes_slippage)
        self.assertEqual(report.paper_trading_days, 0)
        self.assertEqual(report.walk_forward_windows, result.windows)

    def test_future_price_change_does_not_rewrite_earlier_returns(self) -> None:
        data = bars(500)
        original = run_long_cash_momentum(
            data, lookback=60,
            costs=toss_krx_2026_costs(project_outside_effective_period=True),
        )
        changed = data[:-1] + [DailyBar(data[-1].trading_date, data[-1].adjusted_close * 2)]
        mutated = run_long_cash_momentum(
            changed, lookback=60,
            costs=toss_krx_2026_costs(project_outside_effective_period=True),
        )

        self.assertEqual(original.daily_returns[:-1], mutated.daily_returns[:-1])
        self.assertNotEqual(original.daily_returns[-1], mutated.daily_returns[-1])

    def test_mirae_requires_explicit_customer_commission(self) -> None:
        profile = mirae_krx_2026_costs(commission_rate=0.00014)
        self.assertEqual(profile.provider, "mirae_asset")
        self.assertIn("commission_rate_supplied_from_user_contract", profile.assumptions)

    def test_cost_profile_cannot_be_used_outside_effective_period(self) -> None:
        historical = [
            DailyBar(date(2025, 1, 1) + timedelta(days=i), 100 + i)
            for i in range(10)
        ]
        with self.assertRaisesRegex(ValueError, "before cost profile"):
            run_long_cash_momentum(
                historical, lookback=2, costs=toss_krx_2026_costs()
            )


if __name__ == "__main__":
    unittest.main()
