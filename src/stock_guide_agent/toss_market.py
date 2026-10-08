from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any, Callable

from .backtest import DailyBar
from .execution import ExecutionState
from .guidance import GuideInput, Position
from .intent import InvestmentIntent
from .market import MAX_AGE_SECONDS, MarketObservation, build_market_snapshot
from .state import Holding
from .toss import TossInvestClient, TossInvestResponse, TossInvestToken


class TossPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class TossPriceQuote:
    symbol: str
    price: float
    timestamp: datetime
    currency: str


@dataclass(frozen=True)
class TechnicalLevels:
    support_price: float
    resistance_price: float
    entry_reference_price: float
    take_profit_reference_price: float


@dataclass(frozen=True)
class VolatilityTarget:
    close_price: float
    observed_at: datetime
    annualized_volatility: float
    target_exposure: float
    confidence: float


@dataclass(frozen=True)
class InvestorFlowBreakdown:
    foreign_net_amount: float
    institution_net_amount: float
    individual_net_amount: float
    other_corporation_net_amount: float
    directional_score: float
    updated_at: datetime

    def summary(self) -> str:
        return (
            f"외국인 {_signed_krw(self.foreign_net_amount)}, "
            f"기관 {_signed_krw(self.institution_net_amount)}, "
            f"개인 {_signed_krw(self.individual_net_amount)} 순매수"
        )


def daily_bars_from_toss_candles(
    candles: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[DailyBar]:
    """Convert adjusted Toss daily candles to unique chronological backtest bars."""
    by_date: dict[date, DailyBar] = {}
    for candle in candles:
        timestamp = candle.get("timestamp")
        close = candle.get("closePrice")
        if timestamp is None or close is None:
            raise ValueError("Toss candle requires timestamp and closePrice")
        trading_date = datetime.fromisoformat(str(timestamp)).date()
        bar = DailyBar(trading_date, float(close))
        existing = by_date.get(trading_date)
        if existing is not None and existing.adjusted_close != bar.adjusted_close:
            raise ValueError("conflicting Toss candles for one trading date")
        by_date[trading_date] = bar
    return [by_date[item] for item in sorted(by_date)]


class TossGuideInputFactory:
    """Build safe guide inputs from official Toss price, candle and KRX flow data."""

    def __init__(
        self,
        client: TossInvestClient,
        token: TossInvestToken | Callable[[], TossInvestToken],
        execution_state: ExecutionState,
        *,
        strategy_id: str,
        strategy_version: str = "1.0.0",
        strategy_approved: bool | Callable[[], bool],
        market_symbol: str = "KOSPI",
        supplemental_observations: Callable[[str], list[MarketObservation]] | None = None,
        alternative_symbols: Callable[[int, str], list[str]] | None = None,
        max_alternative_symbols: int = 5,
        long_term_strategy_id: str | None = None,
        long_term_strategy_version: str = "1.0.0",
        long_term_strategy_approved: bool | Callable[[], bool] = False,
    ) -> None:
        if market_symbol not in {"KOSPI", "KOSDAQ"}:
            raise ValueError("market_symbol must be KOSPI or KOSDAQ")
        self.client = client
        self._token = token
        self.execution_state = execution_state
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self._strategy_approved = strategy_approved
        self.long_term_strategy_id = long_term_strategy_id
        self.long_term_strategy_version = long_term_strategy_version
        self._long_term_strategy_approved = long_term_strategy_approved
        self.market_symbol = market_symbol
        self.supplemental_observations = supplemental_observations
        self.alternative_symbols = alternative_symbols
        if not 0 <= max_alternative_symbols <= 20:
            raise ValueError("max_alternative_symbols must be between 0 and 20")
        self.max_alternative_symbols = max_alternative_symbols

    def __call__(
        self,
        user_id: int,
        stock_code: str,
        intent: InvestmentIntent,
        holding: Holding | None,
        now: datetime,
    ) -> GuideInput:
        long_term_weight = sum(
            allocation.weight_pct
            for allocation in intent.allocations
            if allocation.strategy in {"long_term", "long_term_stable"}
        )
        other_active_weight = sum(
            allocation.weight_pct
            for allocation in intent.allocations
            if allocation.strategy in {"swing", "day_trading"}
        )
        use_long_term = (
            self.long_term_strategy_id is not None
            and long_term_weight > 0
            and long_term_weight >= other_active_weight
        )
        selected_strategy_id = (
            self.long_term_strategy_id if use_long_term else self.strategy_id
        )
        selected_strategy_version = (
            self.long_term_strategy_version if use_long_term else self.strategy_version
        )
        selected_strategy_approved = (
            self._long_term_strategy_approved
            if use_long_term
            else self._strategy_approved
        )
        lookback = 200 if use_long_term else 60
        token = self._token() if callable(self._token) else self._token
        quote = parse_price_quote(
            self.client.get_prices([stock_code], token), stock_code
        )
        candle_payload = self.client.get_candles(
            stock_code, token, interval="1d", count=lookback
        )
        flow_payload = self.client.get_investor_trading(
            self.market_symbol, token, interval="1d", count=10
        )
        momentum, signal_confidence, invalidation_price = analyze_candles(
            candle_payload, quote.price, lookback=lookback
        )
        levels = analyze_technical_levels(candle_payload)
        flow = analyze_investor_flow_breakdown(flow_payload)
        observations = [
            MarketObservation(
                source_id="toss_invest_prices",
                category="price",
                metric=f"current_vs_{lookback}d_momentum",
                value=momentum,
                observed_at=quote.timestamp,
                fetched_at=now,
                source_tier=1,
                official=True,
                summary=(
                    f"{stock_code} {lookback}거래일 수정주가 절대 모멘텀"
                ),
                reference_url="https://developers.tossinvest.com/docs",
            ),
            MarketObservation(
                source_id="toss_invest_investor_trading",
                category="investor_flow",
                metric=f"{self.market_symbol.lower()}_foreign_institution_net_flow",
                value=flow.directional_score,
                observed_at=flow.updated_at,
                fetched_at=now,
                source_tier=1,
                official=True,
                summary=flow.summary(),
                reference_url="https://developers.tossinvest.com/docs",
            ),
        ]
        if self.supplemental_observations is not None:
            observations.extend(self.supplemental_observations(stock_code))
        market = build_market_snapshot(observations, as_of=now)
        alternative_code, alternative_score, alternative_confidence = self._best_alternative(
            user_id, stock_code, token, now
        )
        position = (
            Position(
                stock_code=stock_code,
                quantity=holding.quantity,
                average_price=holding.average_price,
                current_price=quote.price,
            )
            if holding is not None
            else None
        )
        return GuideInput(
            stock_code=stock_code,
            market=market,
            strategy_id=selected_strategy_id,
            strategy_approved=(
                selected_strategy_approved()
                if callable(selected_strategy_approved)
                else selected_strategy_approved
            ),
            signal_score=momentum,
            signal_confidence=signal_confidence,
            max_trade_loss_pct=intent.max_trade_loss_pct or 0.5,
            max_daily_loss_pct=intent.max_daily_loss_pct or 1.5,
            daily_pnl_pct=self.execution_state.daily_pnl_pct,
            position=position,
            invalidation_price=invalidation_price,
            alternative_score=alternative_score,
            alternative_stock_code=alternative_code,
            alternative_confidence=alternative_confidence,
            current_price=quote.price,
            entry_reference_price=levels.entry_reference_price,
            take_profit_reference_price=levels.take_profit_reference_price,
            api_healthy=self.execution_state.api_healthy,
            kill_switch_active=self.execution_state.kill_switch_active,
            strategy_version=selected_strategy_version,
        )

    def _best_alternative(
        self,
        user_id: int,
        current_stock_code: str,
        token: TossInvestToken,
        now: datetime,
    ) -> tuple[str | None, float | None, float | None]:
        if self.alternative_symbols is None or self.max_alternative_symbols == 0:
            return None, None, None
        candidates = [
            symbol
            for symbol in self.alternative_symbols(user_id, current_stock_code)
            if symbol != current_stock_code
        ][: self.max_alternative_symbols]
        if not candidates:
            return None, None, None
        quotes = self.client.get_prices(candidates, token)
        scored: list[tuple[float, float, str]] = []
        for symbol in candidates:
            quote = parse_price_quote(quotes, symbol)
            age_seconds = (now - quote.timestamp).total_seconds()
            if age_seconds < 0 or age_seconds > MAX_AGE_SECONDS["price"]:
                continue
            candles = self.client.get_candles(
                symbol, token, interval="1d", count=60
            )
            score, confidence, _ = analyze_candles(candles, quote.price)
            confidence *= max(
                0.0, 1.0 - age_seconds / MAX_AGE_SECONDS["price"]
            )
            if confidence >= 0.5:
                scored.append((score, confidence, symbol))
        if not scored:
            return None, None, None
        score, confidence, symbol = max(
            scored, key=lambda item: (item[0], item[1], item[2])
        )
        return symbol, score, confidence


def parse_price_quote(
    response: TossInvestResponse, symbol: str
) -> TossPriceQuote:
    result = response.payload.get("result")
    if not isinstance(result, list):
        raise TossPayloadError("price result must be an array")
    item = next(
        (value for value in result if isinstance(value, dict) and value.get("symbol") == symbol),
        None,
    )
    if item is None:
        raise TossPayloadError(f"price for {symbol} is missing")
    try:
        price = float(item["lastPrice"])
        timestamp = datetime.fromisoformat(str(item["timestamp"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("invalid price payload") from exc
    if price <= 0 or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise TossPayloadError("price and timestamp must be valid")
    return TossPriceQuote(symbol, price, timestamp, str(item.get("currency", "")))


def analyze_candles(
    response: TossInvestResponse,
    current_price: float,
    *,
    lookback: int = 60,
) -> tuple[float, float, float | None]:
    if not 20 <= lookback <= 252:
        raise ValueError("momentum lookback must be between 20 and 252")
    result = response.payload.get("result")
    candles = result.get("candles") if isinstance(result, dict) else None
    if not isinstance(candles, list) or len(candles) < min(20, lookback):
        return 0.0, 0.0, None
    closes: list[float] = []
    lows: list[float] = []
    try:
        for candle in candles[:lookback]:
            if not isinstance(candle, dict):
                raise TypeError
            closes.append(float(candle["closePrice"]))
            lows.append(float(candle["lowPrice"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("invalid candle payload") from exc
    if min(closes) <= 0 or min(lows) <= 0:
        raise TossPayloadError("candle prices must be positive")
    trailing_price = closes[-1]
    raw_return = current_price / trailing_price - 1.0
    score = max(-1.0, min(1.0, math.tanh(raw_return / 0.15)))
    confidence = min(1.0, len(closes) / lookback)
    invalidation = min(lows[:20])
    return round(score, 6), round(confidence, 6), invalidation


def analyze_technical_levels(response: TossInvestResponse) -> TechnicalLevels:
    """Derive descriptive 20-session reference levels, not price predictions."""
    result = response.payload.get("result")
    candles = result.get("candles") if isinstance(result, dict) else None
    if not isinstance(candles, list) or len(candles) < 20:
        raise TossPayloadError("at least 20 candles are required for technical levels")
    lows: list[float] = []
    highs: list[float] = []
    try:
        for candle in candles[:20]:
            if not isinstance(candle, dict):
                raise TypeError
            lows.append(float(candle["lowPrice"]))
            highs.append(float(candle["highPrice"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("invalid candle payload for technical levels") from exc
    support = min(lows)
    resistance = max(highs)
    if support <= 0 or resistance < support:
        raise TossPayloadError("technical level prices must be valid")
    price_range = resistance - support
    entry = support + price_range * 0.25
    take_profit = resistance - price_range * 0.10
    return TechnicalLevels(
        round(support, 6),
        round(resistance, 6),
        round(entry, 6),
        round(take_profit, 6),
    )


def analyze_volatility_target(
    response: TossInvestResponse,
    *,
    lookback: int = 20,
    annual_target_volatility: float = 0.15,
) -> VolatilityTarget:
    if not 5 <= lookback <= 252:
        raise ValueError("volatility lookback must be between 5 and 252")
    if not 0 < annual_target_volatility <= 1:
        raise ValueError("annual target volatility must be in (0, 1]")
    result = response.payload.get("result")
    candles = result.get("candles") if isinstance(result, dict) else None
    if not isinstance(candles, list) or len(candles) < lookback + 1:
        raise TossPayloadError("insufficient candles for volatility target")
    parsed: list[tuple[datetime, float]] = []
    try:
        for candle in candles:
            if not isinstance(candle, dict):
                raise TypeError
            timestamp = datetime.fromisoformat(str(candle["timestamp"]))
            close = float(candle["closePrice"])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None or close <= 0:
                raise ValueError
            parsed.append((timestamp, close))
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("invalid candles for volatility target") from exc
    parsed.sort(key=lambda item: item[0])
    sample = parsed[-(lookback + 1) :]
    closes = [item[1] for item in sample]
    returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
    ]
    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / len(returns)
    annualized = math.sqrt(variance) * math.sqrt(252)
    exposure = min(1.0, annual_target_volatility / max(annualized, 1e-8))
    return VolatilityTarget(
        close_price=closes[-1],
        observed_at=sample[-1][0],
        annualized_volatility=round(annualized, 8),
        target_exposure=round(exposure, 8),
        confidence=round(min(1.0, len(parsed) / 60.0), 6),
    )


def analyze_investor_flow(
    response: TossInvestResponse,
) -> tuple[float, datetime]:
    breakdown = analyze_investor_flow_breakdown(response)
    return breakdown.directional_score, breakdown.updated_at


def analyze_investor_flow_breakdown(
    response: TossInvestResponse,
) -> InvestorFlowBreakdown:
    result = response.payload.get("result")
    records = result.get("records") if isinstance(result, dict) else None
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise TossPayloadError("investor trading records are missing")
    record: dict[str, Any] = records[0]
    try:
        foreigner = record["foreigner"]
        institution = record["institution"]
        individual = record["individual"]
        other = record["otherCorporation"]
        foreign_net = float(foreigner["buyAmount"]) - float(foreigner["sellAmount"])
        institution_net = float(institution["buyAmount"]) - float(
            institution["sellAmount"]
        )
        individual_net = float(individual["buyAmount"]) - float(individual["sellAmount"])
        other_net = float(other["buyAmount"]) - float(other["sellAmount"])
        net = foreign_net + institution_net
        total_buy = sum(
            float(item["buyAmount"])
            for item in (foreigner, institution, individual, other)
        )
        updated_at = datetime.fromisoformat(str(record["updatedAt"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("invalid investor trading payload") from exc
    if total_buy <= 0 or updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise TossPayloadError("investor trading values must be valid")
    score = math.tanh(net / (total_buy * 0.02))
    return InvestorFlowBreakdown(
        foreign_net_amount=foreign_net,
        institution_net_amount=institution_net,
        individual_net_amount=individual_net,
        other_corporation_net_amount=other_net,
        directional_score=round(max(-1.0, min(1.0, score)), 6),
        updated_at=updated_at,
    )


def _signed_krw(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{sign}{absolute / 100_000_000:,.1f}억원"
    return f"{sign}{absolute:,.0f}원"


def parse_holdings_daily_pnl_pct(response: TossInvestResponse) -> float:
    """Parse the official decimal daily P/L rate into percentage points."""
    result = response.payload.get("result")
    daily = result.get("dailyProfitLoss") if isinstance(result, dict) else None
    try:
        rate = float(daily["rate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TossPayloadError("holdings daily profit/loss rate is missing") from exc
    if not math.isfinite(rate) or rate < -1:
        raise TossPayloadError("holdings daily profit/loss rate is invalid")
    return round(rate * 100.0, 6)
