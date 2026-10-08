from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import re
from typing import Callable, Literal

from .brokers import BrokerOrderReceipt, BrokerRouter
from .execution import ApprovalGate, OrderRejected
from .guidance import (
    GuideInput,
    TradeGuide,
    format_invalidation_condition,
    format_reason,
    format_trade_guide,
    generate_trade_guide,
)
from .holdings import parse_holding_message
from .intent import (
    InvestmentIntent,
    merge_investment_intents,
    parse_investment_intent,
)
from .intraday import SQLiteIntradayBarStore
from .market import MarketSnapshot, format_market_snapshot
from .money import parse_account_equity_message
from .order_request import parse_order_request
from .portfolio import (
    PersonalizedStrategyPlan,
    RiskOverlaySignal,
    compose_strategy_plan,
    format_strategy_plan,
)
from .research_queue import SQLiteResearchQueue
from .paper_trading import (
    KOREA_TIMEZONE,
    PaperStrategyEvaluation,
    PaperTradeFill,
    SQLitePaperTradingLedger,
    estimate_paper_transaction_cost,
)
from .state import Holding, SQLiteUserStateStore
from .strategies import StrategyRecord
from .strategy_reporting import (
    format_strategy_overview_requirement,
    format_strategy_report,
)
from .strategy_store import SQLiteStrategyStore, StrategyNotFound
from .telegram import TelegramBotClient, format_intent_confirmation, parse_telegram_update
from .trading_calendar import KrxTradingCalendar, TradingCalendar


EventKind = Literal[
    "ignored",
    "intent_saved",
    "holding_saved",
    "watchlist_saved",
    "broker_selected",
    "account_profile_saved",
    "guide_sent",
    "approval_requested",
    "order_submitted",
    "owner_rejected",
    "kill_switch_changed",
    "strategy_changed",
    "help_sent",
    "error",
]


@dataclass(frozen=True)
class ServiceEvent:
    kind: EventKind
    user_id: int | None = None
    detail: str = ""
    receipt: BrokerOrderReceipt | None = None


GuideInputFactory = Callable[
    [int, str, InvestmentIntent, Holding | None, datetime], GuideInput
]


_GUIDE_PATTERNS = (
    re.compile(r"^가이드\s+([A-Za-z0-9.]{1,20})$"),
    re.compile(
        r"^([A-Za-z0-9.]{1,20})\s*(?:어때|어때요|살까|팔까|봐줘|분석해줘)\??$"
    ),
    re.compile(r"^([A-Za-z0-9.]{1,20})\s+가이드$"),
)
_BROKER_NAMES = {
    "토스": "toss_invest",
    "토스증권": "toss_invest",
    "미래에셋": "mirae_asset",
    "미래에셋증권": "mirae_asset",
}
_BROKER_PATTERN = re.compile(
    r"^(?:증권사\s+)?(토스|토스증권|미래에셋|미래에셋증권)"
    r"(?:으로|로)?\s*(?:할게|해줘|설정|사용)?$"
)
_WATCHLIST_PATTERNS = (
    re.compile(
        r"^관심\s+([A-Za-z0-9.]{1,20}(?:\s*,\s*[A-Za-z0-9.]{1,20})*)$"
    ),
    re.compile(
        r"^([A-Za-z0-9.]{1,20}(?:\s*,\s*[A-Za-z0-9.]{1,20})*)\s+"
        r"관심종목(?:에)?\s*(?:추가|넣어줘)$"
    ),
)
_OWNER_PREFIXES = ("승인 ", "거절 ", "거래중단", "RESET KILL SWITCH")
_HELP_COMMANDS = {"도움말", "도움", "시작", "/start", "/help"}
_PAPER_STATUS_COMMANDS = {"모의현황", "검증현황", "모의투자 현황"}
_DAILY_GUIDE_COMMANDS = {"오늘 가이드", "오늘의 가이드", "오늘 어때", "오늘 뭐해"}
_DAILY_GUIDE_ENABLE_COMMANDS = {"매일 가이드 보내줘", "매일 가이드 켜줘", "자동 가이드 켜줘"}
_DAILY_GUIDE_DISABLE_COMMANDS = {"매일 가이드 그만", "자동 가이드 꺼줘", "가이드 알림 꺼줘"}
_RESEARCH_CANDIDATE_COMMANDS = {"연구후보", "전략후보", "새 알고리즘"}
_STRATEGY_APPROVE_PATTERN = re.compile(
    r"^전략승인\s+([a-z0-9_]{1,100})\s+(\d+\.\d+\.\d+)$"
)
_STRATEGY_SUSPEND_PATTERN = re.compile(
    r"^전략중지\s+([a-z0-9_]{1,100})\s+(\d+\.\d+\.\d+)$"
)
_STRATEGY_REPORT_PATTERN = re.compile(
    r"^전략보고\s+([a-z0-9_]{1,100})\s+(\d+\.\d+\.\d+)$"
)
_ACTION_LABELS = {
    "buy": "매수 검토",
    "hold": "유지",
    "partial_sell": "부분매도 검토",
    "full_sell": "전량매도 검토",
    "review_alternative": "대체종목 검토",
    "stop_trading": "거래중단",
}
_INTENT_HINTS = (
    "목표",
    "수익",
    "장기",
    "단타",
    "스윙",
    "현금",
    "손실",
    "손절",
    "거래당",
    "하루",
    "일일",
    "가이드만",
    "자동주문",
    "자동 매매",
    "주문마다 승인",
)
_HELP_TEXT = """사용 예시
• 내 투자금은 1,000만원
• 보유 005930 10주 평단 70000원
• 005930 10주를 7만원에 갖고 있어
• 관심 005930, 000660
• 장기안정 60% 단타 40%, 연간 8%, 하루 손실 1%
• 005930 어때?
• 토스로 할게 또는 미래에셋으로 할게
• 내 설정 / 내 보유 / 관심종목
• 모의현황
• 오늘 가이드
• 매일 가이드 보내줘 / 자동 가이드 꺼줘
• 가이드 확인 후 매매는 증권사 앱에서 직접 진행
• 소유자: 전략현황 / 전략보고 <ID> <버전> / 전략승인 <ID> <버전> / 전략중지 <ID> <버전>
이 봇은 가이드만 제공하며 주문을 생성하거나 전송하지 않습니다. 매매는 가이드를 확인한 뒤 증권사 앱에서 직접 진행하세요."""


def _is_guide_only_order_request(text: str) -> bool:
    """Recognize trade commands that must never enter the order pipeline.

    Conversational questions such as ``매수해도 돼?`` and investment settings
    containing ``주문마다 승인`` are intentionally left for the guide/intent
    parsers.  Concrete order wording is blocked even when the stock name is
    written in Korean (for example, ``삼성전자 사줘``).
    """
    normalized = re.sub(r"\s+", " ", text.strip())
    if parse_order_request(normalized) is not None:
        return True
    if re.search(r"(?:해도\s*돼|괜찮|어때|가능)\??$", normalized):
        return False
    return bool(
        re.search(
            r"(?:사\s*줘|팔\s*줘|주문\s*(?:해줘|취소|정정해줘)|취소해줘|정정해줘|"
            r"(?:매수|매도)\s*(?:해줘|해주세요|하자|할게|요청))",
            normalized,
        )
    )


def _guide_only_order_message() -> str:
    return (
        "이 봇은 가이드 전용이라 주문·주문 취소·승인 처리를 하지 않습니다. "
        "가이드를 확인한 뒤 매매는 증권사 앱에서 직접 진행하세요."
    )


class StockGuideService:
    """Provide user-scoped guidance, with explicit internal paper research opt-in."""

    def __init__(
        self,
        *,
        bot: TelegramBotClient,
        store: SQLiteUserStateStore,
        approval_gate: ApprovalGate,
        broker_router: BrokerRouter,
        guide_input_factory: GuideInputFactory,
        strategy_records: Callable[[], list[StrategyRecord]] | None = None,
        paper_ledger: SQLitePaperTradingLedger | None = None,
        mirae_paper_commission_rate: float | None = None,
        daily_guide_max_symbols: int = 10,
        research_queue: SQLiteResearchQueue | None = None,
        strategy_store: SQLiteStrategyStore | None = None,
        intraday_store: SQLiteIntradayBarStore | None = None,
        internal_paper_orders_enabled: bool = False,
        daily_notification_hour_kst: int = 9,
        daily_notification_minute_kst: int = 10,
        trading_calendar: TradingCalendar | None = None,
    ) -> None:
        if approval_gate.owner_user_id != bot.owner_user_id:
            raise ValueError("Telegram owner and approval owner must match")
        self.bot = bot
        self.store = store
        self.approval_gate = approval_gate
        self.approval_gate.set_account_equity_source(store.get_account_equity)
        self.broker_router = broker_router
        self.guide_input_factory = guide_input_factory
        self.strategy_records = strategy_records
        self.paper_ledger = paper_ledger
        self.mirae_paper_commission_rate = mirae_paper_commission_rate
        if not 1 <= daily_guide_max_symbols <= 50:
            raise ValueError("daily guide symbol limit must be between 1 and 50")
        self.daily_guide_max_symbols = daily_guide_max_symbols
        self.research_queue = research_queue
        self.strategy_store = strategy_store
        self.intraday_store = intraday_store
        self.internal_paper_orders_enabled = internal_paper_orders_enabled
        if not 0 <= daily_notification_hour_kst <= 23:
            raise ValueError("daily notification hour must be between 0 and 23")
        if not 0 <= daily_notification_minute_kst <= 59:
            raise ValueError("daily notification minute must be between 0 and 59")
        self.daily_notification_hour_kst = daily_notification_hour_kst
        self.daily_notification_minute_kst = daily_notification_minute_kst
        # Resolve the package lazily so ordinary conversational handling remains
        # available if a deployment omitted the scheduler dependency. Scheduled
        # market work itself still fails closed when it attempts resolution.
        self.trading_calendar = trading_calendar
        self._latest_guides: dict[tuple[int, str], TradeGuide] = {}

    def _current_risk_overlay_signals(
        self, now: datetime
    ) -> tuple[RiskOverlaySignal, ...]:
        if self.paper_ledger is None:
            return ()
        korean_date = now.astimezone(KOREA_TIMEZONE).date()
        return tuple(
            RiskOverlaySignal(
                strategy_id=day.strategy_id,
                version=day.version,
                exposure_multiplier=day.target_exposure,
            )
            for day in self.paper_ledger.list_latest_model_days()
            if 0 <= (korean_date - day.trading_date).days <= 3
            and day.api_healthy
            and day.costs_complete
            and day.critical_incidents == 0
        )

    def _current_strategy_paper_days(
        self, record: StrategyRecord, now: datetime
    ) -> tuple[bool, int | None]:
        """Return whether evidence is connected and its safely current count."""
        return self._current_paper_days(
            record.definition.strategy_id,
            record.definition.version,
            requires_intraday_data=record.definition.requires_intraday_data,
            now=now,
        )

    def _current_paper_days(
        self,
        strategy_id: str,
        version: str,
        *,
        requires_intraday_data: bool,
        now: datetime,
    ) -> tuple[bool, int | None]:
        if requires_intraday_data:
            if self.intraday_store is None:
                return False, None
            counter = self.intraday_store.qualified_paper_days
        else:
            if self.paper_ledger is None:
                return False, None
            counter = self.paper_ledger.completed_days
        try:
            calendar = self.trading_calendar or KrxTradingCalendar()
            count = counter(
                strategy_id,
                version,
                as_of=now.astimezone(KOREA_TIMEZONE).date(),
                trading_calendar=calendar,
            )
            return True, count
        except Exception:
            return True, None

    @staticmethod
    def _paper_progress_label(connected: bool, count: int | None) -> str:
        if not connected:
            return "모의원장 미연결, 확인 불가"
        if count is None:
            return "거래일 근거 확인 불가"
        return f"{min(count, 30)}/30일"

    def _apply_personalized_plan(
        self,
        intent: InvestmentIntent,
        value: GuideInput,
        now: datetime,
    ) -> tuple[GuideInput, PersonalizedStrategyPlan | None]:
        if self.strategy_records is None:
            return value, None
        plan = compose_strategy_plan(
            intent,
            self.strategy_records(),
            value.market,
            risk_overlay_signals=self._current_risk_overlay_signals(now),
        )
        effective_weight = sum(
            item.weight_pct
            for item in plan.allocations
            if item.strategy_id == value.strategy_id
            and item.version == value.strategy_version
        )
        return replace(value, strategy_allocation_pct=effective_weight), plan

    def handle_update(
        self, payload: dict, *, now: datetime
    ) -> ServiceEvent:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        message = parse_telegram_update(payload)
        if message is None or not self.bot.is_allowed_user(message.user_id):
            return ServiceEvent("ignored")
        self.store.upsert_user_channel(
            message.user_id, message.chat_id, updated_at=now
        )

        # Production is guidance-only. Keep the old paper-order path available
        # solely for explicit internal research tests; do this before any
        # approval/proposal or broker state can be created.
        if not self.internal_paper_orders_enabled:
            is_approval_command = (
                message.text in {"승인", "거절"}
                or message.text.startswith("승인 ")
                or message.text.startswith("거절 ")
            )
            if is_approval_command or _is_guide_only_order_request(message.text):
                self.bot.send_text(message.chat_id, _guide_only_order_message())
                return ServiceEvent("help_sent", message.user_id, "guide_only_order")

        if message.text in _DAILY_GUIDE_ENABLE_COMMANDS:
            if (
                self.store.load_intent(message.user_id, as_of=now) is None
                or not self.store.list_symbols_for_user(message.user_id)
            ):
                self.bot.send_text(
                    message.chat_id,
                    "자동 가이드를 켜려면 먼저 투자조건과 보유·관심종목을 저장해주세요.",
                )
                return ServiceEvent(
                    "help_sent", message.user_id, "missing_daily_inputs"
                )
            self.store.set_daily_guide_enabled(
                message.user_id, True, updated_at=now
            )
            self.bot.send_text(
                message.chat_id,
                f"매일 가이드 알림을 켰어요. KRX 개장일 {self.daily_notification_hour_kst:02d}:"
                f"{self.daily_notification_minute_kst:02d} 전후에 보내며 주문은 만들지 않습니다.",
            )
            return ServiceEvent("help_sent", message.user_id, "daily_notification_on")

        if message.text in _DAILY_GUIDE_DISABLE_COMMANDS:
            self.store.set_daily_guide_enabled(
                message.user_id, False, updated_at=now
            )
            self.bot.send_text(message.chat_id, "매일 가이드 알림을 껐어요.")
            return ServiceEvent("help_sent", message.user_id, "daily_notification_off")

        if message.text in _HELP_COMMANDS:
            self.bot.send_text(message.chat_id, _HELP_TEXT)
            return ServiceEvent("help_sent", message.user_id, "help")

        if message.text == "내 설정":
            saved = self.store.load_intent(message.user_id, as_of=now)
            account_equity = self.store.get_account_equity(message.user_id)
            if saved is None and account_equity is None:
                self.bot.send_text(
                    message.chat_id,
                    "저장된 설정이 없어요. 예: 장기 70% 현금 30%, 연간 8%, 하루 손실 1%",
                )
                return ServiceEvent("help_sent", message.user_id, "missing_intent")
            rendered = (
                format_intent_confirmation(saved)
                if saved is not None
                else "저장된 투자조건은 아직 없어요."
            )
            if account_equity is not None:
                rendered = f"계좌 평가금: {account_equity:,.0f}원\n" + rendered
            self.bot.send_text(message.chat_id, rendered)
            return ServiceEvent("help_sent", message.user_id, "intent_status")

        if message.text == "내 보유":
            holdings = self.store.list_holdings(message.user_id)
            if not holdings:
                rendered = "저장된 보유종목이 없어요."
            else:
                rendered = "보유종목\n" + "\n".join(
                    f"• {item.stock_code} {item.quantity}주 / 평단 {item.average_price:,.0f}원"
                    for item in holdings
                )
            self.bot.send_text(message.chat_id, rendered)
            return ServiceEvent("help_sent", message.user_id, "holdings_status")

        if message.text == "관심종목":
            symbols = self.store.list_watchlist_symbols(message.user_id)
            rendered = (
                "관심종목: " + ", ".join(symbols)
                if symbols
                else "저장된 관심종목이 없어요."
            )
            self.bot.send_text(message.chat_id, rendered)
            return ServiceEvent("help_sent", message.user_id, "watchlist_status")

        if message.text in _PAPER_STATUS_COMMANDS:
            if self.paper_ledger is not None:
                valuations = self.paper_ledger.list_latest_valuations(message.user_id)
                model_days = self.paper_ledger.list_latest_model_days()
            else:
                valuations = ()
                model_days = ()
            intraday_days = (
                self.intraday_store.list_latest_paper_days(
                    "intraday_opening_range_breakout", "1.0.0"
                )
                if self.intraday_store is not None
                else ()
            )
            if not valuations and not model_days and not intraday_days:
                rendered = (
                    "아직 평가된 모의투자 기록이 없어요. "
                    "투자 설정과 보유·관심종목을 저장하면 장 마감 후 자동 평가합니다."
                )
            else:
                lines = ["내부 모의검증 현황 (실계좌가 아닙니다)"]
                for value in valuations:
                    _, days = self._current_paper_days(
                        value.strategy_id,
                        value.version,
                        requires_intraday_data=False,
                        now=now,
                    )
                    healthy = (
                        value.api_healthy
                        and value.costs_complete
                        and value.critical_incidents == 0
                    )
                    lines.extend(
                        (
                            f"• {value.strategy_id} v{value.version}",
                            f"  기준일 {value.trading_date.isoformat()} / "
                            f"평가금 {value.equity:,.0f}원",
                            f"  일일 {value.daily_pnl_pct:+.2f}% / "
                            f"누적 {value.cumulative_return_pct:+.2f}%",
                            f"  검증 인정 {f'{days}일' if days is not None else '확인 불가'} / "
                            f"상태 {'정상' if healthy else '확인 필요'}",
                        )
                    )
                for value in model_days:
                    _, days = self._current_paper_days(
                        value.strategy_id,
                        value.version,
                        requires_intraday_data=False,
                        now=now,
                    )
                    healthy = (
                        value.api_healthy
                        and value.costs_complete
                        and value.critical_incidents == 0
                    )
                    lines.extend(
                        (
                            f"• 공용 위험모델 {value.strategy_id} v{value.version}",
                            f"  {value.market_symbol} / 기준일 "
                            f"{value.trading_date.isoformat()}",
                            f"  다음 노출 {value.target_exposure * 100:.0f}% / "
                            f"일일 {value.daily_return_pct:+.2f}% / "
                            f"누적 {value.cumulative_return_pct:+.2f}%",
                            f"  검증 인정 {f'{min(days, 30)}/30일' if days is not None else '확인 불가'} / "
                            f"상태 {'정상' if healthy else '확인 필요'}",
                        )
                    )
                if intraday_days:
                    _, qualified = self._current_paper_days(
                        "intraday_opening_range_breakout",
                        "1.0.0",
                        requires_intraday_data=True,
                        now=now,
                    )
                    for value in intraday_days[:5]:
                        healthy = (
                            value.api_healthy
                            and value.costs_complete
                            and value.critical_incidents == 0
                        )
                        lines.extend(
                            (
                                f"• 단타 모의 {value.symbol} / 5분 ORB",
                                f"  기준일 {value.trading_date.isoformat()} / "
                                f"일일 {value.daily_return_pct:+.2f}% / "
                                f"평가금 {value.equity:,.0f}원",
                                f"  검증 인정 {f'{min(qualified, 30)}/30일' if qualified is not None else '확인 불가'} / "
                                f"상태 {'정상' if healthy else '확인 필요'}",
                            )
                        )
                rendered = "\n".join(lines)
            self.bot.send_text(message.chat_id, rendered)
            return ServiceEvent("help_sent", message.user_id, "paper_status")

        if message.text in _DAILY_GUIDE_COMMANDS:
            intent = self.store.load_intent(message.user_id, as_of=now)
            symbols = self.store.list_symbols_for_user(message.user_id)
            if intent is None or not symbols:
                self.bot.send_text(
                    message.chat_id,
                    "먼저 투자 설정과 보유·관심종목을 알려주세요. "
                    "예: 장기안정 60% 단타 40%, 연간 8%, 하루 손실 1%",
                )
                return ServiceEvent("help_sent", message.user_id, "missing_daily_inputs")
            lines = ["오늘의 종목별 가이드"]
            failures = 0
            digest_market: MarketSnapshot | None = None
            digest_plan = None
            for stock_code in symbols[: self.daily_guide_max_symbols]:
                try:
                    value = self.guide_input_factory(
                        message.user_id,
                        stock_code,
                        intent,
                        self.store.get_holding(message.user_id, stock_code),
                        now,
                    )
                    value, plan = self._apply_personalized_plan(intent, value, now)
                    guide = generate_trade_guide(value, now=now)
                except Exception:
                    failures += 1
                    continue
                if digest_market is None:
                    digest_market = value.market
                    digest_plan = plan
                self._latest_guides[(message.user_id, stock_code)] = guide
                if self.paper_ledger is not None:
                    self.paper_ledger.record_evaluation(
                        PaperStrategyEvaluation(
                            guide.strategy_id,
                            guide.strategy_version,
                            message.user_id,
                            stock_code,
                            guide.action,
                            guide.confidence,
                            guide.generated_at,
                            value.api_healthy,
                            0 if value.api_healthy else 1,
                        )
                    )
                fraction = (
                    f" / 제안비중 {guide.suggested_fraction * 100:.0f}%"
                    if guide.suggested_fraction > 0
                    else ""
                )
                alternative = (
                    f" / 대안 {guide.alternative_stock_code}"
                    f"({guide.alternative_score:+.2f}, "
                    f"신뢰 "
                    f"{f'{guide.alternative_confidence * 100:.0f}%' if guide.alternative_confidence is not None else '미확인'})"
                    if guide.alternative_stock_code
                    else ""
                )
                lines.extend(
                    (
                        f"• {stock_code}: {_ACTION_LABELS[guide.action]}{fraction}{alternative}",
                        f"  신뢰 {guide.confidence * 100:.0f}% / "
                        f"거래당 손실한도 {guide.loss_limit_pct:g}%·하루 중단선 "
                        f"{guide.max_daily_loss_pct:g}%",
                        f"  근거: {format_reason(guide.reasons[-1])}",
                        f"  무효화: {format_invalidation_condition(guide.invalidation_condition)}",
                    )
                )
            omitted = max(0, len(symbols) - self.daily_guide_max_symbols)
            if omitted:
                lines.append(f"• 종목 제한으로 {omitted}개 생략")
            if failures:
                lines.append(f"• 데이터 오류로 {failures}개 평가 실패")
            if len(lines) == 1:
                lines.append("현재 생성 가능한 가이드가 없습니다.")
            if digest_market is not None:
                lines[1:1] = format_market_snapshot(digest_market).splitlines()
                if digest_plan is not None:
                    lines.extend(("", format_strategy_plan(digest_plan)))
            lines.append("가이드 확인 후 매매는 증권사 앱에서 직접 진행하세요. 이 봇은 주문을 만들지 않습니다.")
            self.bot.send_text(message.chat_id, "\n".join(lines))
            return ServiceEvent("guide_sent", message.user_id, "daily_digest")

        if message.text in _RESEARCH_CANDIDATE_COMMANDS:
            if not self.bot.is_owner(message.user_id):
                return ServiceEvent("ignored")
            if self.research_queue is None:
                rendered = "학술 전략 후보 큐가 연결되지 않았습니다."
            else:
                candidates = self.research_queue.list_pending_academic_candidates()
                if not candidates:
                    rendered = "검토 대기 중인 신규 학술 전략 후보가 없습니다."
                else:
                    lines = ["신규 학술 전략 후보(자동 채택 금지)"]
                    for item in candidates[:10]:
                        lines.extend(
                            (
                                f"• {item.title}",
                                f"  발행 {item.published_on} / DOI {item.doi}",
                                f"  근거 {item.matched_terms}",
                                f"  {item.landing_url}",
                            )
                        )
                    if len(candidates) > 10:
                        lines.append(f"• 추가 {len(candidates) - 10}건")
                    lines.append(
                        "후보는 별도 구현·한국시장 백테스트·워크포워드·모의검증 전에는 사용하지 않습니다."
                    )
                    rendered = "\n".join(lines)
            self.bot.send_text(message.chat_id, rendered)
            return ServiceEvent("help_sent", message.user_id, "research_candidates")

        report_match = _STRATEGY_REPORT_PATTERN.fullmatch(message.text)
        if message.text == "전략현황" or message.text.startswith(
            ("전략승인", "전략중지")
        ) or report_match is not None:
            if not self.bot.is_owner(message.user_id):
                return ServiceEvent("ignored")
            if self.strategy_store is None:
                self.bot.send_text(message.chat_id, "전략 저장소가 연결되지 않았습니다.")
                return ServiceEvent("error", message.user_id, "strategy_store_missing")
            if message.text == "전략현황":
                status_names = {
                    "research": "연구중",
                    "rejected": "거부",
                    "backtested": "백테스트",
                    "paper_trading": "모의검증중",
                    "validated": "승인대기",
                    "approved": "활성",
                    "suspended": "중지",
                }
                lines = ["전략 현황"]
                for record in self.strategy_store.list_records():
                    lines.append(
                        f"• {record.definition.strategy_id} v{record.definition.version}: "
                        f"{status_names[record.status]}"
                    )
                    lines.append(
                        "  " + format_strategy_overview_requirement(record)
                    )
                    if record.status == "validated":
                        lines.append(
                            f"  승인: 전략승인 {record.definition.strategy_id} "
                            f"{record.definition.version}"
                        )
                self.bot.send_text(message.chat_id, "\n".join(lines))
                return ServiceEvent("help_sent", message.user_id, "strategy_status")
            try:
                if report_match:
                    strategy_id, version = report_match.groups()
                    record = self.strategy_store.get_record(strategy_id, version)
                    connected, current_days = self._current_strategy_paper_days(record, now)
                    paper_progress = self._paper_progress_label(connected, current_days)
                    checked_on = self.strategy_store.latest_validation_on(
                        strategy_id, version
                    )
                    self.bot.send_text(
                        message.chat_id,
                        format_strategy_report(
                            record,
                            checked_on=checked_on,
                            paper_progress=paper_progress,
                        ),
                    )
                    return ServiceEvent("help_sent", message.user_id, "strategy_report")
                approve_match = _STRATEGY_APPROVE_PATTERN.fullmatch(message.text)
                suspend_match = _STRATEGY_SUSPEND_PATTERN.fullmatch(message.text)
                if approve_match:
                    strategy_id, version = approve_match.groups()
                    current_record = self.strategy_store.get_record(strategy_id, version)
                    if current_record.status != "approved":
                        connected, current_days = self._current_strategy_paper_days(
                            current_record, now
                        )
                        if connected and current_days is None:
                            self.bot.send_text(
                                message.chat_id,
                                "전략 활성화 거부: 현재 모의검증일의 거래일 근거를 확인할 수 없습니다.",
                            )
                            return ServiceEvent(
                                "error", message.user_id, "strategy_paper_evidence_unavailable"
                            )
                        if connected and current_days is not None and current_days < 30:
                            self.bot.send_text(
                                message.chat_id,
                                f"전략 활성화 거부: 현재 정상 모의검증이 {min(current_days, 30)}/30일입니다. "
                                "30일을 채운 뒤 다시 검토해주세요.",
                            )
                            return ServiceEvent(
                                "error", message.user_id, "insufficient_current_paper_evidence"
                            )
                    record = self.strategy_store.approve(
                        strategy_id,
                        version,
                        approved_by=message.user_id,
                        approved_at=now,
                    )
                    self.bot.send_text(
                        message.chat_id,
                        f"전략 활성화 완료: {record.definition.strategy_id} "
                        f"v{record.definition.version}",
                    )
                    return ServiceEvent(
                        "strategy_changed", message.user_id, "approved"
                    )
                if suspend_match:
                    self.strategy_store.suspend(
                        suspend_match.group(1),
                        suspend_match.group(2),
                        actor_user_id=message.user_id,
                        occurred_at=now,
                    )
                    self.bot.send_text(
                        message.chat_id,
                        f"전략 중지 완료: {suspend_match.group(1)} "
                        f"v{suspend_match.group(2)}",
                    )
                    return ServiceEvent(
                        "strategy_changed", message.user_id, "suspended"
                    )
                raise ValueError(
                    "형식: 전략승인 <strategy_id> <version> 또는 "
                    "전략중지 <strategy_id> <version>"
                )
            except (ValueError, StrategyNotFound) as exc:
                self.bot.send_text(message.chat_id, f"전략 변경 거부: {exc}")
                return ServiceEvent("error", message.user_id, str(exc))

        if self.bot.is_owner(message.user_id) and message.text.startswith(_OWNER_PREFIXES):
            try:
                result = self.bot.handle_owner_command(
                    payload, self.approval_gate, now=now
                )
                if result is None:
                    return ServiceEvent("ignored")
                if result.status == "approved" and result.authorized_order is not None:
                    receipt = self.broker_router.submit_authorized_order(
                        result.authorized_order
                    )
                    proposal = result.authorized_order.proposal
                    if receipt.simulated and self.paper_ledger is not None:
                        self.paper_ledger.record_fill(
                            PaperTradeFill(
                                proposal_id=proposal.proposal_id,
                                strategy_id=proposal.strategy_id,
                                version=proposal.strategy_version,
                                user_id=proposal.requester_user_id,
                                broker_provider=receipt.provider,
                                stock_code=proposal.stock_code,
                                side=proposal.side,
                                quantity=proposal.quantity,
                                fill_price=proposal.limit_price,
                                filled_at=result.authorized_order.authorized_at,
                                transaction_cost=proposal.estimated_transaction_cost,
                            )
                        )
                    requester_user_id = (
                        proposal.requester_user_id
                    )
                    self.store.record_order_execution(
                        receipt.proposal_id,
                        requester_user_id,
                        executed_at=now,
                    )
                    mode = "모의주문" if receipt.simulated else "실주문"
                    self.bot.send_text(
                        message.chat_id,
                        f"{mode} 처리 완료: {receipt.proposal_id}",
                    )
                    return ServiceEvent(
                        "order_submitted", message.user_id, receipt.proposal_id, receipt
                    )
                if result.status == "rejected":
                    return ServiceEvent(
                        "owner_rejected", message.user_id, result.proposal_id or ""
                    )
                return ServiceEvent("kill_switch_changed", message.user_id, result.status)
            except OrderRejected as exc:
                self.bot.send_text(message.chat_id, f"처리 거부: {exc}")
                return ServiceEvent("error", message.user_id, str(exc))

        broker_match = _BROKER_PATTERN.fullmatch(message.text)
        if broker_match:
            provider = _BROKER_NAMES[broker_match.group(1)]
            if provider not in self.broker_router.available_providers():
                self.bot.send_text(message.chat_id, "해당 증권사 연결이 구성되지 않았습니다.")
                return ServiceEvent("error", message.user_id, "broker_not_configured")
            self.store.set_broker_provider(
                message.user_id, provider, updated_at=now  # type: ignore[arg-type]
            )
            mode = (
                "토스증권(가이드 전용)"
                if provider == "toss_invest"
                else "미래에셋증권(가이드 전용)"
            )
            self.bot.send_text(message.chat_id, f"증권사 설정: {mode}")
            return ServiceEvent("broker_selected", message.user_id, provider)

        account_equity = parse_account_equity_message(message.text)
        if account_equity is not None:
            self.store.set_account_equity(
                message.user_id,
                account_equity.account_equity,
                updated_at=now,
            )
            self.bot.send_text(
                message.chat_id,
                f"투자금 저장: {account_equity.account_equity:,.0f}원. "
                "가이드 비중과 손실예산을 이 금액을 기준으로 계산합니다. 주문은 생성하지 않습니다.",
            )
            return ServiceEvent(
                "account_profile_saved",
                message.user_id,
                f"{account_equity.account_equity:g}",
            )

        holding = parse_holding_message(message.text)
        if holding is not None:
            self.store.upsert_holding(
                Holding(
                    user_id=message.user_id,
                    stock_code=holding.stock_code,
                    quantity=holding.quantity,
                    average_price=holding.average_price,
                    updated_at=now,
                )
            )
            self.bot.send_text(
                message.chat_id,
                f"보유정보 저장: {holding.stock_code} {holding.quantity}주, "
                f"평단 {holding.average_price:g}원",
            )
            return ServiceEvent("holding_saved", message.user_id, holding.stock_code)

        watchlist_match = next(
            (
                match
                for pattern in _WATCHLIST_PATTERNS
                if (match := pattern.fullmatch(message.text)) is not None
            ),
            None,
        )
        if watchlist_match:
            symbols = [
                value.strip().upper()
                for value in watchlist_match.group(1).split(",")
            ]
            self.store.add_watchlist_symbols(
                message.user_id, symbols, added_at=now
            )
            self.bot.send_text(
                message.chat_id, "관심종목 저장: " + ", ".join(symbols)
            )
            return ServiceEvent(
                "watchlist_saved", message.user_id, ",".join(symbols)
            )

        guide_match = next(
            (
                match
                for pattern in _GUIDE_PATTERNS
                if (match := pattern.fullmatch(message.text)) is not None
            ),
            None,
        )
        if guide_match:
            stock_code = guide_match.group(1).upper()
            intent = self.store.load_intent(message.user_id, as_of=now)
            if intent is None:
                self.bot.send_text(
                    message.chat_id,
                    "먼저 목표와 배분을 알려주세요. 예: 장기안정 60% 단타 40%, 연간 8% 목표",
                )
                return ServiceEvent("help_sent", message.user_id, "missing_intent")
            value = self.guide_input_factory(
                message.user_id,
                stock_code,
                intent,
                self.store.get_holding(message.user_id, stock_code),
                now,
            )
            value, plan = self._apply_personalized_plan(intent, value, now)
            guide = generate_trade_guide(value, now=now)
            if (
                self.paper_ledger is not None
                and self.approval_gate.policy.environment == "mock"
            ):
                self.paper_ledger.record_evaluation(
                    PaperStrategyEvaluation(
                        strategy_id=guide.strategy_id,
                        version=guide.strategy_version,
                        user_id=message.user_id,
                        stock_code=stock_code,
                        action=guide.action,
                        confidence=guide.confidence,
                        evaluated_at=guide.generated_at,
                        api_healthy=value.api_healthy,
                        critical_incidents=(0 if value.api_healthy else 1),
                    )
                )
            self._latest_guides[(message.user_id, stock_code)] = guide
            response = format_trade_guide(guide) + "\n\n" + format_market_snapshot(value.market)
            if plan is not None:
                response += "\n\n" + format_strategy_plan(plan)
            self.bot.send_text(message.chat_id, response)
            return ServiceEvent("guide_sent", message.user_id, guide.action)

        order_request = parse_order_request(message.text)
        if order_request is not None:
            korean_trading_date = now.astimezone(KOREA_TIMEZONE).date()
            self.approval_gate.state.roll_trading_day(now)
            self.approval_gate.state.orders_today = max(
                self.approval_gate.state.orders_today,
                self.store.count_all_order_executions(korean_trading_date),
            )
            stock_code = order_request.stock_code
            guide = self._latest_guides.get((message.user_id, stock_code))
            if guide is None:
                self.bot.send_text(
                    message.chat_id, f"먼저 '가이드 {stock_code}'를 요청해주세요."
                )
                return ServiceEvent("help_sent", message.user_id, "missing_guide")
            intent = self.store.load_intent(message.user_id, as_of=now)
            if intent is None:
                return ServiceEvent("help_sent", message.user_id, "missing_intent")
            if intent.approval_level == "guide_only":
                self.bot.send_text(
                    message.chat_id,
                    "현재 설정은 가이드만 제공하며 주문 요청은 차단합니다.",
                )
                return ServiceEvent("error", message.user_id, "guide_only_mode")
            if (
                intent.max_trades_per_day is not None
                and self.store.count_order_executions(
                    message.user_id, korean_trading_date
                )
                >= intent.max_trades_per_day
            ):
                self.bot.send_text(
                    message.chat_id,
                    "설정한 하루 거래 횟수에 도달해 새 주문을 중단합니다.",
                )
                return ServiceEvent("error", message.user_id, "user_frequency_limit")
            expected_side = "buy" if guide.action == "buy" else "sell"
            if (
                order_request.requested_side is not None
                and order_request.requested_side != expected_side
            ):
                self.bot.send_text(
                    message.chat_id,
                    "요청한 매수·매도 방향이 직전 가이드와 달라 주문을 만들지 않았습니다. "
                    f"현재 가이드 방향: {'매수' if expected_side == 'buy' else '매도'}.",
                )
                return ServiceEvent("error", message.user_id, "order_side_mismatch")
            quantity = order_request.quantity
            limit_price = order_request.limit_price
            holding = self.store.get_holding(message.user_id, stock_code)
            broker_provider = self.store.get_broker_provider(message.user_id)
            estimated_cost = estimate_paper_transaction_cost(
                broker_provider=broker_provider,
                side=expected_side,
                notional=quantity * limit_price,
                trading_date=korean_trading_date,
                mirae_commission_rate=self.mirae_paper_commission_rate,
            )
            costs_complete = estimated_cost is not None
            cost_assumption = (
                "수수료·매도세금·5bp 불리한 슬리피지 포함"
                if costs_complete
                else "계약 수수료 미확인; 모의 검증일 불인정, 실거래 차단"
            )
            try:
                challenge = self.approval_gate.propose(
                    guide,
                    quantity=quantity,
                    limit_price=limit_price,
                    now=now,
                    requester_user_id=message.user_id,
                    broker_provider=broker_provider,
                    owned_quantity=(holding.quantity if holding is not None else None),
                    estimated_transaction_cost=estimated_cost,
                    transaction_costs_complete=costs_complete,
                    transaction_cost_assumption=cost_assumption,
                )
            except OrderRejected as exc:
                self.bot.send_text(message.chat_id, f"주문 요청 거부: {exc}")
                return ServiceEvent("error", message.user_id, str(exc))
            self.bot.send_owner_approval_request(challenge)
            self.bot.send_text(
                message.chat_id,
                f"소유자 승인을 요청했습니다: {challenge.proposal.proposal_id}",
            )
            return ServiceEvent(
                "approval_requested", message.user_id, challenge.proposal.proposal_id
            )

        if not any(hint in message.text for hint in _INTENT_HINTS):
            self.bot.send_text(
                message.chat_id,
                "투자조건이나 명령을 확인하지 못했어요. '도움말'을 보내 예시를 확인해주세요.",
            )
            return ServiceEvent("help_sent", message.user_id, "unrecognized_message")
        intent = parse_investment_intent(message.text)
        previous = self.store.load_intent(message.user_id, as_of=now)
        if previous is not None:
            intent = merge_investment_intents(previous, intent)
        self.store.save_intent(message.user_id, intent, updated_at=now)
        self.bot.send_text(message.chat_id, format_intent_confirmation(intent))
        return ServiceEvent("intent_saved", message.user_id)

    def send_due_daily_guides(self, now: datetime) -> str:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("notification time must be timezone-aware")
        local = now.astimezone(KOREA_TIMEZONE)
        calendar = self.trading_calendar or KrxTradingCalendar()
        target = calendar.daily_guide_start(
            now,
            configured_hour=self.daily_notification_hour_kst,
            configured_minute=self.daily_notification_minute_kst,
        )
        if target is None:
            return "market_closed"
        if not target <= local < target + timedelta(minutes=10):
            return "outside_window"
        sent = 0
        failures: list[str] = []
        for recipient in self.store.list_due_daily_guide_recipients(local.date()):
            try:
                event = self.handle_update(
                    {
                        "update_id": 0,
                        "message": {
                            "text": "오늘 가이드",
                            "chat": {"id": recipient.chat_id, "type": "private"},
                            "from": {"id": recipient.user_id},
                        },
                    },
                    now=now,
                )
                if event.kind == "ignored" or event.kind == "error":
                    raise RuntimeError(f"daily guide unavailable: {event.detail}")
                self.store.mark_daily_guide_sent(recipient.user_id, local.date())
                sent += 1
            except Exception as exc:
                failures.append(f"{recipient.user_id}:{type(exc).__name__}")
        if failures:
            raise RuntimeError(
                f"sent={sent},failed={len(failures)} ({','.join(failures)})"
            )
        return f"sent={sent}"
