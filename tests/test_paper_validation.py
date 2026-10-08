import math
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.backtest import DailyBar, toss_krx_2026_costs
from stock_guide_agent.intraday import IntradayBar, SQLiteIntradayBarStore
from stock_guide_agent.paper_trading import (
    PaperStrategyEvaluation,
    PaperPortfolioValuation,
    PaperPortfolioKey,
    PaperModelObservation,
    PaperTradeFill,
    PaperTradingDay,
    SQLitePaperTradingLedger,
    estimate_paper_transaction_cost,
)
from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG
from stock_guide_agent.strategy_store import SQLiteStrategyStore
from stock_guide_agent.validation_pipeline import StrategyValidationPipeline
from stock_guide_agent.trading_calendar import KrxTradingCalendar, TradingCalendarUnavailable


DEFINITION = PUBLIC_STRATEGY_CATALOG[0]


class TransactionCostEstimateTests(unittest.TestCase):
    def test_toss_buy_and_sell_include_commission_tax_and_slippage(self) -> None:
        buy = estimate_paper_transaction_cost(
            broker_provider="toss_invest", side="buy", notional=1_000_000,
            trading_date=date(2026, 7, 22),
        )
        sell = estimate_paper_transaction_cost(
            broker_provider="toss_invest", side="sell", notional=1_000_000,
            trading_date=date(2026, 7, 22),
        )
        self.assertAlmostEqual(buy or 0, 650)
        self.assertAlmostEqual(sell or 0, 2_650)

    def test_mirae_cost_is_unknown_without_contract_commission(self) -> None:
        self.assertIsNone(estimate_paper_transaction_cost(
            broker_provider="mirae_asset", side="buy", notional=1_000_000,
            trading_date=date(2026, 7, 22), mirae_commission_rate=None,
        ))


def evaluation(
    day: date,
    *,
    api_healthy: bool = True,
    critical_incidents: int = 0,
    hour_utc: int = 0,
) -> PaperStrategyEvaluation:
    return PaperStrategyEvaluation(
        strategy_id=DEFINITION.strategy_id,
        version=DEFINITION.version,
        user_id=30,
        stock_code="005930",
        action="hold",
        confidence=0.8,
        evaluated_at=datetime(
            day.year, day.month, day.day, hour_utc, tzinfo=timezone.utc
        ),
        api_healthy=api_healthy,
        critical_incidents=critical_incidents,
    )


def valuation(
    day: date,
    *,
    api_healthy: bool = True,
    costs_complete: bool = True,
    critical_incidents: int = 0,
) -> PaperPortfolioValuation:
    return PaperPortfolioValuation(
        strategy_id=DEFINITION.strategy_id,
        version=DEFINITION.version,
        user_id=30,
        trading_date=day,
        initial_cash=10_000_000,
        cash=10_000_000,
        market_value=0,
        equity=10_000_000,
        daily_pnl_pct=0,
        cumulative_return_pct=0,
        closing_prices={},
        api_healthy=api_healthy,
        costs_complete=costs_complete,
        critical_incidents=critical_incidents,
    )


def bars(count=2100):
    day = date(2017, 1, 2)
    price = 100.0
    result = []
    for index in range(count):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        price *= 1 + 0.0005 + 0.003 * math.sin(index / 19)
        result.append(DailyBar(day, price))
        day += timedelta(days=1)
    return result


class PaperValidationTests(unittest.TestCase):
    def test_completed_day_counts_reject_text_and_blob_financial_metrics(self):
        day = date(2026, 7, 17)
        columns = (
            ("paper_strategy_evaluations", "confidence"),
            ("paper_portfolio_valuations", "initial_cash"),
            ("paper_portfolio_valuations", "cash"),
            ("paper_portfolio_valuations", "market_value"),
            ("paper_portfolio_valuations", "equity"),
            ("paper_portfolio_valuations", "daily_pnl_pct"),
            ("paper_portfolio_valuations", "cumulative_return_pct"),
            ("paper_model_days", "close_price"),
            ("paper_model_days", "target_exposure"),
            ("paper_model_days", "applied_exposure"),
            ("paper_model_days", "daily_return_pct"),
            ("paper_model_days", "equity"),
            ("paper_model_days", "cumulative_return_pct"),
            ("paper_model_days", "transaction_cost_rate"),
        )
        for table, column in columns:
            for corrupt_value in ("not-a-number", sqlite3.Binary(b"not-a-number")):
                with self.subTest(table=table, column=column, value_type=type(corrupt_value).__name__), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "paper.sqlite3"
                    ledger = SQLitePaperTradingLedger(path)
                    if table == "paper_strategy_evaluations":
                        ledger.record_evaluation(evaluation(day))
                        ledger.record_valuation(valuation(day))
                    elif table == "paper_portfolio_valuations":
                        ledger.record_evaluation(evaluation(day))
                        ledger.record_valuation(valuation(day))
                    else:
                        ledger.record_model_observation(
                            PaperModelObservation(
                                DEFINITION.strategy_id,
                                DEFINITION.version,
                                "KOSPI",
                                day,
                                3000,
                                0.4,
                                True,
                            ),
                            initial_equity=10_000_000,
                            buy_cost_rate=0.00015,
                            sell_cost_rate=0.00215,
                        )
                    self.assertEqual(
                        ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version),
                        1,
                    )
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute(
                            f"UPDATE {table} SET {column} = ? WHERE strategy_id = ? AND version = ?",
                            (corrupt_value, DEFINITION.strategy_id, DEFINITION.version),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    self.assertEqual(
                        ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version),
                        0,
                    )

    def test_corrupt_user_metric_does_not_hide_independent_healthy_model_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            ledger = SQLitePaperTradingLedger(path)
            day = date(2026, 7, 17)
            ledger.record_evaluation(evaluation(day))
            ledger.record_valuation(valuation(day))
            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    day,
                    3000,
                    0.4,
                    True,
                ),
                initial_equity=10_000_000,
                buy_cost_rate=0.00015,
                sell_cost_rate=0.00215,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE paper_strategy_evaluations SET confidence = ? WHERE strategy_id = ? AND version = ?",
                    (
                        sqlite3.Binary(b"bad"),
                        DEFINITION.strategy_id,
                        DEFINITION.version,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )

    def test_user_evidence_requires_a_matching_user_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            ledger.record_evaluation(evaluation(day))
            ledger.record_valuation(replace(valuation(day), user_id=40))
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )

            ledger.record_valuation(
                replace(valuation(day), version="2.0.0")
            )
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )
            ledger.record_valuation(valuation(day))
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )

    def test_unhealthy_user_date_does_not_block_an_independent_healthy_model_day(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            ledger.record_evaluation(evaluation(day))
            ledger.record_valuation(valuation(day))
            ledger.record_evaluation(
                replace(evaluation(day), user_id=40, api_healthy=False)
            )
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )

            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    day,
                    100,
                    0.5,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )

    def test_user_and_model_evidence_union_deduplicates_same_date(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            ledger.record_evaluation(evaluation(day))
            ledger.record_valuation(valuation(day))
            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    day,
                    100,
                    0.5,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )

    def test_as_of_and_krx_calendar_exclude_future_weekend_and_holiday_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            for day in (
                date(2026, 1, 1),  # New Year's Day
                date(2026, 1, 2),
                date(2026, 1, 3),  # Saturday
                date(2026, 1, 5),  # After the cutoff
            ):
                ledger.record_evaluation(evaluation(day))
                ledger.record_valuation(valuation(day))

            calendar = KrxTradingCalendar()
            self.assertIsNone(calendar.session_for(date(2026, 1, 1)))
            self.assertIsNone(calendar.session_for(date(2026, 1, 3)))
            self.assertEqual(
                ledger.completed_days(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    as_of=date(2026, 1, 3),
                    trading_calendar=calendar,
                ),
                1,
            )

    def test_unavailable_calendar_fails_before_validation_report_is_written(self):
        class UnavailableCalendar:
            def session_for(self, value):
                raise TradingCalendarUnavailable("calendar unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            ledger.record_evaluation(evaluation(date(2026, 7, 17)))
            ledger.record_valuation(valuation(date(2026, 7, 17)))
            store = SQLiteStrategyStore(root / "strategy.sqlite3")
            pipeline = StrategyValidationPipeline(
                store, ledger, trading_calendar=UnavailableCalendar()
            )
            with self.assertRaises(TradingCalendarUnavailable):
                pipeline.validate_momentum(
                    strategy_id=DEFINITION.strategy_id,
                    version=DEFINITION.version,
                    market="korean_equities",
                    bars=bars(),
                    costs=toss_krx_2026_costs(
                        project_outside_effective_period=True
                    ),
                    checked_on=date(2026, 7, 17),
                    candidate_lookbacks=(20, 60),
                )
            self.assertEqual(
                store.get_record(DEFINITION.strategy_id, DEFINITION.version).reports, []
            )

    def test_pipeline_day_counts_stop_at_checked_on(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            for day in (date(2026, 7, 17), date(2026, 7, 20)):
                ledger.record_evaluation(evaluation(day))
                ledger.record_valuation(valuation(day))
            pipeline = StrategyValidationPipeline(
                SQLiteStrategyStore(root / "strategy.sqlite3"), ledger
            )
            outcome = pipeline.validate_momentum(
                strategy_id=DEFINITION.strategy_id,
                version=DEFINITION.version,
                market="korean_equities",
                bars=bars(),
                costs=toss_krx_2026_costs(
                    project_outside_effective_period=True
                ),
                checked_on=date(2026, 7, 17),
                candidate_lookbacks=(20, 60),
            )
            self.assertEqual(outcome.report.paper_trading_days, 1)

    def test_paper_financial_values_must_be_finite(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            estimate_paper_transaction_cost(
                broker_provider="toss_invest",
                side="buy",
                notional=float("inf"),
                trading_date=date(2026, 7, 17),
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(valuation(date(2026, 7, 17)), equity=float("nan"))
        with self.assertRaisesRegex(ValueError, "invalid"):
            PaperModelObservation(
                DEFINITION.strategy_id,
                DEFINITION.version,
                "KOSPI",
                date(2026, 7, 17),
                float("inf"),
                0.5,
                True,
            )
        with self.assertRaisesRegex(ValueError, "positive"):
            PaperTradeFill(
                "proposal", DEFINITION.strategy_id, DEFINITION.version, 30,
                "toss_invest", "005930", "buy", 1, float("inf"),
                datetime(2026, 7, 17, tzinfo=timezone.utc),
            )

        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            ledger.record_evaluation(evaluation(day))
            ledger.record_valuation(valuation(day))
            with ledger._connect() as connection:
                connection.execute(
                    "UPDATE paper_portfolio_valuations SET equity = ?",
                    (float("inf"),),
                )
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )

    def test_model_overflow_is_rejected_before_persisting_the_day(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    date(2026, 7, 16),
                    100,
                    1.0,
                    True,
                ),
                initial_equity=1e308,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )
            with self.assertRaisesRegex(ValueError, "finite"):
                ledger.record_model_observation(
                    PaperModelObservation(
                        DEFINITION.strategy_id,
                        DEFINITION.version,
                        "KOSPI",
                        date(2026, 7, 17),
                        200,
                        1.0,
                        True,
                    ),
                    initial_equity=1e308,
                    buy_cost_rate=0.001,
                    sell_cost_rate=0.003,
                )
            self.assertIsNone(
                ledger.get_model_day(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    date(2026, 7, 17),
                )
            )

    def test_independent_model_paper_returns_use_prior_exposure_and_costs(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            first = ledger.record_model_observation(
                PaperModelObservation(
                    "volatility_managed_exposure",
                    "1.0.0",
                    "KOSPI",
                    date(2026, 7, 16),
                    100,
                    0.5,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )
            second = ledger.record_model_observation(
                PaperModelObservation(
                    "volatility_managed_exposure",
                    "1.0.0",
                    "KOSPI",
                    date(2026, 7, 17),
                    110,
                    0.25,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )
            repeated = ledger.record_model_observation(
                PaperModelObservation(
                    "volatility_managed_exposure",
                    "1.0.0",
                    "KOSPI",
                    date(2026, 7, 17),
                    999,
                    1.0,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=0.001,
                sell_cost_rate=0.003,
            )

            self.assertAlmostEqual(first.equity, 999_500)
            self.assertEqual(second.applied_exposure, 0.5)
            self.assertAlmostEqual(second.daily_return_pct, 4.925)
            self.assertEqual(repeated, second)
            self.assertEqual(
                ledger.completed_days("volatility_managed_exposure", "1.0.0"),
                2,
            )

    def test_model_day_with_unknown_costs_does_not_qualify(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = ledger.record_model_observation(
                PaperModelObservation(
                    "volatility_managed_exposure",
                    "1.0.0",
                    "KOSPI",
                    date(2028, 1, 3),
                    100,
                    0.5,
                    True,
                ),
                initial_equity=1_000_000,
                buy_cost_rate=None,
                sell_cost_rate=None,
            )

            self.assertFalse(day.costs_complete)
            self.assertEqual(
                ledger.completed_days("volatility_managed_exposure", "1.0.0"),
                0,
            )

    def test_momentum_pipeline_rejects_a_different_strategy_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStrategyStore(root / "strategy.sqlite3")
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            pipeline = StrategyValidationPipeline(store, ledger)
            factor = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "factor_momentum"
            )

            with self.assertRaisesRegex(ValueError, "time_series_momentum"):
                pipeline.validate_momentum(
                    strategy_id=factor.strategy_id,
                    version=factor.version,
                    market="korean_equities",
                    bars=[],
                    costs=toss_krx_2026_costs(),
                    checked_on=date(2026, 7, 17),
                )

    def test_volatility_pipeline_requires_matching_engine_and_real_paper_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStrategyStore(root / "strategy.sqlite3")
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            pipeline = StrategyValidationPipeline(store, ledger)
            volatility = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "volatility_managed_exposure"
            )

            outcome = pipeline.validate_volatility_managed(
                strategy_id=volatility.strategy_id,
                version=volatility.version,
                market="korean_equity_index",
                bars=bars(),
                costs=toss_krx_2026_costs(
                    project_outside_effective_period=True
                ),
                checked_on=date(2026, 7, 17),
                candidate_lookbacks=(20, 40),
                candidate_target_volatilities=(0.10, 0.15),
            )

            self.assertEqual(outcome.report.paper_trading_days, 0)
            self.assertFalse(outcome.decision.eligible)
            self.assertIn(
                "insufficient_paper_trading", outcome.decision.failures
            )
            with self.assertRaisesRegex(ValueError, "volatility_managed_exposure"):
                pipeline.validate_volatility_managed(
                    strategy_id=DEFINITION.strategy_id,
                    version=DEFINITION.version,
                    market="korean_equity_index",
                    bars=[],
                    costs=toss_krx_2026_costs(),
                    checked_on=date(2026, 7, 17),
                )

    def test_intraday_pipeline_uses_only_its_health_gated_paper_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStrategyStore(root / "strategy.sqlite3")
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            intraday = SQLiteIntradayBarStore(root / "intraday.sqlite3")
            strategy = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "intraday_opening_range_breakout"
            )
            intraday.record_paper_day(
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                symbol="005930",
                trading_date=date(2026, 7, 17),
                opening_range_minutes=5,
                daily_return_pct=0.1,
                initial_equity=10_000_000,
                api_healthy=True,
                costs_complete=True,
            )
            intraday.record_paper_day(
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                symbol="000660",
                trading_date=date(2026, 7, 20),
                opening_range_minutes=5,
                daily_return_pct=0.1,
                initial_equity=10_000_000,
                api_healthy=True,
                costs_complete=True,
            )
            sessions = []
            day = date(2026, 1, 2)
            kst = timezone(timedelta(hours=9))
            for session_index in range(80):
                while day.weekday() >= 5:
                    day += timedelta(days=1)
                base = 100 + session_index * 0.1
                for minute in range(7):
                    price = base if minute < 5 else base + 2 + minute * 0.1
                    sessions.append(
                        IntradayBar(
                            "005930",
                            datetime.combine(day, datetime.min.time(), kst)
                            + timedelta(hours=9, minutes=minute),
                            price,
                            price + 0.2,
                            price - 0.2,
                            price + 0.1,
                            1000,
                        )
                    )
                day += timedelta(days=1)
            pipeline = StrategyValidationPipeline(store, ledger, intraday)

            outcome = pipeline.validate_opening_range_breakout(
                strategy_id=strategy.strategy_id,
                version=strategy.version,
                market="korean_equities",
                bars=sessions,
                costs=toss_krx_2026_costs(),
                checked_on=date(2026, 7, 17),
                train_days=60,
                test_days=20,
            )

            self.assertEqual(outcome.report.paper_trading_days, 1)
            self.assertEqual(outcome.report.signal_engine, "opening_range_breakout")
            self.assertFalse(outcome.decision.eligible)

    def test_days_are_immutable_and_incident_days_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            good = PaperTradingDay(
                DEFINITION.strategy_id, DEFINITION.version, date(2026, 7, 17), 1, 0.2
            )
            bad = PaperTradingDay(
                DEFINITION.strategy_id,
                DEFINITION.version,
                date(2026, 7, 18),
                0,
                -0.1,
                critical_incidents=1,
            )
            ledger.record_day(good)
            ledger.record_day(bad)

            # Manually supplied aggregates remain an audit trail, but cannot
            # satisfy the evidence-backed paper-validation duration by themselves.
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )

            first = evaluation(date(2026, 7, 17))
            incident = evaluation(
                date(2026, 7, 18), critical_incidents=1
            )
            self.assertTrue(ledger.record_evaluation(first))
            self.assertFalse(ledger.record_evaluation(first))
            ledger.record_evaluation(incident)
            ledger.record_valuation(valuation(date(2026, 7, 17)))
            ledger.record_valuation(
                valuation(date(2026, 7, 18), critical_incidents=1)
            )

            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )
            self.assertEqual(
                ledger.total_incidents(DEFINITION.strategy_id, DEFINITION.version), 2
            )
            self.assertEqual(
                ledger.evaluation_count(DEFINITION.strategy_id, DEFINITION.version), 2
            )
            with self.assertRaisesRegex(ValueError, "immutable"):
                ledger.record_day(good)

    def test_evaluation_uses_korean_trading_date_and_health_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            # 16:00 UTC is already the following calendar day in Korea.
            ledger.record_evaluation(
                evaluation(date(2026, 7, 17), hour_utc=16)
            )
            ledger.record_evaluation(
                evaluation(date(2026, 7, 18), api_healthy=False, hour_utc=16)
            )
            ledger.record_valuation(valuation(date(2026, 7, 18)))

            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 1
            )

    def test_owner_approved_simulated_fill_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            fill = PaperTradeFill(
                proposal_id="proposal-1",
                strategy_id=DEFINITION.strategy_id,
                version=DEFINITION.version,
                user_id=30,
                broker_provider="toss_invest",
                stock_code="005930",
                side="buy",
                quantity=2,
                fill_price=70_000,
                filled_at=datetime(2026, 7, 17, 16, tzinfo=timezone.utc),
            )

            self.assertTrue(ledger.record_fill(fill))
            self.assertFalse(ledger.record_fill(fill))
            self.assertEqual(ledger.fill_count(user_id=30), 1)
            self.assertEqual(
                ledger.fill_count(
                    strategy_id=DEFINITION.strategy_id,
                    version=DEFINITION.version,
                ),
                1,
            )

    def test_portfolio_replay_includes_costs_and_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            cost = estimate_paper_transaction_cost(
                broker_provider="toss_invest",
                side="buy",
                notional=70_000,
                trading_date=day,
            )
            self.assertAlmostEqual(cost, 45.5)
            ledger.record_fill(
                PaperTradeFill(
                    "proposal-1",
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    30,
                    "toss_invest",
                    "005930",
                    "buy",
                    1,
                    70_000,
                    datetime(2026, 7, 17, 6, tzinfo=timezone.utc),
                    cost,
                )
            )
            key = PaperPortfolioKey(DEFINITION.strategy_id, DEFINITION.version, 30)

            first = ledger.value_portfolio(
                key,
                trading_date=day,
                initial_cash=1_000_000,
                closing_prices={"005930": 71_000},
            )
            repeated = ledger.value_portfolio(
                key,
                trading_date=day,
                initial_cash=1_000_000,
                closing_prices={"005930": 90_000},
            )

            self.assertAlmostEqual(first.cash, 929_954.5)
            self.assertAlmostEqual(first.equity, 1_000_954.5)
            self.assertAlmostEqual(first.daily_pnl_pct, 0.09545)
            self.assertEqual(repeated, first)

    def test_unknown_commission_or_missing_price_blocks_paper_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = SQLitePaperTradingLedger(Path(directory) / "paper.sqlite3")
            day = date(2026, 7, 17)
            ledger.record_fill(
                PaperTradeFill(
                    "proposal-1",
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    30,
                    "mirae_asset",
                    "005930",
                    "buy",
                    1,
                    70_000,
                    datetime(2026, 7, 17, 6, tzinfo=timezone.utc),
                    None,
                )
            )
            ledger.record_evaluation(evaluation(day))
            result = ledger.value_portfolio(
                PaperPortfolioKey(DEFINITION.strategy_id, DEFINITION.version, 30),
                trading_date=day,
                initial_cash=1_000_000,
                closing_prices={},
            )

            self.assertFalse(result.costs_complete)
            self.assertEqual(result.critical_incidents, 1)
            self.assertEqual(
                ledger.completed_days(DEFINITION.strategy_id, DEFINITION.version), 0
            )

    def test_pipeline_reads_paper_days_from_ledger_not_caller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            for index in range(12):
                day = date(2026, 6, 1) + timedelta(days=index)
                ledger.record_evaluation(evaluation(day))
                ledger.record_valuation(valuation(day))
            pipeline = StrategyValidationPipeline(
                SQLiteStrategyStore(root / "strategy.sqlite3"), ledger
            )

            outcome = pipeline.validate_momentum(
                strategy_id=DEFINITION.strategy_id,
                version=DEFINITION.version,
                market="korean_equities",
                bars=bars(),
                costs=toss_krx_2026_costs(
                    project_outside_effective_period=True
                ),
                checked_on=date(2026, 7, 17),
                candidate_lookbacks=(20, 60, 120),
            )

            self.assertEqual(outcome.report.paper_trading_days, 10)
            self.assertFalse(outcome.decision.eligible)
            self.assertIn("insufficient_paper_trading", outcome.decision.failures)


if __name__ == "__main__":
    unittest.main()
