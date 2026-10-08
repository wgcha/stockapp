import tempfile
import unittest
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.intraday import IntradayBar, SQLiteIntradayBarStore
from stock_guide_agent.trading_calendar import KrxTradingCalendar


KST = timezone(timedelta(hours=9))
START = datetime(2026, 7, 17, 9, 0, tzinfo=KST)
FETCHED = datetime(2026, 7, 17, 7, 0, tzinfo=timezone.utc)


def bar(index: int) -> IntradayBar:
    price = 3000 + index * 0.1
    return IntradayBar(
        "KOSPI",
        START + timedelta(minutes=index),
        price,
        price + 1,
        price - 1,
        price + 0.5,
        1000 + index,
    )


class IntradayBarStoreTests(unittest.TestCase):
    def test_intraday_day_count_rejects_text_and_blob_financial_metrics(self) -> None:
        for column in ("daily_return_pct", "equity"):
            for corrupt_value in ("not-a-number", sqlite3.Binary(b"not-a-number")):
                with self.subTest(column=column, value_type=type(corrupt_value).__name__), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "intraday.sqlite3"
                    store = SQLiteIntradayBarStore(path)
                    day = date(2026, 7, 17)
                    store.record_paper_day(
                        strategy_id="orb",
                        version="1.0.0",
                        symbol="005930",
                        trading_date=day,
                        opening_range_minutes=5,
                        daily_return_pct=0.1,
                        initial_equity=10_000_000,
                        api_healthy=True,
                        costs_complete=True,
                    )
                    self.assertEqual(store.qualified_paper_days("orb", "1.0.0"), 1)
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute(
                            f"UPDATE intraday_paper_days SET {column} = ? WHERE strategy_id = ? AND version = ?",
                            (corrupt_value, "orb", "1.0.0"),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    self.assertEqual(store.qualified_paper_days("orb", "1.0.0"), 0)

    def test_invalid_financial_metric_on_any_symbol_invalidates_intraday_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intraday.sqlite3"
            store = SQLiteIntradayBarStore(path)
            for symbol in ("005930", "000660"):
                store.record_paper_day(
                    strategy_id="orb",
                    version="1.0.0",
                    symbol=symbol,
                    trading_date=date(2026, 7, 17),
                    opening_range_minutes=5,
                    daily_return_pct=0.1,
                    initial_equity=10_000_000,
                    api_healthy=True,
                    costs_complete=True,
                )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE intraday_paper_days SET equity = ? WHERE symbol = ?",
                    ("invalid", "000660"),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(store.qualified_paper_days("orb", "1.0.0"), 0)

    def test_qualified_day_requires_every_symbol_to_be_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            for symbol, healthy, complete, incidents in (
                ("005930", True, True, 0),
                ("000660", False, True, 0),
            ):
                store.record_paper_day(
                    strategy_id="orb",
                    version="1.0.0",
                    symbol=symbol,
                    trading_date=date(2026, 7, 17),
                    opening_range_minutes=5,
                    daily_return_pct=0.1,
                    initial_equity=10_000_000,
                    api_healthy=healthy,
                    costs_complete=complete,
                    critical_incidents=incidents,
                )
            self.assertEqual(store.qualified_paper_days("orb", "1.0.0"), 0)
            self.assertEqual(store.qualified_paper_days("orb", "2.0.0"), 0)

    def test_qualified_days_apply_cutoff_and_krx_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            for day in (
                date(2026, 1, 1),
                date(2026, 1, 2),
                date(2026, 1, 3),
                date(2026, 1, 5),
            ):
                store.record_paper_day(
                    strategy_id="orb",
                    version="1.0.0",
                    symbol="005930",
                    trading_date=day,
                    opening_range_minutes=5,
                    daily_return_pct=0.1,
                    initial_equity=10_000_000,
                    api_healthy=True,
                    costs_complete=True,
                )
            self.assertEqual(
                store.qualified_paper_days(
                    "orb",
                    "1.0.0",
                    as_of=date(2026, 1, 3),
                    trading_calendar=KrxTradingCalendar(),
                ),
                1,
            )

    def test_intraday_paper_values_and_bars_reject_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(bar(0), volume=float("inf"))
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            with self.assertRaisesRegex(ValueError, "finite"):
                store.record_paper_day(
                    strategy_id="orb",
                    version="1.0.0",
                    symbol="005930",
                    trading_date=date(2026, 7, 17),
                    opening_range_minutes=5,
                    daily_return_pct=100,
                    initial_equity=1e308,
                    api_healthy=True,
                    costs_complete=True,
                )
            self.assertIsNone(
                store.get_paper_day(
                    "orb", "1.0.0", "005930", date(2026, 7, 17)
                )
            )

    def test_intraday_paper_days_are_immutable_compounded_and_health_gated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            first = store.record_paper_day(
                strategy_id="orb",
                version="1.0.0",
                symbol="005930",
                trading_date=date(2026, 7, 16),
                opening_range_minutes=5,
                daily_return_pct=1,
                initial_equity=10_000_000,
                api_healthy=True,
                costs_complete=True,
            )
            duplicate = store.record_paper_day(
                strategy_id="orb",
                version="1.0.0",
                symbol="005930",
                trading_date=date(2026, 7, 16),
                opening_range_minutes=5,
                daily_return_pct=99,
                initial_equity=10_000_000,
                api_healthy=False,
                costs_complete=False,
            )
            second = store.record_paper_day(
                strategy_id="orb",
                version="1.0.0",
                symbol="005930",
                trading_date=date(2026, 7, 17),
                opening_range_minutes=5,
                daily_return_pct=-1,
                initial_equity=10_000_000,
                api_healthy=True,
                costs_complete=True,
                critical_incidents=1,
            )

            self.assertEqual(first, duplicate)
            self.assertEqual(first.equity, 10_100_000)
            self.assertEqual(second.equity, 9_999_000)
            self.assertEqual(store.qualified_paper_days("orb", "1.0.0"), 1)
            self.assertEqual(store.list_latest_paper_days("orb", "1.0.0"), (second,))

    def test_archive_is_idempotent_complete_and_chronological(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            bars = [bar(index) for index in range(300)]

            self.assertEqual(store.record_bars(bars, fetched_at=FETCHED), 300)
            self.assertEqual(store.record_bars(bars, fetched_at=FETCHED), 0)
            self.assertTrue(store.has_complete_day("KOSPI", START.date()))
            self.assertEqual(store.complete_day_count("KOSPI"), 1)
            restored = store.list_bars("KOSPI", START.date())
            self.assertEqual(restored[0], bars[0])
            self.assertEqual(restored[-1], bars[-1])

    def test_archive_fails_closed_on_conflict_or_invalid_ohlc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteIntradayBarStore(Path(directory) / "intraday.sqlite3")
            original = bar(0)
            store.record_bars([original], fetched_at=FETCHED)

            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.record_bars(
                    [replace(original, close_price=original.close_price + 0.25)],
                    fetched_at=FETCHED,
                )
            with self.assertRaisesRegex(ValueError, "OHLC"):
                IntradayBar("KOSPI", START, 100, 99, 98, 100, 1)


if __name__ == "__main__":
    unittest.main()
