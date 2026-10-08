import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.cache import SQLiteMarketCache
from stock_guide_agent.academic_discovery import AcademicStrategyCandidate
from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.intent import parse_investment_intent
from stock_guide_agent.intraday import SQLiteIntradayBarStore
from stock_guide_agent.execution import ExecutionState
from stock_guide_agent.guidance import GuideInput
from stock_guide_agent.market import MarketSnapshot
from stock_guide_agent.news import NewsArticle
from stock_guide_agent.paper_trading import (
    PaperStrategyEvaluation,
    PaperTradeFill,
    SQLitePaperTradingLedger,
)
from stock_guide_agent.research_queue import SQLiteResearchQueue
from stock_guide_agent.scheduled_handlers import ScheduledDataHandlers
from stock_guide_agent.source_monitor import (
    SQLiteSourceFingerprintStore,
    StrategySourceMonitor,
)
from stock_guide_agent.state import Holding, SQLiteUserStateStore
from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, ValidationReport
from stock_guide_agent.strategy_store import SQLiteStrategyStore
from stock_guide_agent.toss import TossInvestClient, TossInvestToken
from stock_guide_agent.trading_calendar import TradingSession


NOW = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)


class FixedTradingCalendar:
    """A deterministic XKRX-like calendar for unrelated handler unit tests."""

    def session_for(self, value):
        local_day = (
            value.astimezone(timezone(timedelta(hours=9))).date()
            if isinstance(value, datetime)
            else value
        )
        opens_at = datetime.combine(
            local_day, datetime.min.time(), timezone(timedelta(hours=9))
        ).replace(hour=9)
        closes_at = opens_at.replace(hour=15, minute=30)
        return TradingSession(local_day, opens_at, closes_at)

    def is_after_close_evaluation(self, now):
        return now.astimezone(timezone(timedelta(hours=9))) >= self.session_for(now).evaluation_available_at

    def daily_guide_start(self, now, *, configured_hour, configured_minute):
        session = self.session_for(now)
        configured = session.opens_at.replace(
            hour=configured_hour, minute=configured_minute
        )
        return max(configured, session.opens_at + timedelta(minutes=10))


class FakeTransport:
    def __init__(self):
        self.requests: list[HttpRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.endswith("/prices"):
            symbols = request.query["symbols"].split(",")
            return HttpResponse(
                200,
                {},
                {
                    "result": [
                        {
                            "symbol": symbol,
                            "timestamp": NOW.isoformat(),
                            "lastPrice": "70000",
                            "currency": "KRW",
                        }
                        for symbol in symbols
                    ]
                },
            )
        if request.url.endswith("/stocks"):
            symbols = request.query["symbols"].split(",")
            return HttpResponse(
                200,
                {},
                {
                    "result": [
                        {"symbol": symbol, "name": "테스트기업"}
                        for symbol in symbols
                    ]
                },
            )
        if request.url.endswith("/holdings"):
            return HttpResponse(
                200,
                {},
                {"result": {"dailyProfitLoss": {"rate": "-0.016"}}},
            )
        if request.url.endswith("/candles"):
            return HttpResponse(
                200,
                {},
                {
                    "result": {
                        "candles": [
                            {
                                "timestamp": NOW.isoformat(),
                                "closePrice": str(100 - index),
                                "lowPrice": str(99 - index),
                            }
                            for index in range(60)
                        ]
                    }
                },
            )
        return HttpResponse(
            200,
            {},
            {
                "result": {
                    "records": [
                        {
                            "updatedAt": NOW.isoformat(),
                            "individual": {"buyAmount": "1", "sellAmount": "1"},
                        }
                    ]
                }
            },
        )


class ScheduledHandlerTests(unittest.TestCase):
    def test_after_close_intraday_archive_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, users, _, _, _, transport = self.make_handlers(root)
            archive = SQLiteIntradayBarStore(root / "intraday.sqlite3")
            handlers.intraday_store = archive
            users.upsert_holding(Holding(30, "005930", 10, 70000, NOW))
            local_start = NOW.astimezone(timezone(timedelta(hours=9))).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            candles = tuple(
                {
                    "timestamp": (local_start + timedelta(minutes=index)).isoformat(),
                    "openPrice": str(3000 + index * 0.1),
                    "highPrice": str(3001 + index * 0.1),
                    "lowPrice": str(2999 + index * 0.1),
                    "closePrice": str(3000.5 + index * 0.1),
                    "volume": str(1000 + index),
                }
                for index in range(390)
            )
            calls = 0

            def history(*args, **kwargs):
                nonlocal calls
                calls += 1
                return candles

            handlers.toss_client.get_candle_history = history  # type: ignore[method-assign]

            first = handlers.archive_intraday_bars(NOW)
            second = handlers.archive_intraday_bars(NOW)

            self.assertIn("archived=2,skipped=0,inserted=780", first)
            self.assertIn("archived=0,skipped=2,inserted=0", second)
            self.assertEqual(calls, 2)
            self.assertTrue(archive.has_complete_day("KOSPI", NOW.date()))
            self.assertTrue(archive.has_complete_day("005930", NOW.date()))
            self.assertEqual(
                archive.qualified_paper_days(
                    "intraday_opening_range_breakout", "1.0.0"
                ),
                1,
            )
            self.assertEqual(transport.requests, [])

    def test_monthly_long_history_validation_never_auto_activates_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, _, _, strategies, _, transport = self.make_handlers(root)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            count_arguments = []
            ledger.completed_days = (  # type: ignore[method-assign]
                lambda strategy_id, version, **kwargs: (
                    count_arguments.append(kwargs) or 30
                )
            )
            handlers.paper_ledger = ledger
            candles = tuple(
                {
                    "timestamp": (NOW - timedelta(days=1999 - index)).isoformat(),
                    "closePrice": str(100 + (index % 20) * 2 + index * 0.02),
                }
                for index in range(2000)
            )
            handlers.toss_client.get_candle_history = (  # type: ignore[method-assign]
                lambda *args, **kwargs: candles
            )

            detail = handlers.validate_ready_strategies(NOW)

            self.assertTrue(detail.startswith("evaluated=3,awaiting_owner="))
            executable = [
                record
                for record in strategies.list_records()
                if record.definition.signal_engine
                in {"time_series_momentum", "volatility_managed_exposure"}
            ]
            self.assertTrue(all(len(record.reports) == 1 for record in executable))
            self.assertTrue(all(record.status != "approved" for record in executable))
            self.assertTrue(
                all(
                    "apply_2026_costs_outside_disclosed_effective_period"
                    in record.reports[-1].cost_assumptions
                    for record in executable
                )
            )
            self.assertEqual(transport.requests, [])
            self.assertTrue(count_arguments)
            self.assertTrue(
                all(arguments["as_of"] == NOW.date() for arguments in count_arguments)
            )
            self.assertTrue(
                all(
                    arguments["trading_calendar"] is handlers.trading_calendar
                    for arguments in count_arguments
                )
            )

    def make_handlers(self, root):
        transport = FakeTransport()
        users = SQLiteUserStateStore(root / "users.sqlite3")
        cache = SQLiteMarketCache(root / "cache.sqlite3")
        strategies = SQLiteStrategyStore(root / "strategies.sqlite3")
        queue = SQLiteResearchQueue(root / "research.sqlite3")
        handlers = ScheduledDataHandlers(
            toss_client=TossInvestClient("CLIENT", "SECRET", transport=transport),
            token_source=lambda: TossInvestToken("TOKEN", "Bearer", 3600),
            user_store=users,
            market_cache=cache,
            strategy_store=strategies,
            research_queue=queue,
            trading_calendar=FixedTradingCalendar(),
        )
        return handlers, users, cache, strategies, queue, transport

    def test_watchlist_prices_and_market_flow_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, users, cache, _, _, transport = self.make_handlers(root)
            users.upsert_holding(Holding(30, "005930", 10, 70000, NOW))
            users.upsert_holding(Holding(40, "005930", 1, 80000, NOW))
            users.upsert_holding(Holding(40, "000660", 2, 180000, NOW))

            self.assertEqual(handlers.refresh_prices(NOW), "updated=2")
            self.assertEqual(handlers.refresh_investor_flow(NOW), "updated=2")

            self.assertIsNotNone(cache.get("toss_invest", "price", "005930"))
            self.assertIsNotNone(cache.get("toss_invest", "price", "000660"))
            self.assertIsNotNone(cache.get("toss_invest", "investor_flow", "KOSPI"))
            price_requests = [r for r in transport.requests if r.url.endswith("/prices")]
            self.assertEqual(len(price_requests), 1)

    def test_account_daily_loss_updates_state_and_activates_kill_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            handlers, _, _, _, _, _ = self.make_handlers(Path(directory))
            handlers.toss_client.account_sequence = "1"
            state = ExecutionState(max_daily_loss_pct=1.5)
            handlers.execution_state = state

            detail = handlers.refresh_account_risk(NOW)

            self.assertEqual(state.daily_pnl_pct, -1.6)
            self.assertTrue(state.kill_switch_active)
            self.assertIn("kill_switch=true", detail)

    def test_after_close_paper_portfolio_is_valued_with_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, _, _, _, _, _ = self.make_handlers(root)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            handlers.paper_ledger = ledger
            ledger.record_fill(
                PaperTradeFill(
                    "proposal-1",
                    "time_series_momentum",
                    "1.0.0",
                    30,
                    "toss_invest",
                    "005930",
                    "buy",
                    1,
                    70_000,
                    NOW,
                    50,
                )
            )
            ledger.record_evaluation(
                PaperStrategyEvaluation(
                    "time_series_momentum",
                    "1.0.0",
                    30,
                    "005930",
                    "buy",
                    0.8,
                    NOW,
                    True,
                )
            )

            detail = handlers.value_paper_portfolios(NOW)

            self.assertEqual(detail, "valued=1, clean=1, incidents=0")
            self.assertEqual(
                ledger.valuation_count("time_series_momentum", "1.0.0"), 1
            )
            self.assertEqual(
                ledger.completed_days("time_series_momentum", "1.0.0"), 1
            )

    def test_after_close_strategy_targets_are_evaluated_once_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, users, _, _, _, _ = self.make_handlers(root)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            handlers.paper_ledger = ledger
            users.save_intent(
                30,
                parse_investment_intent("장기 100%, 연간 8% 목표"),
                updated_at=NOW,
            )
            users.upsert_holding(Holding(30, "005930", 1, 70_000, NOW))

            def factory(user_id, stock_code, intent, holding, now):
                return GuideInput(
                    stock_code=stock_code,
                    market=MarketSnapshot(
                        as_of=now,
                        regime="risk_on",
                        score=0.5,
                        confidence=0.8,
                        category_scores={"price": 0.5},
                        evidence=[],
                    ),
                    strategy_id="time_series_momentum",
                    strategy_approved=False,
                    signal_score=0.6,
                    signal_confidence=0.8,
                    max_trade_loss_pct=1,
                    max_daily_loss_pct=1.5,
                    daily_pnl_pct=0,
                )

            handlers.guide_input_factory = factory

            first = handlers.evaluate_paper_strategies(NOW)
            second = handlers.evaluate_paper_strategies(NOW)
            valuation = handlers.value_paper_portfolios(NOW)

            self.assertEqual(first, "evaluated=1, skipped=0, failures=0")
            self.assertEqual(second, "evaluated=0, skipped=1, failures=0")
            self.assertEqual(valuation, "valued=1, clean=1, incidents=0")
            self.assertEqual(
                ledger.evaluation_count("time_series_momentum", "1.0.0"), 1
            )
            self.assertEqual(
                ledger.completed_days("time_series_momentum", "1.0.0"), 1
            )

    def test_after_close_volatility_model_records_independent_paper_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, _, _, _, _, _ = self.make_handlers(root)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            handlers.paper_ledger = ledger

            detail = handlers.evaluate_volatility_model(NOW)
            day = ledger.get_model_day(
                "volatility_managed_exposure",
                "1.0.0",
                "KOSPI",
                NOW.date(),
            )

            self.assertIsNotNone(day)
            self.assertIn("qualified=true", detail)
            self.assertGreaterEqual(day.target_exposure, 0)
            self.assertLessEqual(day.target_exposure, 1)
            self.assertEqual(
                ledger.completed_days("volatility_managed_exposure", "1.0.0"),
                1,
            )

    def test_after_close_long_term_model_records_independent_paper_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, _, _, _, _, _ = self.make_handlers(root)
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            handlers.paper_ledger = ledger

            detail = handlers.evaluate_long_term_model(NOW)
            day = ledger.get_model_day(
                "long_term_absolute_momentum",
                "1.0.0",
                "KOSPI",
                NOW.date(),
            )

            self.assertIsNotNone(day)
            self.assertIn("qualified=true", detail)
            self.assertIn(day.target_exposure, {0.0, 1.0})
            self.assertEqual(
                ledger.completed_days("long_term_absolute_momentum", "1.0.0"),
                1,
            )

    def test_research_job_enqueues_due_sources_without_approving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handlers, _, _, strategies, queue, _ = self.make_handlers(Path(directory))

            detail = handlers.enqueue_strategy_research(NOW)

            self.assertEqual(detail, f"queued={len(PUBLIC_STRATEGY_CATALOG)}")
            self.assertEqual(len(queue.list_pending()), len(PUBLIC_STRATEGY_CATALOG))
            self.assertFalse(
                strategies.is_approved(
                    PUBLIC_STRATEGY_CATALOG[0].strategy_id,
                    PUBLIC_STRATEGY_CATALOG[0].version,
                )
            )

            claimed = queue.claim_next(now=NOW)
            self.assertIsNotNone(claimed)
            queue.complete(
                claimed.task_id,
                succeeded=True,
                notes="Primary source checked; validation still required.",
                now=NOW,
            )
            self.assertEqual(queue.history_count(), 1)

    def test_changed_source_suspends_previously_approved_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handlers, _, _, strategies, _, _ = self.make_handlers(root)
            definition = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "time_series_momentum"
            )
            strategies.apply_validation(
                ValidationReport(
                    definition.strategy_id,
                    definition.version,
                    "korean_equities",
                    8,
                    True,
                    6,
                    300,
                    0.8,
                    0.18,
                    0.75,
                    60,
                    True,
                    True,
                    "time_series_momentum",
                ),
                checked_on=NOW.date(),
            )
            strategies.approve(
                definition.strategy_id,
                definition.version,
                approved_by=900,
                approved_at=NOW,
            )
            content = {"value": b"baseline"}
            handlers.source_monitor = StrategySourceMonitor(
                SQLiteSourceFingerprintStore(root / "fingerprints.sqlite3"),
                fetcher=lambda url: content["value"],
            )

            first = handlers.enqueue_strategy_research(NOW)
            content["value"] = b"revised-source"
            second = handlers.enqueue_strategy_research(NOW + timedelta(days=91))

            self.assertIn("suspended=0", first)
            self.assertIn("suspended=1", second)
            self.assertEqual(
                strategies.get_record(
                    definition.strategy_id, definition.version
                ).status,
                "suspended",
            )
            self.assertEqual(
                strategies.list_activation_events(
                    definition.strategy_id, definition.version
                )[-1].action,
                "suspended",
            )

    def test_academic_discovery_only_enqueues_new_candidates(self) -> None:
        class FakeAcademicDiscovery:
            def discover(self, *, since, rows=20):
                return (
                    AcademicStrategyCandidate(
                        "10.1000/new.1",
                        "Momentum Strategy for Stock Portfolio Trading",
                        date(2026, 6, 1),
                        "https://doi.org/10.1000/new.1",
                        ("Ada Kim",),
                        ("stock", "portfolio", "momentum", "strategy"),
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            handlers, _, _, _, queue, _ = self.make_handlers(Path(directory))
            handlers.academic_discovery = FakeAcademicDiscovery()

            first = handlers.discover_academic_strategies(NOW)
            second = handlers.discover_academic_strategies(NOW)

            self.assertEqual(first, "discovered=1, queued=1")
            self.assertEqual(second, "discovered=1, queued=0")
            self.assertEqual(len(queue.list_pending_academic_candidates()), 1)

    def test_overseas_benchmarks_are_normalized_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handlers, _, cache, _, _, transport = self.make_handlers(
                Path(directory)
            )

            detail = handlers.refresh_overseas(NOW)

            self.assertEqual(detail, "updated=2")
            spy = cache.get("toss_invest", "overseas", "SPY")
            self.assertIsNotNone(spy)
            self.assertGreater(spy.payload["normalized_score"], 0)
            self.assertEqual(spy.payload["source_tier"], 1)
            candle_requests = [
                r for r in transport.requests if r.url.endswith("/candles")
            ]
            self.assertEqual(len(candle_requests), 2)

    def test_only_corroborated_material_news_is_cached(self) -> None:
        class FakeNews:
            def search(self, query, *, display=10):
                return [
                    NewsArticle(
                        "테스트기업 거래정지",
                        "https://publisher-one.example/a",
                        NOW,
                    ),
                    NewsArticle(
                        "테스트기업 거래정지 보도",
                        "https://publisher-two.example/b",
                        NOW,
                    ),
                ]

        with tempfile.TemporaryDirectory() as directory:
            handlers, users, cache, _, _, _ = self.make_handlers(Path(directory))
            handlers.news_client = FakeNews()
            users.upsert_holding(Holding(30, "005930", 10, 70000, NOW))

            detail = handlers.refresh_news(NOW)

            self.assertEqual(detail, "updated=1")
            news = cache.get("naver_api_hub_news", "news", "005930")
            self.assertIsNotNone(news)
            self.assertEqual(news.payload["normalized_score"], -0.35)
            self.assertEqual(len(news.payload["reference_urls"]), 2)

    def test_health_check_suspends_stale_approved_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handlers, _, _, strategies, _, _ = self.make_handlers(Path(directory))
            definition = PUBLIC_STRATEGY_CATALOG[0]
            strategies.apply_validation(
                ValidationReport(
                    definition.strategy_id,
                    definition.version,
                    "korean_equities",
                    8,
                    True,
                    6,
                    300,
                    0.8,
                    0.18,
                    0.75,
                    60,
                    True,
                    True,
                    "time_series_momentum",
                ),
                checked_on=date(2026, 1, 1),
            )
            strategies.approve(
                definition.strategy_id,
                definition.version,
                approved_by=900,
                approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            self.assertTrue(
                strategies.is_approved(definition.strategy_id, definition.version)
            )

            detail = handlers.check_strategy_health(NOW)

            self.assertEqual(detail, "suspended=1")
            self.assertEqual(
                strategies.get_record(definition.strategy_id, definition.version).status,
                "suspended",
            )


if __name__ == "__main__":
    unittest.main()
