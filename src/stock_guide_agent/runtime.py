from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

from .academic_discovery import CrossrefAcademicDiscoveryClient
from .brokers import (
    BrokerRouter,
    MiraeAssetBrokerGateway,
    TossBrokerGateway,
)
from .execution import ApprovalGate, ExecutionPolicy, ExecutionState
from .execution_safety import SQLiteExecutionSafetyStore
from .cache import SQLiteMarketCache
from .collectors import EcosClient, OpenDartClient
from .research_queue import SQLiteResearchQueue
from .scheduled_handlers import ScheduledDataHandlers
from .scheduler import (
    DEFAULT_JOBS,
    JobRunResult,
    ScheduledJobRunner,
    SQLiteJobStateStore,
)
from .service import ServiceEvent, StockGuideService
from .state import SQLiteUserStateStore
from .strategy_store import SQLiteStrategyStore
from .telegram import TelegramBotClient
from .tokens import TossTokenManager
from .toss import TossInvestClient
from .toss_market import TossGuideInputFactory
from .supplemental import CachedSupplementalObservationProvider
from .source_monitor import SQLiteSourceFingerprintStore, StrategySourceMonitor
from .news import NaverNewsHubClient
from .paper_trading import SQLitePaperTradingLedger
from .intraday import SQLiteIntradayBarStore
from .background_jobs import BackgroundJobRunner
from .runtime_settings import validate_runtime_settings
from .trading_calendar import KrxTradingCalendar
from .messaging import SQLiteMessageStore


@dataclass(frozen=True)
class PollResult:
    events: tuple[ServiceEvent, ...]
    next_offset: int | None
    failures: int
    scheduled_jobs: tuple[JobRunResult, ...] = ()


class AgentRuntime:
    def __init__(
        self,
        bot: TelegramBotClient,
        service: StockGuideService,
        *,
        scheduler: ScheduledJobRunner | None = None,
        message_store: SQLiteMessageStore | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.scheduler = scheduler
        self.message_store = message_store
        self.next_offset: int | None = None
        self._recovery_checked = False
        self._poll_retry_at: datetime | None = None
        self._poll_backoff_seconds = 1

    def poll_once(
        self,
        *,
        now: datetime | None = None,
        timeout_seconds: int = 30,
    ) -> PollResult:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        else:
            current = current.astimezone(timezone.utc)
        if self.next_offset is None and self.message_store is not None:
            try:
                self.next_offset = self.message_store.get_next_offset()
            except Exception:
                # A corrupt/unavailable message store must fail closed.  The
                # next poll gets another chance after the backing store recovers.
                self._notify_owner("메시지 상태 저장소를 읽지 못했습니다. 다음 실행에서 다시 시도합니다.")
                return PollResult((), self.next_offset, 1)
        if not self._recovery_checked and self.message_store is not None:
            try:
                recovered = tuple(self.message_store.recover_interrupted(now=current))
                self._recovery_checked = True
                if recovered:
                    ids = ", ".join(str(item) for item in recovered[:20])
                    self._notify_owner(
                        f"이전 실행에서 처리 중이던 업데이트가 중단되었습니다: {ids}. "
                        "처리 결과가 불명확하므로 해당 요청을 다시 보내 주세요."
                    )
            except Exception:
                self._notify_owner("메시지 복구 상태를 확인하지 못했습니다. 다음 실행에서 다시 시도합니다.")
                return PollResult((), self.next_offset, 1)

        # A pending outbox must be retried even when Telegram polling is down.
        if self.message_store is not None:
            try:
                if self.message_store.reset_after_idle(now=current):
                    self.next_offset = None
            except Exception:
                return PollResult((), self.next_offset, 1)
        flush_failures = self._flush_outbox(current)
        scheduler_failures = 0
        if self.scheduler:
            try:
                scheduled = self.scheduler.run_due(current)
            except Exception:
                scheduled = ()
                scheduler_failures = 1
                self._notify_owner("정기 작업을 실행하지 못했습니다. 다음 실행에서 다시 시도합니다.")
        else:
            scheduled = ()
        for result in scheduled:
            if not result.success:
                self._notify_owner(f"정기 작업을 완료하지 못했습니다 (작업 ID: {result.job_id}).")
            elif result.job_id == "external_strategy_research" and result.detail != "queued=0":
                self._notify_owner("전략 리서치 검토 큐가 갱신되었습니다.")
            elif (
                result.job_id == "academic_strategy_discovery"
                and "queued=0" not in result.detail
            ):
                self._notify_owner("신규 학술 전략 후보가 추가되었습니다.")
        if self._poll_retry_at is not None and current < self._poll_retry_at:
            return PollResult((), self.next_offset, flush_failures + scheduler_failures, scheduled)
        try:
            updates = self.bot.get_updates(
                offset=self.next_offset, timeout_seconds=timeout_seconds
            )
            self._poll_retry_at = None
            self._poll_backoff_seconds = 1
        except Exception:
            retry = self._poll_backoff_seconds
            self._poll_retry_at = current + timedelta(seconds=retry)
            self._poll_backoff_seconds = min(60, retry * 2)
            return PollResult((), self.next_offset, 1 + flush_failures + scheduler_failures, scheduled)
        events: list[ServiceEvent] = []
        failures = flush_failures + scheduler_failures
        normalized: list[tuple[int, dict[str, Any]]] = []
        for payload in updates:
            if not isinstance(payload, dict):
                failures += 1
                continue
            raw_id = payload.get("update_id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                failures += 1
                continue
            update_id = raw_id
            if update_id < 0:
                failures += 1
                continue
            normalized.append((update_id, {**payload, "update_id": update_id}))
        for update_id, payload in sorted(normalized, key=lambda item: item[0]):
            if self.message_store is not None:
                try:
                    if not self.message_store.claim_update(update_id, now=current):
                        continue
                except Exception:
                    failures += 1
                    self._notify_owner(f"업데이트 {update_id}의 처리 상태를 저장하지 못했습니다.")
                    break
            try:
                events.append(self.service.handle_update(payload, now=current))
            except Exception:  # isolate one malformed/provider-failed update
                failures += 1
                if self.message_store is not None:
                    try:
                        self.message_store.finish_update(update_id, status="failed", now=current)
                    except Exception:
                        pass
                self._notify_owner(f"업데이트 {update_id} 처리에 실패했습니다. 요청을 다시 보내 주세요.")
            else:
                if self.message_store is not None:
                    try:
                        self.message_store.finish_update(update_id, status="completed", now=current)
                    except Exception:
                        failures += 1
                        self._notify_owner(f"업데이트 {update_id}의 완료 상태를 저장하지 못했습니다.")
            finally:
                self.next_offset = max(self.next_offset or 0, update_id + 1)
        failures += self._flush_outbox(datetime.now(timezone.utc) if now is None else current)
        return PollResult(tuple(events), self.next_offset, failures, scheduled)

    def _notify_owner(self, text: str) -> None:
        try:
            self.bot.send_text(self.bot.owner_chat_id, text)
        except Exception:
            pass

    def close(self) -> None:
        close = getattr(self.scheduler, "close", None)
        if close is not None:
            close(wait=True)
        self._flush_outbox(datetime.now(timezone.utc))

    def _flush_outbox(self, now: datetime) -> int:
        flush = getattr(self.bot, "flush_outbox", None)
        if flush is None:
            return 0
        try:
            return int(flush(now=now, limit=20) or 0)
        except Exception:
            return 1


def build_runtime_from_env() -> AgentRuntime:
    environment = os.environ.get("AGENT_EXECUTION_ENV", "mock").strip().lower()
    if environment == "live":
        raise ValueError(
            "AGENT_EXECUTION_ENV=live is not supported; this is a guide-only runtime"
        )
    if environment != "mock":
        raise ValueError("AGENT_EXECUTION_ENV must be mock")

    runtime_env = {**os.environ, **validate_runtime_settings(os.environ)}

    data_dir = Path(runtime_env.get("AGENT_DATA_DIR", ".")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    safety_store = SQLiteExecutionSafetyStore(data_dir / "users.sqlite3")
    kill_switch_active, kill_switch_reason = safety_store.load()
    bot = TelegramBotClient.from_env()
    toss_client = TossInvestClient.from_env()
    token_manager = TossTokenManager(toss_client)
    account_equity_text = runtime_env.get("AGENT_ACCOUNT_EQUITY_KRW", "").strip()
    state = ExecutionState(
        max_daily_loss_pct=float(runtime_env.get("AGENT_MAX_DAILY_LOSS_PCT", "1.5")),
        kill_switch_active=kill_switch_active,
        kill_switch_reason=kill_switch_reason,
        account_equity=(float(account_equity_text) if account_equity_text else None),
        _kill_switch_persist=safety_store.save,
    )
    live_enabled = runtime_env.get("AGENT_LIVE_TRADING_ENABLED", "").lower() == "true"
    policy = ExecutionPolicy(
        environment=environment,  # type: ignore[arg-type]
        live_trading_enabled=live_enabled,
        max_order_notional=float(
            runtime_env.get("AGENT_MAX_ORDER_NOTIONAL", "1000000")
        ),
        max_orders_per_day=int(runtime_env.get("AGENT_MAX_ORDERS_PER_DAY", "5")),
    )
    gate = ApprovalGate(policy, state, owner_user_id=bot.owner_user_id)
    strategy_store = SQLiteStrategyStore(data_dir / "strategies.sqlite3")
    market_cache = SQLiteMarketCache(data_dir / "market_cache.sqlite3")
    user_store = SQLiteUserStateStore(data_dir / "users.sqlite3")
    paper_ledger = SQLitePaperTradingLedger(data_dir / "paper_trading.sqlite3")
    intraday_store = SQLiteIntradayBarStore(data_dir / "intraday_bars.sqlite3")
    strategy_id = "time_series_momentum"
    strategy_version = "1.0.0"
    long_term_strategy_id = "long_term_absolute_momentum"
    long_term_strategy_version = "1.0.0"
    guide_factory = TossGuideInputFactory(
        toss_client,
        token_manager,
        state,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_approved=lambda: strategy_store.is_approved(
            strategy_id, strategy_version
        ),
        supplemental_observations=CachedSupplementalObservationProvider(
            market_cache
        ),
        alternative_symbols=user_store.list_candidate_symbols,
        long_term_strategy_id=long_term_strategy_id,
        long_term_strategy_version=long_term_strategy_version,
        long_term_strategy_approved=lambda: strategy_store.is_approved(
            long_term_strategy_id, long_term_strategy_version
        ),
    )
    router = BrokerRouter(
        [
            TossBrokerGateway(toss_client, token_manager),
            MiraeAssetBrokerGateway(),
        ]
    )
    research_queue = SQLiteResearchQueue(data_dir / "research.sqlite3")
    message_store = SQLiteMessageStore(data_dir / "messages.sqlite3")
    trading_calendar = KrxTradingCalendar(overrides_path=runtime_env.get("KRX_CALENDAR_OVERRIDES"))
    service = StockGuideService(
        trading_calendar=trading_calendar,
        bot=bot,
        store=user_store,
        approval_gate=gate,
        broker_router=router,
        guide_input_factory=guide_factory,
        strategy_records=strategy_store.list_records,
        strategy_store=strategy_store,
        intraday_store=intraday_store,
        paper_ledger=paper_ledger,
        research_queue=research_queue,
        mirae_paper_commission_rate=(
            float(os.environ["MIRAE_PAPER_COMMISSION_RATE"])
            if runtime_env.get("MIRAE_PAPER_COMMISSION_RATE")
            else None
        ),
        daily_notification_hour_kst=int(
            runtime_env.get("DAILY_GUIDE_HOUR_KST", "9")
        ),
        daily_notification_minute_kst=int(
            runtime_env.get("DAILY_GUIDE_MINUTE_KST", "10")
        ),
    )
    source_monitor = StrategySourceMonitor(
        SQLiteSourceFingerprintStore(data_dir / "source_fingerprints.sqlite3")
    )
    open_dart = (
        OpenDartClient.from_env() if runtime_env.get("OPENDART_API_KEY") else None
    )
    ecos = EcosClient.from_env() if runtime_env.get("ECOS_API_KEY") else None
    news_client = (
        NaverNewsHubClient.from_env()
        if runtime_env.get("NAVER_API_HUB_CLIENT_ID")
        and runtime_env.get("NAVER_API_HUB_CLIENT_SECRET")
        else None
    )
    ecos_queries = _parse_ecos_queries(runtime_env.get("ECOS_QUERIES", ""))
    scheduled_handlers = ScheduledDataHandlers(
        trading_calendar=trading_calendar,
        toss_client=toss_client,
        token_source=token_manager,
        user_store=user_store,
        market_cache=market_cache,
        strategy_store=strategy_store,
        research_queue=research_queue,
        open_dart=open_dart,
        ecos=ecos,
        ecos_queries=ecos_queries,
        overseas_symbols=_parse_symbols(
            runtime_env.get("OVERSEAS_BENCHMARK_SYMBOLS", "SPY,QQQ")
        ),
        source_monitor=source_monitor,
        news_client=news_client,
        news_max_symbols=int(runtime_env.get("NEWS_MAX_SYMBOLS", "20")),
        execution_state=state,
        paper_ledger=paper_ledger,
        paper_initial_cash=float(
            runtime_env.get("PAPER_INITIAL_CAPITAL_KRW", "10000000")
        ),
        guide_input_factory=guide_factory,
        paper_evaluation_max_symbols=int(
            runtime_env.get("PAPER_EVALUATION_MAX_SYMBOLS", "20")
        ),
        academic_discovery=(
            CrossrefAcademicDiscoveryClient(
                mailto=runtime_env.get("CROSSREF_MAILTO") or None
            )
            if runtime_env.get("CROSSREF_DISCOVERY_ENABLED", "true").lower()
            == "true"
            else None
        ),
        academic_discovery_rows=int(
            runtime_env.get("CROSSREF_DISCOVERY_ROWS", "20")
        ),
        intraday_store=intraday_store,
        intraday_minimum_bars=int(
            runtime_env.get("INTRADAY_MINIMUM_BARS", "300")
        ),
        intraday_archive_max_symbols=int(
            runtime_env.get("INTRADAY_ARCHIVE_MAX_SYMBOLS", "5")
        ),
    )
    handlers = {
        "daily_user_digest": service.send_due_daily_guides,
        "toss_invest_market_data": scheduled_handlers.refresh_prices,
        "toss_invest_investor_flow": scheduled_handlers.refresh_investor_flow,
        "toss_invest_overseas": scheduled_handlers.refresh_overseas,
        "paper_portfolio_valuation": scheduled_handlers.value_paper_portfolios,
        "paper_strategy_evaluation": scheduled_handlers.evaluate_paper_strategies,
        "volatility_model_evaluation": (
            scheduled_handlers.evaluate_volatility_model
        ),
        "long_term_model_evaluation": scheduled_handlers.evaluate_long_term_model,
        "intraday_bar_archive": scheduled_handlers.archive_intraday_bars,
        "strategy_health_check": scheduled_handlers.check_strategy_health,
        "strategy_validation": scheduled_handlers.validate_ready_strategies,
        "academic_strategy_discovery": (
            scheduled_handlers.discover_academic_strategies
        ),
        "external_strategy_research": scheduled_handlers.enqueue_strategy_research,
    }
    if toss_client.account_sequence:
        handlers["toss_account_risk"] = scheduled_handlers.refresh_account_risk
    if open_dart is not None:
        handlers["open_dart_disclosures"] = scheduled_handlers.refresh_disclosures
    if ecos is not None and ecos_queries:
        handlers["ecos_macro"] = scheduled_handlers.refresh_macro
    if news_client is not None:
        handlers["naver_material_news"] = scheduled_handlers.refresh_news
    if scheduled_handlers.academic_discovery is None:
        handlers.pop("academic_strategy_discovery", None)
    enabled = tuple(item for item in DEFAULT_JOBS if item.job_id in handlers)
    scheduler = ScheduledJobRunner(
        SQLiteJobStateStore(
            data_dir / "jobs.sqlite3",
            start=datetime.now(timezone.utc),
            definitions=enabled,
        ),
        handlers,
    )
    bot.message_store = message_store
    scheduler.store.recover_interrupted(datetime.now(timezone.utc))
    return AgentRuntime(bot, service, scheduler=BackgroundJobRunner(scheduler), message_store=message_store)


def _parse_ecos_queries(
    raw: str,
) -> tuple[tuple[str, str, str | None, float | None], ...]:
    if not raw.strip():
        return ()
    result: list[tuple[str, str, str | None, float | None]] = []
    for entry in raw.split(";"):
        fields = [value.strip() for value in entry.split(",")]
        if len(fields) not in {2, 3, 4} or not fields[0] or not fields[1]:
            raise ValueError(
                "ECOS_QUERIES must use code,cycle[,item[,polarity]] entries; "
                "polarity must be 1 or -1"
            )
        item = fields[2] or None if len(fields) >= 3 else None
        polarity = float(fields[3]) if len(fields) == 4 else None
        if polarity not in {None, -1.0, 1.0}:
            raise ValueError("ECOS query polarity must be 1 or -1")
        result.append((fields[0], fields[1], item, polarity))
    return tuple(result)


def _parse_symbols(raw: str) -> tuple[str, ...]:
    return tuple(value.strip().upper() for value in raw.split(",") if value.strip())

