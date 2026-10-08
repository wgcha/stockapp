import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from stock_guide_agent.backtest import (
    TradingCostProfile,
    run_opening_range_breakout,
    toss_krx_2026_costs,
    walk_forward_opening_range_breakout,
)
from stock_guide_agent.intraday import IntradayBar


KST = timezone(timedelta(hours=9))


def session(
    trading_date: date,
    *,
    next_open: float = 103.0,
    final_close: float = 110.0,
    stop_gap: bool = False,
    minutes: int = 40,
) -> list[IntradayBar]:
    start = datetime.combine(trading_date, datetime.min.time(), KST).replace(hour=9)
    bars: list[IntradayBar] = []
    for index in range(minutes):
        if index < 5:
            open_price, high, low, close = 100.0, 101.0, 99.0, 100.0
        elif index == 5:
            open_price, high, low, close = 100.5, 102.5, 100.0, 102.0
        elif index == 6:
            open_price = next_open
            high, low, close = max(next_open, 104.0), 100.0, 103.0
        elif stop_gap and index == 7:
            open_price, high, low, close = 95.0, 96.0, 94.0, 95.0
        else:
            progress = (index - 6) / max(1, minutes - 7)
            close = 103.0 + (final_close - 103.0) * progress
            open_price, high, low = close, close + 0.5, close - 0.5
        bars.append(
            IntradayBar(
                "005930",
                start + timedelta(minutes=index),
                open_price,
                high,
                low,
                close,
                1000 + index,
            )
        )
    return bars


def zero_costs() -> TradingCostProfile:
    return TradingCostProfile(
        "test",
        "KRX",
        0,
        0,
        0,
        0,
        date(2020, 1, 1),
        None,
        ("https://example.test/costs",),
    )


class OpeningRangeBacktestTests(unittest.TestCase):
    def test_breakout_enters_next_bar_and_charges_all_costs(self) -> None:
        day = session(date(2026, 7, 17))
        free = run_opening_range_breakout(
            day, opening_range_minutes=5, costs=zero_costs()
        )
        costed = run_opening_range_breakout(
            day,
            opening_range_minutes=5,
            costs=toss_krx_2026_costs(slippage_bps=5),
        )

        self.assertEqual(free.trades, 1)
        self.assertAlmostEqual(free.daily_returns[0], 110 / 103 - 1, places=8)
        self.assertLess(costed.daily_returns[0], free.daily_returns[0])
        self.assertGreater(costed.total_cost_rate, 0)

    def test_stop_gap_uses_worse_open_and_future_day_does_not_rewrite_past(self) -> None:
        first = session(date(2026, 7, 16), stop_gap=True)
        second = session(date(2026, 7, 17), final_close=110)
        baseline = run_opening_range_breakout(
            first + second, opening_range_minutes=5, costs=zero_costs()
        )
        changed_future = [
            replace(bar, close_price=bar.close_price * 1.2, high_price=bar.high_price * 1.2)
            if bar.trading_date == date(2026, 7, 17) and bar.timestamp.minute == 39
            else bar
            for bar in first + second
        ]
        changed = run_opening_range_breakout(
            changed_future, opening_range_minutes=5, costs=zero_costs()
        )

        self.assertAlmostEqual(baseline.daily_returns[0], 95 / 103 - 1, places=8)
        self.assertEqual(baseline.daily_returns[0], changed.daily_returns[0])

    def test_walk_forward_selects_only_declared_opening_ranges(self) -> None:
        start = date(2024, 1, 1)
        bars = [
            bar
            for day_index in range(756)
            for bar in session(
                start + timedelta(days=day_index),
                final_close=108 + (day_index % 5),
            )
        ]

        result = walk_forward_opening_range_breakout(
            bars,
            candidate_opening_minutes=(5, 15, 30),
            costs=toss_krx_2026_costs(
                slippage_bps=5, project_outside_effective_period=True
            ),
        )

        self.assertGreaterEqual(result.windows, 3)
        self.assertTrue(
            set(result.selected_opening_range_minutes).issubset({5, 15, 30})
        )
        self.assertTrue(result.includes_fees_taxes_slippage)
        self.assertGreaterEqual(result.sample_years, 2)


if __name__ == "__main__":
    unittest.main()
