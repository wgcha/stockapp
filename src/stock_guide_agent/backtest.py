from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import math
from numbers import Real
from statistics import mean, pstdev

from .intraday import IntradayBar
from .strategies import SignalEngine, ValidationReport


@dataclass(frozen=True)
class DailyBar:
    trading_date: date
    adjusted_close: float

    def __post_init__(self) -> None:
        if not _is_finite_real(self.adjusted_close) or self.adjusted_close <= 0:
            raise ValueError("adjusted_close must be a finite positive number")


@dataclass(frozen=True)
class TradingCostProfile:
    provider: str
    venue: str
    buy_commission_rate: float
    sell_commission_rate: float
    sell_tax_rate: float
    slippage_rate_per_side: float
    effective_from: date
    effective_to: date | None
    source_urls: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    allow_outside_effective_period: bool = False

    def __post_init__(self) -> None:
        rates = (
            self.buy_commission_rate,
            self.sell_commission_rate,
            self.sell_tax_rate,
            self.slippage_rate_per_side,
        )
        if any(
            not _is_finite_real(rate) or rate < 0 or rate >= 0.1
            for rate in rates
        ):
            raise ValueError("cost rates must be finite numbers in [0, 0.1)")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")
        if not self.source_urls:
            raise ValueError("at least one cost source is required")

    def validate_period(self, start: date, end: date) -> None:
        if self.allow_outside_effective_period:
            return
        if start < self.effective_from:
            raise ValueError("backtest begins before cost profile effective date")
        if self.effective_to is not None and end > self.effective_to:
            raise ValueError("backtest ends after cost profile expiry")


def toss_krx_2026_costs(
    *,
    slippage_bps: float = 5.0,
    project_outside_effective_period: bool = False,
) -> TradingCostProfile:
    """2026 Toss KRX disclosed commission and Korean sell-side tax profile."""
    return TradingCostProfile(
        provider="toss_invest",
        venue="KRX",
        buy_commission_rate=0.00015,
        sell_commission_rate=0.00015,
        sell_tax_rate=0.0020,
        slippage_rate_per_side=slippage_bps / 10_000,
        effective_from=date(2026, 1, 1),
        effective_to=date(2027, 5, 13),
        source_urls=(
            "https://p.tossinvest.com/ko/open-api",
            "https://securities.miraeasset.com/bbs/board/message/view.do?categoryId=66&messageId=2336448&selectedId=1000000202&selectedId=1000000203&selectedId=1000003101",
        ),
        assumptions=(
            f"slippage_{slippage_bps:g}_bps_each_side",
            *(
                ("apply_2026_costs_outside_disclosed_effective_period",)
                if project_outside_effective_period
                else ()
            ),
        ),
        allow_outside_effective_period=project_outside_effective_period,
    )


def mirae_krx_2026_costs(
    *,
    commission_rate: float,
    slippage_bps: float = 5.0,
    project_outside_effective_period: bool = False,
) -> TradingCostProfile:
    """Mirae profile requires the user's contracted commission rate."""
    return TradingCostProfile(
        provider="mirae_asset",
        venue="KRX",
        buy_commission_rate=commission_rate,
        sell_commission_rate=commission_rate,
        sell_tax_rate=0.0020,
        slippage_rate_per_side=slippage_bps / 10_000,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source_urls=(
            "https://securities.miraeasset.com/bbs/board/message/view.do?categoryId=66&messageId=2336448&selectedId=1000000202&selectedId=1000000203&selectedId=1000003101",
        ),
        assumptions=(
            "commission_rate_supplied_from_user_contract",
            f"slippage_{slippage_bps:g}_bps_each_side",
            *(
                ("apply_2026_costs_outside_effective_period",)
                if project_outside_effective_period
                else ()
            ),
        ),
        allow_outside_effective_period=project_outside_effective_period,
    )


@dataclass(frozen=True)
class BacktestResult:
    daily_returns: tuple[float, ...]
    equity_curve: tuple[float, ...]
    cost_adjusted_sharpe: float
    max_drawdown: float
    trades: int
    total_cost_rate: float
    exposures: tuple[float, ...] = ()


@dataclass(frozen=True)
class WalkForwardResult:
    windows: int
    selected_lookbacks: tuple[int, ...]
    oos_daily_returns: tuple[float, ...]
    cost_adjusted_sharpe: float
    max_drawdown: float
    trades: int
    sample_years: float
    parameter_stability: float
    includes_fees_taxes_slippage: bool
    data_leakage_check_passed: bool = True
    selected_target_volatilities: tuple[float, ...] = ()
    cost_source_urls: tuple[str, ...] = ()
    cost_assumptions: tuple[str, ...] = ()
    selected_opening_range_minutes: tuple[int, ...] = ()

    def to_validation_report(
        self,
        *,
        strategy_id: str,
        version: str,
        market: str,
        paper_trading_days: int,
        signal_engine: SignalEngine,
    ) -> ValidationReport:
        return ValidationReport(
            strategy_id=strategy_id,
            version=version,
            market=market,
            sample_years=self.sample_years,
            out_of_sample=True,
            walk_forward_windows=self.windows,
            trades=self.trades,
            cost_adjusted_sharpe=self.cost_adjusted_sharpe,
            max_drawdown=self.max_drawdown,
            parameter_stability=self.parameter_stability,
            paper_trading_days=paper_trading_days,
            includes_fees_taxes_slippage=self.includes_fees_taxes_slippage,
            data_leakage_check_passed=self.data_leakage_check_passed,
            signal_engine=signal_engine,
            cost_source_urls=self.cost_source_urls,
            cost_assumptions=self.cost_assumptions,
        )


def run_long_cash_momentum(
    bars: list[DailyBar], *, lookback: int, costs: TradingCostProfile
) -> BacktestResult:
    _validate_bars(bars, lookback)
    costs.validate_period(bars[0].trading_date, bars[-1].trading_date)
    returns: list[float] = []
    equity = 1.0
    curve = [equity]
    previous_position = 0
    trades = 0
    total_cost = 0.0
    closes = [bar.adjusted_close for bar in bars]
    for index in range(lookback + 1, len(bars)):
        # Signal uses data only through index-1; it controls index-1 -> index exposure.
        target_position = int(closes[index - 1] > closes[index - 1 - lookback])
        turnover = abs(target_position - previous_position)
        cost = 0.0
        if turnover:
            commission = (
                costs.buy_commission_rate
                if target_position > previous_position
                else costs.sell_commission_rate + costs.sell_tax_rate
            )
            cost = turnover * (commission + costs.slippage_rate_per_side)
            trades += 1
        gross_return = target_position * (closes[index] / closes[index - 1] - 1.0)
        net_return = gross_return - cost
        if not math.isfinite(net_return):
            raise ValueError("backtest results must be finite")
        if net_return <= -1:
            raise ValueError("strategy equity became non-positive")
        equity *= 1.0 + net_return
        if not math.isfinite(equity):
            raise ValueError("backtest results must be finite")
        returns.append(net_return)
        curve.append(equity)
        total_cost += cost
        previous_position = target_position
    sharpe, drawdown = _metrics(returns)
    return BacktestResult(
        tuple(returns),
        tuple(curve),
        sharpe,
        drawdown,
        trades,
        round(total_cost, 8),
    )


def walk_forward_momentum(
    bars: list[DailyBar],
    *,
    candidate_lookbacks: tuple[int, ...],
    costs: TradingCostProfile,
    train_days: int = 756,
    test_days: int = 252,
) -> WalkForwardResult:
    if not candidate_lookbacks or any(value < 2 for value in candidate_lookbacks):
        raise ValueError("candidate lookbacks must contain values of at least 2")
    if train_days < max(candidate_lookbacks) + 2 or test_days < 20:
        raise ValueError("walk-forward windows are too short")
    _validate_bars(bars, max(candidate_lookbacks))
    costs.validate_period(bars[0].trading_date, bars[-1].trading_date)

    selected: list[int] = []
    oos_returns: list[float] = []
    trades = 0
    test_start = train_days
    while test_start + test_days <= len(bars):
        train_start = max(0, test_start - train_days)
        train = bars[train_start:test_start]
        candidates = [
            (run_long_cash_momentum(train, lookback=value, costs=costs), value)
            for value in candidate_lookbacks
        ]
        _, chosen = max(
            candidates,
            key=lambda item: (item[0].cost_adjusted_sharpe, -item[1]),
        )
        context_start = test_start - chosen - 1
        test = bars[context_start : test_start + test_days]
        result = run_long_cash_momentum(test, lookback=chosen, costs=costs)
        selected.append(chosen)
        oos_returns.extend(result.daily_returns)
        trades += result.trades
        test_start += test_days

    if not selected:
        raise ValueError("not enough bars for one walk-forward window")
    sharpe, drawdown = _metrics(oos_returns)
    modal_count = Counter(selected).most_common(1)[0][1]
    years = (bars[-1].trading_date - bars[0].trading_date).days / 365.25
    has_costs = (
        costs.buy_commission_rate > 0
        and costs.sell_commission_rate > 0
        and costs.sell_tax_rate > 0
        and costs.slippage_rate_per_side > 0
    )
    return WalkForwardResult(
        windows=len(selected),
        selected_lookbacks=tuple(selected),
        oos_daily_returns=tuple(oos_returns),
        cost_adjusted_sharpe=sharpe,
        max_drawdown=drawdown,
        trades=trades,
        sample_years=round(years, 4),
        parameter_stability=round(modal_count / len(selected), 4),
        includes_fees_taxes_slippage=has_costs,
        cost_source_urls=costs.source_urls,
        cost_assumptions=costs.assumptions,
    )


def run_volatility_managed_exposure(
    bars: list[DailyBar],
    *,
    volatility_lookback: int,
    annual_target_volatility: float,
    rebalance_threshold: float,
    costs: TradingCostProfile,
) -> BacktestResult:
    """Long/cash risk overlay using only volatility known at the prior close."""
    if not 5 <= volatility_lookback <= 252:
        raise ValueError("volatility lookback must be between 5 and 252")
    if not 0 < annual_target_volatility <= 1:
        raise ValueError("annual target volatility must be in (0, 1]")
    if not 0 <= rebalance_threshold <= 0.5:
        raise ValueError("rebalance threshold must be in [0, 0.5]")
    _validate_bars(bars, volatility_lookback)
    costs.validate_period(bars[0].trading_date, bars[-1].trading_date)
    closes = [bar.adjusted_close for bar in bars]
    strategy_returns: list[float] = []
    equity = 1.0
    curve = [equity]
    previous_exposure = 0.0
    exposures: list[float] = []
    trades = 0
    total_cost = 0.0
    for index in range(volatility_lookback + 1, len(bars)):
        # Returns ending at index-1 are available before index-1 -> index exposure.
        history = [
            closes[offset] / closes[offset - 1] - 1.0
            for offset in range(index - volatility_lookback, index)
        ]
        if not all(math.isfinite(value) for value in history):
            raise ValueError("backtest results must be finite")
        try:
            realized = pstdev(history) * math.sqrt(252)
        except (OverflowError, ValueError):
            raise ValueError("backtest results must be finite") from None
        if not math.isfinite(realized):
            raise ValueError("backtest results must be finite")
        target_exposure = min(1.0, annual_target_volatility / max(realized, 1e-8))
        if abs(target_exposure - previous_exposure) < rebalance_threshold:
            target_exposure = previous_exposure
        turnover = abs(target_exposure - previous_exposure)
        cost = 0.0
        if turnover > 0:
            increasing = target_exposure > previous_exposure
            rate = (
                costs.buy_commission_rate
                if increasing
                else costs.sell_commission_rate + costs.sell_tax_rate
            )
            cost = turnover * (rate + costs.slippage_rate_per_side)
            trades += 1
        gross = target_exposure * (closes[index] / closes[index - 1] - 1.0)
        net = gross - cost
        if not math.isfinite(net):
            raise ValueError("backtest results must be finite")
        if net <= -1:
            raise ValueError("strategy equity became non-positive")
        equity *= 1.0 + net
        if not math.isfinite(equity):
            raise ValueError("backtest results must be finite")
        strategy_returns.append(net)
        curve.append(equity)
        total_cost += cost
        previous_exposure = target_exposure
        exposures.append(target_exposure)
    sharpe, drawdown = _metrics(strategy_returns)
    return BacktestResult(
        tuple(strategy_returns),
        tuple(curve),
        sharpe,
        drawdown,
        trades,
        round(total_cost, 8),
        tuple(exposures),
    )


def walk_forward_volatility_managed(
    bars: list[DailyBar],
    *,
    candidate_lookbacks: tuple[int, ...],
    candidate_target_volatilities: tuple[float, ...],
    rebalance_threshold: float,
    costs: TradingCostProfile,
    train_days: int = 756,
    test_days: int = 252,
) -> WalkForwardResult:
    if not candidate_lookbacks or any(not 5 <= value <= 252 for value in candidate_lookbacks):
        raise ValueError("candidate volatility lookbacks must be between 5 and 252")
    if not candidate_target_volatilities or any(
        not 0 < value <= 1 for value in candidate_target_volatilities
    ):
        raise ValueError("candidate target volatilities must be in (0, 1]")
    if train_days < max(candidate_lookbacks) + 2 or test_days < 20:
        raise ValueError("walk-forward windows are too short")
    _validate_bars(bars, max(candidate_lookbacks))
    costs.validate_period(bars[0].trading_date, bars[-1].trading_date)

    selected: list[tuple[int, float]] = []
    oos_returns: list[float] = []
    trades = 0
    test_start = train_days
    while test_start + test_days <= len(bars):
        train = bars[max(0, test_start - train_days) : test_start]
        candidates = [
            (
                run_volatility_managed_exposure(
                    train,
                    volatility_lookback=lookback,
                    annual_target_volatility=target,
                    rebalance_threshold=rebalance_threshold,
                    costs=costs,
                ),
                (lookback, target),
            )
            for lookback in candidate_lookbacks
            for target in candidate_target_volatilities
        ]
        _, chosen = max(
            candidates,
            key=lambda item: (
                item[0].cost_adjusted_sharpe,
                -item[1][0],
                -item[1][1],
            ),
        )
        lookback, target = chosen
        context_start = test_start - lookback - 1
        test = bars[context_start : test_start + test_days]
        result = run_volatility_managed_exposure(
            test,
            volatility_lookback=lookback,
            annual_target_volatility=target,
            rebalance_threshold=rebalance_threshold,
            costs=costs,
        )
        selected.append(chosen)
        oos_returns.extend(result.daily_returns)
        trades += result.trades
        test_start += test_days

    if not selected:
        raise ValueError("not enough bars for one walk-forward window")
    sharpe, drawdown = _metrics(oos_returns)
    modal_count = Counter(selected).most_common(1)[0][1]
    years = (bars[-1].trading_date - bars[0].trading_date).days / 365.25
    has_costs = all(
        value > 0
        for value in (
            costs.buy_commission_rate,
            costs.sell_commission_rate,
            costs.sell_tax_rate,
            costs.slippage_rate_per_side,
        )
    )
    return WalkForwardResult(
        windows=len(selected),
        selected_lookbacks=tuple(value[0] for value in selected),
        selected_target_volatilities=tuple(value[1] for value in selected),
        oos_daily_returns=tuple(oos_returns),
        cost_adjusted_sharpe=sharpe,
        max_drawdown=drawdown,
        trades=trades,
        sample_years=round(years, 4),
        parameter_stability=round(modal_count / len(selected), 4),
        includes_fees_taxes_slippage=has_costs,
        cost_source_urls=costs.source_urls,
        cost_assumptions=costs.assumptions,
    )


def run_opening_range_breakout(
    bars: list[IntradayBar],
    *,
    opening_range_minutes: int,
    costs: TradingCostProfile,
) -> BacktestResult:
    """Long-only ORB with next-bar entry, conservative stop fills, and flat close."""
    if not 5 <= opening_range_minutes <= 60:
        raise ValueError("opening range must be between 5 and 60 minutes")
    days = _group_intraday_bars(bars, minimum_bars=opening_range_minutes + 2)
    first_date = min(days)
    last_date = max(days)
    costs.validate_period(first_date, last_date)
    daily_returns: list[float] = []
    equity = 1.0
    curve = [equity]
    trades = 0
    total_cost_rate = 0.0
    for trading_date in sorted(days):
        session = days[trading_date]
        opening = session[:opening_range_minutes]
        opening_high = max(bar.high_price for bar in opening)
        opening_low = min(bar.low_price for bar in opening)
        net_return = 0.0
        for index in range(opening_range_minutes, len(session) - 1):
            signal_bar = session[index]
            if signal_bar.close_price <= opening_high:
                continue
            entry_bar = session[index + 1]
            entry_price = entry_bar.open_price * (1 + costs.slippage_rate_per_side)
            exit_base = session[-1].close_price
            for active_bar in session[index + 1 :]:
                if active_bar.low_price <= opening_low:
                    exit_base = min(opening_low, active_bar.open_price)
                    break
            exit_price = exit_base * (1 - costs.slippage_rate_per_side)
            if (
                not math.isfinite(entry_price)
                or not math.isfinite(exit_price)
                or entry_price <= 0
                or exit_price <= 0
            ):
                raise ValueError("backtest results must be finite")
            explicit_cost = (
                costs.buy_commission_rate
                + costs.sell_commission_rate
                + costs.sell_tax_rate
            )
            net_return = exit_price / entry_price - 1.0 - explicit_cost
            if not math.isfinite(net_return):
                raise ValueError("backtest results must be finite")
            trades += 1
            total_cost_rate += explicit_cost + 2 * costs.slippage_rate_per_side
            break
        daily_returns.append(net_return)
        equity = max(0.0, equity * (1.0 + net_return))
        if not math.isfinite(equity):
            raise ValueError("backtest results must be finite")
        curve.append(equity)
    sharpe, drawdown = _metrics(daily_returns)
    return BacktestResult(
        daily_returns=tuple(daily_returns),
        equity_curve=tuple(curve),
        cost_adjusted_sharpe=sharpe,
        max_drawdown=drawdown,
        trades=trades,
        total_cost_rate=round(total_cost_rate, 10),
    )


def walk_forward_opening_range_breakout(
    bars: list[IntradayBar],
    *,
    candidate_opening_minutes: tuple[int, ...],
    costs: TradingCostProfile,
    train_days: int = 252,
    test_days: int = 126,
) -> WalkForwardResult:
    if not candidate_opening_minutes:
        raise ValueError("at least one opening-range candidate is required")
    if train_days < 60 or test_days < 20:
        raise ValueError("intraday train/test windows are too short")
    if any(not 5 <= value <= 60 for value in candidate_opening_minutes):
        raise ValueError("opening-range candidates must be between 5 and 60")
    grouped = _group_intraday_bars(
        bars, minimum_bars=max(candidate_opening_minutes) + 2
    )
    dates = sorted(grouped)
    selected: list[int] = []
    oos_returns: list[float] = []
    trades = 0
    test_start = train_days
    while test_start + test_days <= len(dates):
        train_dates = dates[test_start - train_days : test_start]
        test_dates = dates[test_start : test_start + test_days]
        train_bars = [bar for day in train_dates for bar in grouped[day]]
        scored = [
            (
                run_opening_range_breakout(
                    train_bars,
                    opening_range_minutes=candidate,
                    costs=costs,
                ),
                candidate,
            )
            for candidate in candidate_opening_minutes
        ]
        _, chosen = max(
            scored,
            key=lambda item: (
                item[0].cost_adjusted_sharpe,
                -item[0].max_drawdown,
                -item[1],
            ),
        )
        test_bars = [bar for day in test_dates for bar in grouped[day]]
        result = run_opening_range_breakout(
            test_bars,
            opening_range_minutes=chosen,
            costs=costs,
        )
        selected.append(chosen)
        oos_returns.extend(result.daily_returns)
        trades += result.trades
        test_start += test_days
    if not selected:
        raise ValueError("not enough intraday days for one walk-forward window")
    sharpe, drawdown = _metrics(oos_returns)
    modal_count = Counter(selected).most_common(1)[0][1]
    years = (dates[-1] - dates[0]).days / 365.25
    has_costs = all(
        value > 0
        for value in (
            costs.buy_commission_rate,
            costs.sell_commission_rate,
            costs.sell_tax_rate,
            costs.slippage_rate_per_side,
        )
    )
    return WalkForwardResult(
        windows=len(selected),
        selected_lookbacks=(),
        selected_opening_range_minutes=tuple(selected),
        oos_daily_returns=tuple(oos_returns),
        cost_adjusted_sharpe=sharpe,
        max_drawdown=drawdown,
        trades=trades,
        sample_years=round(years, 4),
        parameter_stability=round(modal_count / len(selected), 4),
        includes_fees_taxes_slippage=has_costs,
        cost_source_urls=costs.source_urls,
        cost_assumptions=costs.assumptions,
    )


def _group_intraday_bars(
    bars: list[IntradayBar], *, minimum_bars: int
) -> dict[date, list[IntradayBar]]:
    if not bars:
        raise ValueError("intraday bars are required")
    symbols = {bar.symbol for bar in bars}
    if len(symbols) != 1:
        raise ValueError("intraday backtest requires exactly one symbol")
    timestamps = [bar.timestamp for bar in bars]
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("intraday timestamps must be unique")
    grouped: dict[date, list[IntradayBar]] = {}
    for bar in sorted(bars, key=lambda item: item.timestamp):
        grouped.setdefault(bar.trading_date, []).append(bar)
    if any(len(session) < minimum_bars for session in grouped.values()):
        raise ValueError("intraday session is incomplete")
    return grouped


def _validate_bars(bars: list[DailyBar], lookback: int) -> None:
    if len(bars) < lookback + 3:
        raise ValueError("insufficient bars")
    dates = [bar.trading_date for bar in bars]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("bars must be unique and sorted ascending")


def _metrics(returns: list[float]) -> tuple[float, float]:
    if not returns:
        return 0.0, 0.0
    if not all(math.isfinite(value) for value in returns):
        raise ValueError("backtest results must be finite")
    try:
        deviation = pstdev(returns)
        sharpe = mean(returns) / deviation * math.sqrt(252) if deviation > 0 else 0.0
    except (OverflowError, ValueError, ZeroDivisionError):
        raise ValueError("backtest metrics must be finite") from None
    if not math.isfinite(sharpe):
        raise ValueError("backtest metrics must be finite")
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        if not math.isfinite(equity):
            raise ValueError("backtest metrics must be finite")
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    return round(sharpe, 6), round(max_drawdown, 6)


def _is_finite_real(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False
