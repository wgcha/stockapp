from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Callable

from .academic_discovery import CrossrefAcademicDiscoveryClient
from .backtest import run_opening_range_breakout, toss_krx_2026_costs
from .cache import CachedPayload, SQLiteMarketCache
from .execution import ExecutionState
from .guidance import GuideInput, generate_trade_guide
from .intraday import IntradayBar, SQLiteIntradayBarStore
from .collectors import EcosClient, OpenDartClient
from .research_queue import SQLiteResearchQueue
from .news import NaverNewsHubClient, score_material_news
from .paper_trading import (
    KOREA_TIMEZONE,
    PaperModelObservation,
    PaperStrategyEvaluation,
    SQLitePaperTradingLedger,
    estimate_paper_transaction_cost,
)
from .state import SQLiteUserStateStore
from .strategy_store import SQLiteStrategyStore
from .source_monitor import StrategySourceMonitor
from .toss import TossInvestClient, TossInvestToken
from .toss_market import (
    analyze_candles,
    analyze_volatility_target,
    daily_bars_from_toss_candles,
    parse_holdings_daily_pnl_pct,
    parse_price_quote,
)
from .trading_calendar import KrxTradingCalendar, TradingCalendar
from .validation_pipeline import StrategyValidationPipeline


class ScheduledDataHandlers:
    def __init__(
        self,
        *,
        toss_client: TossInvestClient,
        token_source: Callable[[], TossInvestToken],
        user_store: SQLiteUserStateStore,
        market_cache: SQLiteMarketCache,
        strategy_store: SQLiteStrategyStore,
        research_queue: SQLiteResearchQueue,
        open_dart: OpenDartClient | None = None,
        ecos: EcosClient | None = None,
        ecos_queries: tuple[
            tuple[str, str, str | None, float | None], ...
        ] = (),
        overseas_symbols: tuple[str, ...] = ("SPY", "QQQ"),
        source_monitor: StrategySourceMonitor | None = None,
        news_client: NaverNewsHubClient | None = None,
        news_max_symbols: int = 20,
        execution_state: ExecutionState | None = None,
        strategy_health_days: int = 90,
        paper_ledger: SQLitePaperTradingLedger | None = None,
        paper_initial_cash: float = 10_000_000.0,
        guide_input_factory: Callable[..., GuideInput] | None = None,
        paper_evaluation_max_symbols: int = 20,
        academic_discovery: CrossrefAcademicDiscoveryClient | None = None,
        academic_discovery_rows: int = 20,
        volatility_strategy_id: str = "volatility_managed_exposure",
        volatility_strategy_version: str = "1.0.0",
        volatility_market_symbol: str = "KOSPI",
        long_term_strategy_id: str = "long_term_absolute_momentum",
        long_term_strategy_version: str = "1.0.0",
        intraday_store: SQLiteIntradayBarStore | None = None,
        intraday_minimum_bars: int = 300,
        intraday_archive_max_symbols: int = 5,
        intraday_strategy_id: str = "intraday_opening_range_breakout",
        intraday_strategy_version: str = "1.0.0",
        intraday_opening_range_minutes: int = 5,
        trading_calendar: TradingCalendar | None = None,
    ) -> None:
        self.toss_client = toss_client
        self.token_source = token_source
        self.user_store = user_store
        self.market_cache = market_cache
        self.strategy_store = strategy_store
        self.research_queue = research_queue
        self.open_dart = open_dart
        self.ecos = ecos
        self.ecos_queries = ecos_queries
        self.overseas_symbols = overseas_symbols
        self.source_monitor = source_monitor
        self.news_client = news_client
        if not 1 <= news_max_symbols <= 100:
            raise ValueError("news_max_symbols must be between 1 and 100")
        self.news_max_symbols = news_max_symbols
        self.execution_state = execution_state
        self.strategy_health_days = strategy_health_days
        self.paper_ledger = paper_ledger
        if paper_initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        self.paper_initial_cash = paper_initial_cash
        self.guide_input_factory = guide_input_factory
        if not 1 <= paper_evaluation_max_symbols <= 200:
            raise ValueError("paper evaluation symbol limit must be between 1 and 200")
        self.paper_evaluation_max_symbols = paper_evaluation_max_symbols
        self.academic_discovery = academic_discovery
        if not 1 <= academic_discovery_rows <= 100:
            raise ValueError("academic discovery rows must be between 1 and 100")
        self.academic_discovery_rows = academic_discovery_rows
        self.volatility_strategy_id = volatility_strategy_id
        self.volatility_strategy_version = volatility_strategy_version
        if volatility_market_symbol not in {"KOSPI", "KOSDAQ"}:
            raise ValueError("volatility market symbol must be KOSPI or KOSDAQ")
        self.volatility_market_symbol = volatility_market_symbol
        self.long_term_strategy_id = long_term_strategy_id
        self.long_term_strategy_version = long_term_strategy_version
        self.intraday_store = intraday_store
        if not 60 <= intraday_minimum_bars <= 500:
            raise ValueError("intraday minimum bars must be between 60 and 500")
        self.intraday_minimum_bars = intraday_minimum_bars
        if not 0 <= intraday_archive_max_symbols <= 20:
            raise ValueError("intraday archive symbol limit must be between 0 and 20")
        self.intraday_archive_max_symbols = intraday_archive_max_symbols
        self.intraday_strategy_id = intraday_strategy_id
        self.intraday_strategy_version = intraday_strategy_version
        if not 5 <= intraday_opening_range_minutes <= 60:
            raise ValueError("intraday opening range must be between 5 and 60")
        self.intraday_opening_range_minutes = intraday_opening_range_minutes
        self.trading_calendar = trading_calendar

    def _after_close_or_detail(self, now: datetime) -> str | None:
        """Return a skip detail unless XKRX has closed and final data can settle."""
        calendar = self.trading_calendar or KrxTradingCalendar()
        session = calendar.session_for(now)
        if session is None:
            return "market_closed"
        if not calendar.is_after_close_evaluation(now):
            return "market_not_closed"
        return None

    def refresh_prices(self, now: datetime) -> str:
        symbols = self.user_store.list_distinct_symbols()
        if not symbols:
            return "no_watchlist_symbols"
        updated = 0
        token = self.token_source()
        for start in range(0, len(symbols), 200):
            payload = self.toss_client.get_prices(symbols[start : start + 200], token).payload
            result = payload.get("result")
            if not isinstance(result, list):
                raise ValueError("Toss prices result must be an array")
            for item in result:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", ""))
                timestamp = datetime.fromisoformat(str(item["timestamp"]))
                self.market_cache.put(
                    CachedPayload(
                        "toss_invest",
                        "price",
                        symbol,
                        item,
                        timestamp,
                        now,
                    )
                )
                updated += 1
        return f"updated={updated}"

    def validate_ready_strategies(self, now: datetime) -> str:
        """Run monthly long-history validation; passing versions await owner approval."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("validation time must be timezone-aware")
        if self.paper_ledger is None:
            return "paper_ledger_unavailable"
        checked_on = now.astimezone(KOREA_TIMEZONE).date()
        calendar = self.trading_calendar or KrxTradingCalendar()
        ready = []
        intraday_ready = []
        for record in self.strategy_store.list_records():
            engine = record.definition.signal_engine
            if engine == "opening_range_breakout":
                if self.intraday_store is None or self.intraday_store.qualified_paper_days(
                    record.definition.strategy_id,
                    record.definition.version,
                    as_of=checked_on,
                    trading_calendar=calendar,
                ) < 30:
                    continue
                latest = self.strategy_store.latest_validation_on(
                    record.definition.strategy_id, record.definition.version
                )
                if latest is not None and checked_on < latest + timedelta(days=30):
                    continue
                symbol = next(
                    (
                        candidate
                        for candidate in self.user_store.list_distinct_symbols()
                        if self.intraday_store.complete_day_count(
                            candidate,
                            minimum_bars=self.intraday_minimum_bars,
                            as_of=checked_on,
                            trading_calendar=calendar,
                        )
                        >= 378
                    ),
                    None,
                )
                if symbol is not None:
                    intraday_ready.append((record, symbol))
                continue
            if engine not in {"time_series_momentum", "volatility_managed_exposure"}:
                continue
            if self.paper_ledger.completed_days(
                record.definition.strategy_id,
                record.definition.version,
                as_of=checked_on,
                trading_calendar=calendar,
            ) < 30:
                continue
            latest = self.strategy_store.latest_validation_on(
                record.definition.strategy_id, record.definition.version
            )
            if latest is not None and checked_on < latest + timedelta(days=30):
                continue
            ready.append(record)
        if not ready and not intraday_ready:
            return "no_ready_strategies"

        bars = []
        if ready:
            token = self.token_source()
            candles = self.toss_client.get_candle_history(
                self.volatility_market_symbol,
                token,
                interval="1d",
                max_count=2000,
                adjusted=True,
            )
            bars = daily_bars_from_toss_candles(candles)
        pipeline = StrategyValidationPipeline(
            self.strategy_store,
            self.paper_ledger,
            self.intraday_store,
            trading_calendar=calendar,
        )
        costs = toss_krx_2026_costs(project_outside_effective_period=True)
        eligible = 0
        for record in ready:
            definition = record.definition
            if definition.signal_engine == "time_series_momentum":
                outcome = pipeline.validate_momentum(
                    strategy_id=definition.strategy_id,
                    version=definition.version,
                    market="korean_equity_index",
                    bars=bars,
                    costs=costs,
                    checked_on=checked_on,
                    candidate_lookbacks=(
                        (120, 200, 252)
                        if definition.horizon == "long_term"
                        else (20, 60, 120, 200)
                    ),
                )
            else:
                outcome = pipeline.validate_volatility_managed(
                    strategy_id=definition.strategy_id,
                    version=definition.version,
                    market="korean_equity_index",
                    bars=bars,
                    costs=costs,
                    checked_on=checked_on,
                )
            eligible += int(outcome.decision.eligible)
        for record, symbol in intraday_ready:
            definition = record.definition
            outcome = pipeline.validate_opening_range_breakout(
                strategy_id=definition.strategy_id,
                version=definition.version,
                market=f"korean_equity:{symbol}",
                bars=self.intraday_store.list_complete_bars(
                    symbol, minimum_bars=self.intraday_minimum_bars
                ),
                costs=costs,
                checked_on=checked_on,
            )
            eligible += int(outcome.decision.eligible)
        evaluated = len(ready) + len(intraday_ready)
        return f"evaluated={evaluated},awaiting_owner={eligible}"

    def refresh_account_risk(self, now: datetime) -> str:
        if self.execution_state is None:
            raise RuntimeError("execution state is not configured")
        try:
            response = self.toss_client.get_holdings(self.token_source())
            daily_pnl_pct = parse_holdings_daily_pnl_pct(response)
        except Exception:
            self.execution_state.api_healthy = False
            raise
        self.execution_state.api_healthy = True
        self.execution_state.daily_pnl_pct = daily_pnl_pct
        if daily_pnl_pct <= -self.execution_state.max_daily_loss_pct:
            self.execution_state.activate_kill_switch(
                f"account_daily_loss_{daily_pnl_pct:g}pct"
            )
        return (
            f"daily_pnl_pct={daily_pnl_pct:g}, "
            f"kill_switch={str(self.execution_state.kill_switch_active).lower()}"
        )

    def value_paper_portfolios(self, now: datetime) -> str:
        if self.paper_ledger is None:
            raise RuntimeError("paper ledger is not configured")
        local = now.astimezone(KOREA_TIMEZONE)
        if detail := self._after_close_or_detail(now):
            return detail
        trading_date = local.date()
        portfolios = self.paper_ledger.list_portfolios()
        if not portfolios:
            return "no_paper_portfolios"
        symbols = sorted(
            {
                symbol
                for key in portfolios
                for symbol in self.paper_ledger.portfolio_symbols(
                    key, through=trading_date
                )
            }
        )
        prices: dict[str, float] = {}
        if symbols:
            response = self.toss_client.get_prices(symbols, self.token_source())
            for symbol in symbols:
                try:
                    quote = parse_price_quote(response, symbol)
                except (KeyError, TypeError, ValueError):
                    continue
                if quote.timestamp.astimezone(KOREA_TIMEZONE).date() == trading_date:
                    prices[symbol] = quote.price
        clean = 0
        incidents = 0
        for key in portfolios:
            valuation = self.paper_ledger.value_portfolio(
                key,
                trading_date=trading_date,
                initial_cash=self.paper_initial_cash,
                closing_prices=prices,
                api_healthy=True,
            )
            if valuation.costs_complete and valuation.critical_incidents == 0:
                clean += 1
            else:
                incidents += 1
        return f"valued={len(portfolios)}, clean={clean}, incidents={incidents}"

    def evaluate_paper_strategies(self, now: datetime) -> str:
        if self.paper_ledger is None or self.guide_input_factory is None:
            raise RuntimeError("paper strategy evaluator is not configured")
        local = now.astimezone(KOREA_TIMEZONE)
        if detail := self._after_close_or_detail(now):
            return detail
        evaluated = 0
        skipped = 0
        failures = 0
        remaining = self.paper_evaluation_max_symbols
        for user_id in self.user_store.list_users_with_guide_targets():
            intent = self.user_store.load_intent(user_id, as_of=now)
            if intent is None:
                continue
            for stock_code in self.user_store.list_symbols_for_user(user_id):
                if remaining <= 0:
                    skipped += 1
                    continue
                remaining -= 1
                try:
                    value = self.guide_input_factory(
                        user_id,
                        stock_code,
                        intent,
                        self.user_store.get_holding(user_id, stock_code),
                        now,
                    )
                    guide = generate_trade_guide(value, now=now)
                    inserted = self.paper_ledger.record_evaluation(
                        PaperStrategyEvaluation(
                            guide.strategy_id,
                            guide.strategy_version,
                            user_id,
                            stock_code,
                            guide.action,
                            guide.confidence,
                            guide.generated_at,
                            value.api_healthy,
                            0 if value.api_healthy else 1,
                        )
                    )
                except Exception:
                    failures += 1
                    continue
                if inserted:
                    evaluated += 1
                else:
                    skipped += 1
        detail = f"evaluated={evaluated}, skipped={skipped}, failures={failures}"
        if failures:
            raise RuntimeError(detail)
        return detail

    def evaluate_volatility_model(self, now: datetime) -> str:
        if self.paper_ledger is None:
            raise RuntimeError("paper ledger is not configured")
        definition = self.strategy_store.get_record(
            self.volatility_strategy_id, self.volatility_strategy_version
        ).definition
        if definition.signal_engine != "volatility_managed_exposure":
            raise RuntimeError("configured volatility strategy engine does not match")
        local = now.astimezone(KOREA_TIMEZONE)
        if detail := self._after_close_or_detail(now):
            return detail
        candles = self.toss_client.get_candles(
            self.volatility_market_symbol,
            self.token_source(),
            interval="1d",
            count=60,
        )
        signal = analyze_volatility_target(candles)
        trading_date = signal.observed_at.astimezone(KOREA_TIMEZONE).date()
        if trading_date != local.date():
            raise RuntimeError("volatility model received a stale market close")
        buy_rate = estimate_paper_transaction_cost(
            broker_provider="toss_invest",
            side="buy",
            notional=1.0,
            trading_date=trading_date,
        )
        sell_rate = estimate_paper_transaction_cost(
            broker_provider="toss_invest",
            side="sell",
            notional=1.0,
            trading_date=trading_date,
        )
        day = self.paper_ledger.record_model_observation(
            PaperModelObservation(
                self.volatility_strategy_id,
                self.volatility_strategy_version,
                self.volatility_market_symbol,
                trading_date,
                signal.close_price,
                signal.target_exposure,
                True,
            ),
            initial_equity=self.paper_initial_cash,
            buy_cost_rate=buy_rate,
            sell_cost_rate=sell_rate,
        )
        return (
            f"date={day.trading_date.isoformat()}, exposure={day.target_exposure:.4f}, "
            f"equity={day.equity:.2f}, qualified={str(day.costs_complete).lower()}"
        )

    def evaluate_long_term_model(self, now: datetime) -> str:
        if self.paper_ledger is None:
            raise RuntimeError("paper ledger is not configured")
        definition = self.strategy_store.get_record(
            self.long_term_strategy_id, self.long_term_strategy_version
        ).definition
        if (
            definition.signal_engine != "time_series_momentum"
            or definition.horizon != "long_term"
        ):
            raise RuntimeError("configured long-term strategy definition does not match")
        local = now.astimezone(KOREA_TIMEZONE)
        if detail := self._after_close_or_detail(now):
            return detail
        candles = self.toss_client.get_candles(
            self.volatility_market_symbol,
            self.token_source(),
            interval="1d",
            count=200,
        )
        result = candles.payload.get("result")
        items = result.get("candles") if isinstance(result, dict) else None
        if not isinstance(items, list) or not items:
            raise RuntimeError("long-term model candles are missing")
        dated = [
            datetime.fromisoformat(str(item["timestamp"]))
            for item in items
            if isinstance(item, dict) and item.get("timestamp")
        ]
        if not dated or any(value.tzinfo is None for value in dated):
            raise RuntimeError("long-term model candle timestamps are invalid")
        observed_at = max(dated)
        trading_date = observed_at.astimezone(KOREA_TIMEZONE).date()
        if trading_date != local.date():
            raise RuntimeError("long-term model received a stale market close")
        latest = max(
            (
                (datetime.fromisoformat(str(item["timestamp"])), float(item["closePrice"]))
                for item in items
                if isinstance(item, dict)
            ),
            key=lambda value: value[0],
        )
        momentum, _, _ = analyze_candles(candles, latest[1], lookback=200)
        target_exposure = 1.0 if momentum > 0 else 0.0
        buy_rate = estimate_paper_transaction_cost(
            broker_provider="toss_invest",
            side="buy",
            notional=1.0,
            trading_date=trading_date,
        )
        sell_rate = estimate_paper_transaction_cost(
            broker_provider="toss_invest",
            side="sell",
            notional=1.0,
            trading_date=trading_date,
        )
        day = self.paper_ledger.record_model_observation(
            PaperModelObservation(
                self.long_term_strategy_id,
                self.long_term_strategy_version,
                self.volatility_market_symbol,
                trading_date,
                latest[1],
                target_exposure,
                True,
            ),
            initial_equity=self.paper_initial_cash,
            buy_cost_rate=buy_rate,
            sell_cost_rate=sell_rate,
        )
        return (
            f"date={day.trading_date.isoformat()}, exposure={day.target_exposure:.4f}, "
            f"equity={day.equity:.2f}, qualified={str(day.costs_complete).lower()}"
        )

    def archive_intraday_bars(self, now: datetime) -> str:
        if self.intraday_store is None:
            return "intraday_store_unavailable"
        local = now.astimezone(KOREA_TIMEZONE)
        if detail := self._after_close_or_detail(now):
            return detail
        trading_date = local.date()
        symbols = tuple(
            dict.fromkeys(
                (
                    self.volatility_market_symbol,
                    *self.user_store.list_distinct_symbols()[
                        : self.intraday_archive_max_symbols
                    ],
                )
            )
        )
        token = self.token_source()
        archived = 0
        skipped = 0
        inserted_total = 0
        failures: list[str] = []
        paper_evaluated = 0
        for symbol in symbols:
            if self.intraday_store.has_complete_day(
                symbol,
                trading_date,
                minimum_bars=self.intraday_minimum_bars,
            ):
                skipped += 1
                paper_evaluated += int(
                    self._evaluate_intraday_paper_day(symbol, trading_date)
                )
                continue
            try:
                candles = self.toss_client.get_candle_history(
                    symbol,
                    token,
                    interval="1m",
                    max_count=500,
                    adjusted=True,
                    max_pages=5,
                )
                bars: list[IntradayBar] = []
                for item in candles:
                    timestamp = datetime.fromisoformat(str(item["timestamp"]))
                    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                        raise ValueError
                    timestamp_local = timestamp.astimezone(KOREA_TIMEZONE)
                    if timestamp_local.date() != trading_date:
                        continue
                    bars.append(
                        IntradayBar(
                            symbol=symbol,
                            timestamp=timestamp_local,
                            open_price=float(item["openPrice"]),
                            high_price=float(item["highPrice"]),
                            low_price=float(item["lowPrice"]),
                            close_price=float(item["closePrice"]),
                            volume=float(item["volume"]),
                        )
                    )
                inserted_total += self.intraday_store.record_bars(
                    bars, fetched_at=now
                )
                total = self.intraday_store.bar_count(symbol, trading_date)
                if total < self.intraday_minimum_bars:
                    failures.append(
                        f"{symbol}:{total}/{self.intraday_minimum_bars}"
                    )
                else:
                    archived += 1
                    paper_evaluated += int(
                        self._evaluate_intraday_paper_day(symbol, trading_date)
                    )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"invalid Toss intraday candle payload for {symbol}"
                ) from exc
        detail = (
            f"date={trading_date.isoformat()},archived={archived},"
            f"skipped={skipped},inserted={inserted_total},paper={paper_evaluated}"
        )
        if failures:
            raise RuntimeError(detail + ",incomplete=" + ";".join(failures))
        return detail

    def _evaluate_intraday_paper_day(
        self, symbol: str, trading_date: date
    ) -> bool:
        if self.intraday_store is None:
            return False
        existing = self.intraday_store.get_paper_day(
            self.intraday_strategy_id,
            self.intraday_strategy_version,
            symbol,
            trading_date,
        )
        if existing is not None:
            return False
        definition = self.strategy_store.get_record(
            self.intraday_strategy_id, self.intraday_strategy_version
        ).definition
        if definition.signal_engine != "opening_range_breakout":
            raise RuntimeError("configured intraday strategy engine does not match")
        bars = list(self.intraday_store.list_bars(symbol, trading_date))
        result = run_opening_range_breakout(
            bars,
            opening_range_minutes=self.intraday_opening_range_minutes,
            costs=toss_krx_2026_costs(),
        )
        cost_profile = toss_krx_2026_costs()
        self.intraday_store.record_paper_day(
            strategy_id=self.intraday_strategy_id,
            version=self.intraday_strategy_version,
            symbol=symbol,
            trading_date=trading_date,
            opening_range_minutes=self.intraday_opening_range_minutes,
            daily_return_pct=result.daily_returns[0] * 100,
            initial_equity=self.paper_initial_cash,
            api_healthy=True,
            costs_complete=all(
                value > 0
                for value in (
                    cost_profile.buy_commission_rate,
                    cost_profile.sell_commission_rate,
                    cost_profile.sell_tax_rate,
                    cost_profile.slippage_rate_per_side,
                )
            ),
        )
        return True

    def refresh_investor_flow(self, now: datetime) -> str:
        token = self.token_source()
        updated = 0
        for symbol in ("KOSPI", "KOSDAQ"):
            payload = self.toss_client.get_investor_trading(
                symbol, token, interval="1d", count=10
            ).payload
            result = payload.get("result")
            records = result.get("records") if isinstance(result, dict) else None
            observed = (
                datetime.fromisoformat(str(records[0]["updatedAt"]))
                if isinstance(records, list) and records
                else now
            )
            self.market_cache.put(
                CachedPayload(
                    "toss_invest",
                    "investor_flow",
                    symbol,
                    payload,
                    observed,
                    now,
                )
            )
            updated += 1
        return f"updated={updated}"

    def refresh_overseas(self, now: datetime) -> str:
        """Cache low-cost US benchmark momentum from the official Toss feed."""
        if not self.overseas_symbols:
            return "no_overseas_symbols"
        token = self.token_source()
        prices = self.toss_client.get_prices(list(self.overseas_symbols), token)
        updated = 0
        for symbol in self.overseas_symbols:
            quote = parse_price_quote(prices, symbol)
            candles = self.toss_client.get_candles(
                symbol, token, interval="1d", count=60
            )
            score, confidence, _ = analyze_candles(candles, quote.price)
            self.market_cache.put(
                CachedPayload(
                    "toss_invest",
                    "overseas",
                    symbol,
                    {
                        "metric": f"{symbol.lower()}_60d_momentum",
                        "normalized_score": score,
                        "signal_confidence": confidence,
                        "source_tier": 1,
                        "official": True,
                        "summary": (
                            f"{symbol} momentum normalized from adjusted daily candles"
                        ),
                        "reference_url": "https://developers.tossinvest.com/docs",
                    },
                    quote.timestamp,
                    now,
                )
            )
            updated += 1
        return f"updated={updated}"

    def refresh_disclosures(self, now: datetime) -> str:
        if self.open_dart is None:
            raise RuntimeError("OpenDART is not configured")
        events = self.open_dart.search_disclosures(now.date(), now.date())
        for event in events:
            self.market_cache.put(
                CachedPayload(
                    "open_dart",
                    "disclosure",
                    event.receipt_number,
                    {
                        "receipt_number": event.receipt_number,
                        "corporation_name": event.corporation_name,
                        "stock_code": event.stock_code,
                        "report_name": event.report_name,
                        "filer_name": event.filer_name,
                        "receipt_date": event.receipt_date.isoformat(),
                        "corrected": event.corrected,
                        "detail_url": event.detail_url,
                    },
                    now,
                    now,
                )
            )
        return f"updated={len(events)}"

    def refresh_news(self, now: datetime) -> str:
        if self.news_client is None:
            raise RuntimeError("NAVER API HUB news is not configured")
        symbols = self.user_store.list_distinct_symbols()[: self.news_max_symbols]
        if not symbols:
            return "no_watchlist_symbols"
        token = self.token_source()
        stock_payload = self.toss_client.get_stocks(symbols, token).payload
        stocks = stock_payload.get("result")
        if not isinstance(stocks, list):
            raise ValueError("Toss stocks result must be an array")
        names = {
            str(item.get("symbol")): str(item.get("name") or "")
            for item in stocks
            if isinstance(item, dict)
        }
        updated = 0
        for symbol in symbols:
            name = names.get(symbol, "").strip()
            if not name:
                continue
            articles = self.news_client.search(f'"{name}" 주식', display=10)
            signal = score_material_news(articles)
            if signal is None:
                continue
            self.market_cache.put(
                CachedPayload(
                    "naver_api_hub_news",
                    "news",
                    symbol,
                    {
                        "metric": "corroborated_material_news",
                        "normalized_score": signal.normalized_score,
                        "stock_codes": [symbol],
                        "source_tier": 3,
                        "official": False,
                        "summary": signal.summary,
                        "reference_url": signal.reference_urls[0],
                        "reference_urls": list(signal.reference_urls),
                    },
                    signal.observed_at,
                    now,
                )
            )
            updated += 1
        return f"updated={updated}"

    def refresh_macro(self, now: datetime) -> str:
        if self.ecos is None or not self.ecos_queries:
            raise RuntimeError("ECOS queries are not configured")
        updated = 0
        for statistic_code, cycle, item_code, polarity in self.ecos_queries:
            start_period, end_period = _ecos_period_range(now, cycle)
            points = self.ecos.search(
                statistic_code,
                cycle,
                start_period,
                end_period,
                item_code1=item_code,
            )
            ordered = sorted(points, key=lambda point: point.time)
            normalized_score = _macro_score(ordered, polarity)
            for point in ordered[-1:]:
                key = f"{statistic_code}:{point.item_code1 or ''}:{point.time}"
                self.market_cache.put(
                    CachedPayload(
                        "bok_ecos",
                        "macro",
                        key,
                        {
                            "statistic_code": point.statistic_code,
                            "cycle": point.cycle,
                            "time": point.time,
                            "value": point.value,
                            "unit_name": point.unit_name,
                            "normalized_score": normalized_score,
                            "metric": f"ecos_{statistic_code}_{item_code or 'all'}_trend",
                            "source_tier": 1,
                            "official": True,
                            "summary": (
                                f"ECOS {statistic_code} {point.time}; polarity={polarity}"
                            ),
                            "reference_url": "https://ecos.bok.or.kr/api/",
                        },
                        now,
                        now,
                    )
                )
                updated += 1
        return f"updated={updated}"

    def check_strategy_health(self, now: datetime) -> str:
        suspended = 0
        cutoff = now.date() - timedelta(days=self.strategy_health_days)
        for record in self.strategy_store.list_records():
            if record.status != "approved":
                continue
            checked_on = self.strategy_store.latest_validation_on(
                record.definition.strategy_id, record.definition.version
            )
            if checked_on is None or checked_on < cutoff:
                self.strategy_store.suspend(
                    record.definition.strategy_id, record.definition.version
                )
                suspended += 1
        return f"suspended={suspended}"

    def enqueue_strategy_research(self, now: datetime) -> str:
        due = self.strategy_store.due_for_research(now.date())
        if self.source_monitor is None:
            inserted = self.research_queue.enqueue_due(due, now=now)
            return f"queued={inserted}"
        scan = self.source_monitor.scan(due, now=now)
        inserted = self.research_queue.enqueue_due(list(scan.changed), now=now)
        suspended = 0
        for record in scan.changed:
            if record.last_researched_on is None or record.status != "approved":
                continue
            self.strategy_store.suspend(
                record.definition.strategy_id,
                record.definition.version,
                actor_user_id=None,
                occurred_at=now,
            )
            suspended += 1
        for record in scan.successful:
            self.strategy_store.mark_researched(
                record.definition.strategy_id,
                record.definition.version,
                checked_on=now.date(),
            )
        if scan.failures:
            raise RuntimeError("; ".join(scan.failures))
        return (
            f"queued={inserted}, unchanged={len(scan.unchanged)}, "
            f"suspended={suspended}"
        )

    def discover_academic_strategies(self, now: datetime) -> str:
        if self.academic_discovery is None:
            raise RuntimeError("academic discovery is not configured")
        candidates = self.academic_discovery.discover(
            since=now.date() - timedelta(days=120),
            rows=self.academic_discovery_rows,
        )
        inserted = self.research_queue.enqueue_academic_candidates(
            candidates, now=now
        )
        return f"discovered={len(candidates)}, queued={inserted}"


def _ecos_period(now: datetime, cycle: str) -> str:
    if cycle == "D":
        return now.strftime("%Y%m%d")
    if cycle == "M":
        return now.strftime("%Y%m")
    if cycle == "Q":
        quarter = (now.month - 1) // 3 + 1
        return f"{now.year}Q{quarter}"
    if cycle == "A":
        return str(now.year)
    raise ValueError(f"unsupported ECOS cycle: {cycle}")


def _ecos_period_range(now: datetime, cycle: str) -> tuple[str, str]:
    end = _ecos_period(now, cycle)
    if cycle == "D":
        return (now - timedelta(days=30)).strftime("%Y%m%d"), end
    if cycle == "M":
        total_months = now.year * 12 + now.month - 1 - 12
        return f"{total_months // 12:04d}{total_months % 12 + 1:02d}", end
    if cycle == "Q":
        current = now.year * 4 + (now.month - 1) // 3
        prior = current - 8
        return f"{prior // 4}Q{prior % 4 + 1}", end
    if cycle == "A":
        return str(now.year - 5), end
    raise ValueError(f"unsupported ECOS cycle: {cycle}")


def _macro_score(points, polarity: float | None) -> float | None:
    """Score only series whose economic direction was explicitly configured."""
    if polarity not in {-1.0, 1.0} or len(points) < 2:
        return None
    first = float(points[0].value)
    last = float(points[-1].value)
    scale = max(abs(first), 1e-9)
    return round(math.tanh(((last - first) / scale) / 0.1) * polarity, 6)
