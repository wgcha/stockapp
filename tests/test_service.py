import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.brokers import (
    BrokerRouter,
    MiraeAssetBrokerGateway,
    TossBrokerGateway,
)
from stock_guide_agent.academic_discovery import AcademicStrategyCandidate
from stock_guide_agent.execution import ApprovalGate, ExecutionPolicy, ExecutionState
from stock_guide_agent.guidance import GuideInput
from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.intraday import SQLiteIntradayBarStore
from stock_guide_agent.market import MarketSnapshot
from stock_guide_agent.paper_trading import (
    PaperModelObservation,
    PaperPortfolioValuation,
    SQLitePaperTradingLedger,
)
from stock_guide_agent.research_queue import SQLiteResearchQueue
from stock_guide_agent.service import StockGuideService
from stock_guide_agent.state import SQLiteUserStateStore
from stock_guide_agent.strategies import (
    PUBLIC_STRATEGY_CATALOG,
    StrategyRecord,
    ValidationReport,
)
from stock_guide_agent.strategy_store import SQLiteStrategyStore
from stock_guide_agent.telegram import TelegramBotClient
from stock_guide_agent.toss import TossInvestClient, TossInvestToken
from stock_guide_agent.trading_calendar import TradingSession


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class FixedTradingCalendar:
    """Keeps this service unit test independent from packaged calendar data."""

    def session_for(self, value):
        if isinstance(value, datetime):
            local = value.astimezone(timezone(timedelta(hours=9)))
        else:
            local = datetime.combine(
                value, datetime.min.time(), tzinfo=timezone(timedelta(hours=9))
            )
        opens_at = local.replace(hour=9, minute=0, second=0, microsecond=0)
        closes_at = local.replace(hour=15, minute=30, second=0, microsecond=0)
        return TradingSession(local.date(), opens_at, closes_at)

    def is_after_close_evaluation(self, now):
        return now.astimezone(timezone(timedelta(hours=9))) >= self.session_for(now).evaluation_available_at

    def daily_guide_start(self, now, *, configured_hour, configured_minute):
        session = self.session_for(now)
        configured = session.opens_at.replace(
            hour=configured_hour, minute=configured_minute
        )
        return max(configured, session.opens_at + timedelta(minutes=10))


class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(200, {}, {"ok": True, "result": {}})


def update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id},
        },
    }


def guide_factory(user_id, stock_code, intent, holding, now):
    market = MarketSnapshot(
        as_of=now,
        regime="risk_on",
        score=0.6,
        confidence=0.8,
        category_scores={"price": 0.7, "investor_flow": 0.5},
        evidence=[],
    )
    return GuideInput(
        stock_code=stock_code,
        market=market,
        strategy_id="approved_strategy",
        strategy_approved=True,
        signal_score=0.8,
        signal_confidence=0.8,
        max_trade_loss_pct=intent.max_trade_loss_pct or 0.5,
        max_daily_loss_pct=intent.max_daily_loss_pct or 1.5,
        daily_pnl_pct=0,
    )


def strategy_service(root, strategy_store, *, paper_ledger=None, trading_calendar=None, transport=None):
    return StockGuideService(
        bot=TelegramBotClient(
            "TOKEN", 900, allowed_user_ids={30}, transport=transport or FakeTransport()
        ),
        store=SQLiteUserStateStore(root / "state.sqlite3"),
        approval_gate=ApprovalGate(
            ExecutionPolicy(), ExecutionState(), owner_user_id=900
        ),
        broker_router=BrokerRouter([]),
        guide_input_factory=guide_factory,
        strategy_store=strategy_store,
        paper_ledger=paper_ledger,
        trading_calendar=trading_calendar,
    )


class ServiceFlowTests(unittest.TestCase):
    def test_strategy_report_is_owner_only_and_explains_unimplemented_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            strategies = SQLiteStrategyStore(root / "strategies.sqlite3")
            definition = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "factor_momentum"
            )
            strategies.apply_validation(
                ValidationReport(
                    definition.strategy_id,
                    definition.version,
                    "korean_equities",
                    8,
                    False,
                    1,
                    300,
                    0.4,
                    0.18,
                    0.75,
                    12,
                    False,
                    True,
                    None,
                    cost_source_urls=("https://example.test/costs",),
                    cost_assumptions=("수수료율 미확정",),
                ),
                checked_on=NOW.date(),
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=SQLiteUserStateStore(root / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter([]),
                guide_input_factory=guide_factory,
                strategy_store=strategies,
            )

            command = f"전략보고 {definition.strategy_id} {definition.version}"
            member = service.handle_update(update(31, 30, command), now=NOW)
            owner = service.handle_update(update(32, 900, command), now=NOW)
            rendered = transport.requests[-1].json_body["text"]
            missing = service.handle_update(
                update(33, 900, "전략보고 unknown_strategy 1.0.0"), now=NOW
            )

            self.assertEqual(member.kind, "ignored")
            self.assertEqual(owner.detail, "strategy_report")
            self.assertIn("factor_momentum v1.0.0", rendered)
            self.assertIn("최근 검증: 2026-07-17", rendered)
            self.assertIn("아직 구현되지 않았습니다", rendered)
            self.assertIn("표본 외 검증이 없습니다", rendered)
            self.assertIn("https://example.test/costs", rendered)
            self.assertIn("수수료율 미확정", rendered)
            self.assertIn("기술 검증 통과는 전략 활성화와 다르며", rendered)
            self.assertEqual(missing.kind, "error")
            self.assertFalse(strategies.is_approved(definition.strategy_id, definition.version))

    def test_new_owner_activation_requires_30_current_days_when_ledger_connected(self) -> None:
        definition = next(
            item
            for item in PUBLIC_STRATEGY_CATALOG
            if item.strategy_id == "time_series_momentum"
        )
        for available_days, calendar, expected_detail in (
            (1, FixedTradingCalendar(), "insufficient_current_paper_evidence"),
            (30, FixedTradingCalendar(), "approved"),
            (30, object(), "strategy_paper_evidence_unavailable"),
        ):
            with self.subTest(available_days=available_days, calendar=type(calendar).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                strategies = SQLiteStrategyStore(root / "strategies.sqlite3")
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
                ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
                for index in range(available_days):
                    trading_date = NOW.astimezone(timezone(timedelta(hours=9))).date() - timedelta(days=index + 1)
                    ledger.record_model_observation(
                        PaperModelObservation(
                            definition.strategy_id,
                            definition.version,
                            "KOSPI",
                            trading_date,
                            3000 + index,
                            0.4,
                            True,
                        ),
                        initial_equity=10_000_000,
                        buy_cost_rate=0.00015,
                        sell_cost_rate=0.00215,
                    )
                service = strategy_service(
                    root,
                    strategies,
                    paper_ledger=ledger,
                    trading_calendar=calendar,
                )

                event = service.handle_update(
                    update(
                        40,
                        900,
                        f"전략승인 {definition.strategy_id} {definition.version}",
                    ),
                    now=NOW + timedelta(hours=8),
                )

                self.assertEqual(event.detail, expected_detail)
                self.assertEqual(
                    strategies.is_approved(definition.strategy_id, definition.version),
                    expected_detail == "approved",
                )

    def test_strategy_report_separates_live_days_from_validation_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "time_series_momentum"
            )
            strategies = SQLiteStrategyStore(root / "strategies.sqlite3")
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
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            ledger.record_model_observation(
                PaperModelObservation(
                    definition.strategy_id,
                    definition.version,
                    "KOSPI",
                    NOW.astimezone(timezone(timedelta(hours=9))).date(),
                    3000,
                    0.4,
                    True,
                ),
                initial_equity=10_000_000,
                buy_cost_rate=0.00015,
                sell_cost_rate=0.00215,
            )
            transport = FakeTransport()
            service = strategy_service(
                root,
                strategies,
                paper_ledger=ledger,
                trading_calendar=FixedTradingCalendar(),
                transport=transport,
            )

            service.handle_update(
                update(
                    41,
                    900,
                    f"전략보고 {definition.strategy_id} {definition.version}",
                ),
                now=NOW + timedelta(hours=8),
            )
            rendered = transport.requests[-1].json_body["text"]

            self.assertIn("정상 모의검증 진행: 1/30일", rendered)
            self.assertIn("보고서 모의검증 60일", rendered)

    def test_only_owner_can_activate_and_suspend_a_validated_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            strategies = SQLiteStrategyStore(root / "strategies.sqlite3")
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
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=SQLiteUserStateStore(root / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                strategy_records=strategies.list_records,
                strategy_store=strategies,
            )
            command = f"전략승인 {definition.strategy_id} {definition.version}"

            member = service.handle_update(update(1, 30, command), now=NOW)
            status = service.handle_update(update(2, 900, "전략현황"), now=NOW)
            rendered_status = transport.requests[-1].json_body["text"]
            approved = service.handle_update(update(3, 900, command), now=NOW)
            suspended = service.handle_update(
                update(
                    4,
                    900,
                    f"전략중지 {definition.strategy_id} {definition.version}",
                ),
                now=NOW,
            )

            self.assertEqual(member.kind, "ignored")
            self.assertEqual(status.detail, "strategy_status")
            self.assertIn("승인대기", rendered_status)
            self.assertEqual(approved.detail, "approved")
            self.assertEqual(suspended.detail, "suspended")
            self.assertFalse(strategies.is_approved(definition.strategy_id, definition.version))
            events = strategies.list_activation_events(
                definition.strategy_id, definition.version
            )
            self.assertEqual([event.action for event in events], ["approved", "suspended"])
            self.assertTrue(all(event.actor_user_id == 900 for event in events))

    def test_guide_applies_only_fresh_healthy_approved_risk_overlay(self) -> None:
        volatility = next(
            definition
            for definition in PUBLIC_STRATEGY_CATALOG
            if definition.strategy_id == "volatility_managed_exposure"
        )
        swing = next(
            definition
            for definition in PUBLIC_STRATEGY_CATALOG
            if definition.strategy_id == "time_series_momentum"
        )
        for age_days, expected_overlay in ((0, True), (4, False)):
            with self.subTest(age_days=age_days), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                transport = FakeTransport()
                ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
                ledger.record_model_observation(
                    PaperModelObservation(
                        volatility.strategy_id,
                        volatility.version,
                        "KOSPI",
                        NOW.date() - timedelta(days=age_days),
                        3000,
                        0.4,
                        True,
                    ),
                    initial_equity=10_000_000,
                    buy_cost_rate=0.00015,
                    sell_cost_rate=0.00215,
                )
                service = StockGuideService(
                    bot=TelegramBotClient(
                        "TOKEN", 900, allowed_user_ids={30}, transport=transport
                    ),
                    store=SQLiteUserStateStore(root / "state.sqlite3"),
                    approval_gate=ApprovalGate(
                        ExecutionPolicy(), ExecutionState(), owner_user_id=900
                    ),
                    broker_router=BrokerRouter(
                        [
                            TossBrokerGateway(
                                TossInvestClient("CLIENT", "SECRET"),
                                TossInvestToken("TOKEN", "Bearer", None),
                            )
                        ]
                    ),
                    guide_input_factory=guide_factory,
                    strategy_records=lambda: [
                        StrategyRecord(swing, status="approved"),
                        StrategyRecord(volatility, status="approved"),
                    ],
                    paper_ledger=ledger,
                )
                service.handle_update(
                    update(1, 30, "스윙 100%, 연간 8%, 하루 손실 1%"), now=NOW
                )
                service.handle_update(
                    update(2, 30, "보유 005930 10주 평단 70000원"), now=NOW
                )
                service.handle_update(update(3, 30, "가이드 005930"), now=NOW)
                rendered = transport.requests[-1].json_body["text"]

                if expected_overlay:
                    self.assertIn("위험조정 40%", rendered)
                    self.assertIn("현금·미배정: 60%", rendered)
                else:
                    self.assertNotIn("위험조정", rendered)
                    self.assertIn("현금·미배정: 0%", rendered)

    def test_only_owner_can_view_academic_strategy_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            queue = SQLiteResearchQueue(root / "research.sqlite3")
            queue.enqueue_academic_candidates(
                (
                    AcademicStrategyCandidate(
                        "10.1000/new.1",
                        "Momentum Strategy for Stock Portfolio Trading",
                        NOW.date(),
                        "https://doi.org/10.1000/new.1",
                        ("Ada Kim",),
                        ("stock", "portfolio", "momentum", "strategy"),
                    ),
                ),
                now=NOW,
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=SQLiteUserStateStore(root / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                research_queue=queue,
            )

            member = service.handle_update(update(1, 30, "연구후보"), now=NOW)
            owner = service.handle_update(update(2, 900, "연구후보"), now=NOW)
            rendered = transport.requests[-1].json_body["text"]

            self.assertEqual(member.kind, "ignored")
            self.assertEqual(owner.detail, "research_candidates")
            self.assertIn("Momentum Strategy", rendered)
            self.assertIn("자동 채택 금지", rendered)

    def test_daily_guide_summarizes_all_saved_symbols_without_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            store = SQLiteUserStateStore(root / "state.sqlite3")
            gate = ApprovalGate(
                ExecutionPolicy(), ExecutionState(), owner_user_id=900
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=gate,
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                internal_paper_orders_enabled=True,
            )
            service.handle_update(
                update(1, 30, "장기안정 60% 단타 40%, 연간 8%, 하루 손실 1%"),
                now=NOW,
            )
            service.handle_update(
                update(2, 30, "보유 005930 10주 평단 70000원"), now=NOW
            )
            service.handle_update(update(3, 30, "관심 000660"), now=NOW)

            result = service.handle_update(update(4, 30, "오늘 가이드"), now=NOW)
            rendered = transport.requests[-1].json_body["text"]

            self.assertEqual(result.kind, "guide_sent")
            self.assertEqual(result.detail, "daily_digest")
            self.assertIn("000660: 매수 검토", rendered)
            self.assertIn("005930: 매수 검토", rendered)
            self.assertIn("거래당 손실한도", rendered)
            self.assertIn("무효화:", rendered)
            self.assertIn("시황: 위험선호", rendered)
            self.assertIn("가격 +0.70", rendered)
            self.assertIn("수급 +0.50", rendered)
            self.assertIn("증권사 앱에서 직접", rendered)
            self.assertEqual(gate.state.orders_today, 0)
            order = service.handle_update(
                update(5, 30, "주문 005930 1주 70000원"), now=NOW
            )
            self.assertEqual(order.kind, "approval_requested")

    def test_opt_in_daily_notification_sends_once_without_creating_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            store = SQLiteUserStateStore(root / "state.sqlite3")
            gate = ApprovalGate(
                ExecutionPolicy(), ExecutionState(), owner_user_id=900
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
                ),
                store=store,
                approval_gate=gate,
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                daily_notification_hour_kst=9,
                daily_notification_minute_kst=10,
                trading_calendar=FixedTradingCalendar(),
            )
            for user_id in (30, 40):
                service.handle_update(
                    update(user_id, user_id, "장기 100%, 연간 8%, 하루 손실 1%"),
                    now=NOW,
                )
                service.handle_update(
                    update(user_id + 10, user_id, "관심 005930"), now=NOW
                )
                service.handle_update(
                    update(user_id + 20, user_id, "매일 가이드 보내줘"), now=NOW
                )
            service.handle_update(
                update(99, 40, "자동 가이드 꺼줘"), now=NOW
            )
            scheduled_time = NOW + timedelta(minutes=10)

            first = service.send_due_daily_guides(scheduled_time)
            second = service.send_due_daily_guides(scheduled_time)

            self.assertEqual(first, "sent=1")
            self.assertEqual(second, "sent=0")
            digests = [
                request
                for request in transport.requests
                if request.json_body
                and request.json_body.get("chat_id") == 30
                and "오늘의 종목별 가이드" in request.json_body.get("text", "")
            ]
            self.assertEqual(len(digests), 1)
            self.assertEqual(gate.state.orders_today, 0)
            owner_approvals = [
                request
                for request in transport.requests
                if request.json_body
                and request.json_body.get("chat_id") == 900
                and "승인 요청" in request.json_body.get("text", "")
            ]
            self.assertEqual(owner_approvals, [])

    def test_restart_restores_global_daily_order_limit_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            store = SQLiteUserStateStore(root / "state.sqlite3")
            for index in range(5):
                store.record_order_execution(
                    f"prior-{index}", 40, executed_at=NOW
                )
            gate = ApprovalGate(
                ExecutionPolicy(max_orders_per_day=5),
                ExecutionState(),
                owner_user_id=900,
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
                ),
                store=store,
                approval_gate=gate,
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                internal_paper_orders_enabled=True,
            )
            service.handle_update(
                update(1, 30, "장기 100%, 연간 8%, 주문마다 승인"), now=NOW
            )
            service.handle_update(
                update(2, 30, "보유 005930 1주 평단 70000원"), now=NOW
            )
            service.handle_update(update(3, 30, "가이드 005930"), now=NOW)

            result = service.handle_update(
                update(4, 30, "주문 005930 1주 70000원"), now=NOW
            )

            self.assertEqual(result.kind, "error")
            self.assertIn("daily order count", result.detail)
            self.assertEqual(gate.state.orders_today, 5)

    def test_member_can_read_only_own_paper_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            ledger = SQLitePaperTradingLedger(root / "paper.sqlite3")
            ledger.record_valuation(
                PaperPortfolioValuation(
                    "approved_strategy",
                    "1.0.0",
                    30,
                    NOW.date(),
                    10_000_000,
                    9_000_000,
                    1_100_000,
                    10_100_000,
                    1.0,
                    1.0,
                    {"005930": 70_000},
                    True,
                    True,
                )
            )
            ledger.record_model_observation(
                PaperModelObservation(
                    "volatility_managed_exposure",
                    "1.0.0",
                    "KOSPI",
                    NOW.date(),
                    3000,
                    0.4,
                    True,
                ),
                initial_equity=10_000_000,
                buy_cost_rate=0.00065,
                sell_cost_rate=0.00265,
            )
            ledger.record_valuation(
                PaperPortfolioValuation(
                    "private_strategy",
                    "2.0.0",
                    40,
                    NOW.date(),
                    10_000_000,
                    10_000_000,
                    0,
                    10_000_000,
                    0,
                    0,
                    {},
                    True,
                    True,
                )
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
                ),
                store=SQLiteUserStateStore(root / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                paper_ledger=ledger,
                trading_calendar=FixedTradingCalendar(),
            )

            result = service.handle_update(update(1, 30, "모의현황"), now=NOW)
            rendered = transport.requests[-1].json_body["text"]

            self.assertEqual(result.detail, "paper_status")
            self.assertIn("approved_strategy", rendered)
            self.assertIn("10,100,000원", rendered)
            self.assertIn("검증 인정 0일", rendered)
            self.assertIn("공용 위험모델", rendered)
            self.assertIn("다음 노출 40%", rendered)
            self.assertIn("검증 인정 1/30일", rendered)
            self.assertNotIn("private_strategy", rendered)

    def test_member_can_read_intraday_paper_status_without_portfolio_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            intraday_store = SQLiteIntradayBarStore(root / "intraday.sqlite3")
            intraday_store.record_paper_day(
                strategy_id="intraday_opening_range_breakout",
                version="1.0.0",
                symbol="005930",
                trading_date=NOW.date(),
                opening_range_minutes=5,
                daily_return_pct=0.75,
                initial_equity=10_000_000,
                api_healthy=True,
                costs_complete=True,
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=SQLiteUserStateStore(root / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
                intraday_store=intraday_store,
                trading_calendar=FixedTradingCalendar(),
            )

            result = service.handle_update(update(1, 30, "모의현황"), now=NOW)
            rendered = transport.requests[-1].json_body["text"]

            self.assertEqual(result.detail, "paper_status")
            self.assertIn("단타 모의 005930 / 5분 ORB", rendered)
            self.assertIn("일일 +0.75%", rendered)
            self.assertIn("검증 인정 1/30일", rendered)

    def test_help_and_unknown_text_do_not_overwrite_saved_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            store = SQLiteUserStateStore(Path(directory) / "state.sqlite3")
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )
            service.handle_update(
                update(1, 30, "장기 100%, 연간 8%, 하루 손실 1%"), now=NOW
            )

            help_event = service.handle_update(update(2, 30, "도움말"), now=NOW)
            unknown = service.handle_update(update(3, 30, "확인"), now=NOW)

            self.assertEqual(help_event.kind, "help_sent")
            self.assertEqual(unknown.detail, "unrecognized_message")
            self.assertEqual(store.load_intent(30).target_return_pct, 8)

    def test_separate_natural_language_messages_merge_user_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            store = SQLiteUserStateStore(root / "state.sqlite3")
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )

            service.handle_update(
                update(1, 30, "장기안정 60% 단타 40%, 하루 손실 1%"), now=NOW
            )
            service.handle_update(
                update(2, 30, "이번 주는 매일 3% 목표"), now=NOW
            )
            service.handle_update(
                update(3, 30, "하루 손실은 0.8%로 해줘"), now=NOW
            )

            intent = store.load_intent(30, as_of=NOW)
            assert intent is not None
            self.assertEqual(intent.target_return_pct, 3)
            self.assertEqual(intent.max_daily_loss_pct, 0.8)
            self.assertEqual(len(intent.allocations), 2)

    def test_member_can_read_own_profile_holdings_and_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            store = SQLiteUserStateStore(Path(directory) / "state.sqlite3")
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )
            service.handle_update(
                update(1, 30, "장기 100%, 연간 8%, 하루 손실 1%"), now=NOW
            )
            service.handle_update(update(2, 30, "보유 005930 10주 평단 70000원"), now=NOW)
            service.handle_update(update(3, 30, "관심 000660"), now=NOW)

            details = [
                service.handle_update(update(index, 30, text), now=NOW).detail
                for index, text in enumerate(
                    ("내 설정", "내 보유", "관심종목"), start=4
                )
            ]

            self.assertEqual(
                details, ["intent_status", "holdings_status", "watchlist_status"]
            )

    def test_member_can_save_natural_account_equity_without_affecting_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = FakeTransport()
            store = SQLiteUserStateStore(root / "state.sqlite3")
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
                ),
                store=store,
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )

            result = service.handle_update(
                update(1, 30, "내 투자금은 1,000만원"), now=NOW
            )
            status = service.handle_update(update(2, 30, "내 설정"), now=NOW)

            self.assertEqual(result.kind, "account_profile_saved")
            self.assertEqual(status.detail, "intent_status")
            self.assertEqual(store.get_account_equity(30), 10_000_000)
            self.assertIsNone(store.get_account_equity(40))
            self.assertIn(
                "계좌 평가금: 10,000,000원",
                transport.requests[-1].json_body["text"],
            )

    def test_member_can_register_multiple_watchlist_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            store = SQLiteUserStateStore(Path(directory) / "state.sqlite3")
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )

            result = service.handle_update(
                update(1, 30, "005930, 000660 관심종목에 넣어줘"), now=NOW
            )

            self.assertEqual(result.kind, "watchlist_saved")
            self.assertEqual(store.list_watchlist_symbols(30), ["000660", "005930"])

    def test_guide_only_profile_blocks_order_before_owner_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=SQLiteUserStateStore(Path(directory) / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        )
                    ]
                ),
                guide_input_factory=guide_factory,
            )
            service.handle_update(
                update(1, 30, "장기 100%, 연간 8%, 일일 손실 1%, 가이드만"),
                now=NOW,
            )
            service.handle_update(update(2, 30, "가이드 005930"), now=NOW)

            result = service.handle_update(
                update(3, 30, "주문 005930 1주 70000원"), now=NOW
            )

            self.assertEqual(result.kind, "help_sent")
            self.assertEqual(result.detail, "guide_only_order")
            owner_messages = [
                request
                for request in transport.requests
                if request.json_body and request.json_body.get("chat_id") == 900
            ]
            self.assertEqual(owner_messages, [])

    def test_member_to_owner_to_toss_mock_order_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            bot = TelegramBotClient(
                "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
            )
            gate = ApprovalGate(
                ExecutionPolicy(), ExecutionState(), owner_user_id=900
            )
            paper_ledger = SQLitePaperTradingLedger(
                Path(directory) / "paper_trading.sqlite3"
            )
            service = StockGuideService(
                bot=bot,
                store=SQLiteUserStateStore(Path(directory) / "state.sqlite3"),
                approval_gate=gate,
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        ),
                        MiraeAssetBrokerGateway(),
                    ]
                ),
                guide_input_factory=guide_factory,
                paper_ledger=paper_ledger,
                internal_paper_orders_enabled=True,
            )

            self.assertEqual(
                service.handle_update(update(1, 30, "005930 10주를 7만원에 갖고 있어"), now=NOW).kind,
                "holding_saved",
            )
            self.assertEqual(
                service.handle_update(
                    update(2, 30, "장기안정 60% 단타 40%, 매일 3% 목표"), now=NOW
                ).kind,
                "intent_saved",
            )
            self.assertEqual(
                service.handle_update(update(3, 30, "005930 어때?"), now=NOW).kind,
                "guide_sent",
            )
            self.assertEqual(
                paper_ledger.evaluation_count("approved_strategy", "1.0.0"), 1
            )
            mismatch = service.handle_update(
                update(4, 30, "005930 1주를 7만원에 팔아줘"), now=NOW
            )
            requested = service.handle_update(
                update(5, 30, "005930 1주를 7만원에 사줘"), now=NOW
            )
            self.assertEqual(mismatch.detail, "order_side_mismatch")
            self.assertEqual(requested.kind, "approval_requested")

            owner_request = next(
                request.json_body
                for request in reversed(transport.requests)
                if request.json_body.get("chat_id") == 900
                and "승인 요청" in request.json_body.get("text", "")
            )
            self.assertEqual(owner_request["chat_id"], 900)
            approval_line = next(
                line for line in owner_request["text"].splitlines() if line.startswith("• 모의 승인:")
            )
            owner_command = approval_line.removeprefix("• 모의 승인: ")
            completed = service.handle_update(update(6, 900, owner_command), now=NOW)

            self.assertEqual(completed.kind, "order_submitted")
            self.assertTrue(completed.receipt.simulated)
            self.assertEqual(gate.state.orders_today, 1)
            self.assertEqual(paper_ledger.fill_count(user_id=30), 1)

    def test_one_member_cannot_use_another_members_latest_guide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30, 40}, transport=transport
                ),
                store=SQLiteUserStateStore(Path(directory) / "state.sqlite3"),
                approval_gate=ApprovalGate(
                    ExecutionPolicy(), ExecutionState(), owner_user_id=900
                ),
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        ),
                        MiraeAssetBrokerGateway(),
                    ]
                ),
                guide_input_factory=guide_factory,
                internal_paper_orders_enabled=True,
            )
            service.handle_update(update(1, 30, "장기 100%, 연간 8% 목표"), now=NOW)
            service.handle_update(update(2, 30, "가이드 005930"), now=NOW)

            result = service.handle_update(
                update(3, 40, "주문 005930 1주 70000원"), now=NOW
            )

            self.assertEqual(result.kind, "help_sent")
            self.assertEqual(result.detail, "missing_guide")

    def test_user_can_select_mirae_asset_and_complete_mock_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport()
            store = SQLiteUserStateStore(Path(directory) / "state.sqlite3")
            gate = ApprovalGate(
                ExecutionPolicy(), ExecutionState(), owner_user_id=900
            )
            service = StockGuideService(
                bot=TelegramBotClient(
                    "TOKEN", 900, allowed_user_ids={30}, transport=transport
                ),
                store=store,
                approval_gate=gate,
                broker_router=BrokerRouter(
                    [
                        TossBrokerGateway(
                            TossInvestClient("CLIENT", "SECRET"),
                            TossInvestToken("TOKEN", "Bearer", None),
                        ),
                        MiraeAssetBrokerGateway(),
                    ]
                ),
                guide_input_factory=guide_factory,
                internal_paper_orders_enabled=True,
            )
            selected = service.handle_update(
                update(1, 30, "미래에셋으로 할게"), now=NOW
            )
            service.handle_update(update(2, 30, "장기 100%, 연간 8% 목표"), now=NOW)
            service.handle_update(update(3, 30, "가이드 005930"), now=NOW)
            requested = service.handle_update(
                update(4, 30, "주문 005930 1주 70000원"), now=NOW
            )
            owner_request = next(
                request.json_body["text"]
                for request in reversed(transport.requests)
                if request.json_body.get("chat_id") == 900
                and "주문 승인 요청" in request.json_body.get("text", "")
            )
            approval_line = next(
                line for line in owner_request.splitlines() if line.startswith("• 모의 승인:")
            )
            completed = service.handle_update(
                update(5, 900, approval_line.removeprefix("• 모의 승인: ")), now=NOW
            )

            self.assertEqual(selected.kind, "broker_selected")
            self.assertEqual(store.get_broker_provider(30), "mirae_asset")
            self.assertEqual(requested.kind, "approval_requested")
            self.assertEqual(completed.kind, "order_submitted")
            self.assertEqual(completed.receipt.provider, "mirae_asset")
            self.assertTrue(completed.receipt.simulated)


if __name__ == "__main__":
    unittest.main()
