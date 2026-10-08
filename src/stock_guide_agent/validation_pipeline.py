from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .backtest import (
    DailyBar,
    TradingCostProfile,
    WalkForwardResult,
    walk_forward_momentum,
    walk_forward_volatility_managed,
    walk_forward_opening_range_breakout,
)
from .intraday import IntradayBar, SQLiteIntradayBarStore
from .paper_trading import KOREA_TIMEZONE
from .paper_trading import SQLitePaperTradingLedger
from .strategies import PromotionDecision, ValidationReport
from .strategy_store import SQLiteStrategyStore

if TYPE_CHECKING:
    from .trading_calendar import TradingCalendar


@dataclass(frozen=True)
class ValidationOutcome:
    walk_forward: WalkForwardResult
    report: ValidationReport
    decision: PromotionDecision


class StrategyValidationPipeline:
    def __init__(
        self,
        strategy_store: SQLiteStrategyStore,
        paper_ledger: SQLitePaperTradingLedger,
        intraday_store: SQLiteIntradayBarStore | None = None,
        trading_calendar: TradingCalendar | None = None,
    ) -> None:
        self.strategy_store = strategy_store
        self.paper_ledger = paper_ledger
        self.intraday_store = intraday_store
        self._trading_calendar = trading_calendar

    @property
    def trading_calendar(self) -> TradingCalendar:
        if self._trading_calendar is None:
            from .trading_calendar import KrxTradingCalendar

            self._trading_calendar = KrxTradingCalendar()
        return self._trading_calendar

    def validate_momentum(
        self,
        *,
        strategy_id: str,
        version: str,
        market: str,
        bars: list[DailyBar],
        costs: TradingCostProfile,
        checked_on: date,
        candidate_lookbacks: tuple[int, ...] = (20, 60, 120, 200),
        train_days: int = 756,
        test_days: int = 252,
    ) -> ValidationOutcome:
        definition = self.strategy_store.get_record(
            strategy_id, version
        ).definition
        if definition.signal_engine != "time_series_momentum":
            raise ValueError(
                "momentum validation can only be applied to a time_series_momentum engine"
            )
        eligible_bars = [bar for bar in bars if bar.trading_date <= checked_on]
        walk_forward = walk_forward_momentum(
            eligible_bars,
            candidate_lookbacks=candidate_lookbacks,
            costs=costs,
            train_days=train_days,
            test_days=test_days,
        )
        paper_days = self.paper_ledger.completed_days(
            strategy_id,
            version,
            as_of=checked_on,
            trading_calendar=self.trading_calendar,
        )
        report = walk_forward.to_validation_report(
            strategy_id=strategy_id,
            version=version,
            market=market,
            paper_trading_days=paper_days,
            signal_engine="time_series_momentum",
        )
        decision = self.strategy_store.apply_validation(
            report, checked_on=checked_on
        )
        return ValidationOutcome(walk_forward, report, decision)

    def validate_volatility_managed(
        self,
        *,
        strategy_id: str,
        version: str,
        market: str,
        bars: list[DailyBar],
        costs: TradingCostProfile,
        checked_on: date,
        candidate_lookbacks: tuple[int, ...] = (20, 40, 60),
        candidate_target_volatilities: tuple[float, ...] = (0.10, 0.15, 0.20),
        rebalance_threshold: float = 0.05,
        train_days: int = 756,
        test_days: int = 252,
    ) -> ValidationOutcome:
        definition = self.strategy_store.get_record(
            strategy_id, version
        ).definition
        if definition.signal_engine != "volatility_managed_exposure":
            raise ValueError(
                "volatility validation can only be applied to a "
                "volatility_managed_exposure engine"
            )
        eligible_bars = [bar for bar in bars if bar.trading_date <= checked_on]
        walk_forward = walk_forward_volatility_managed(
            eligible_bars,
            candidate_lookbacks=candidate_lookbacks,
            candidate_target_volatilities=candidate_target_volatilities,
            rebalance_threshold=rebalance_threshold,
            costs=costs,
            train_days=train_days,
            test_days=test_days,
        )
        paper_days = self.paper_ledger.completed_days(
            strategy_id,
            version,
            as_of=checked_on,
            trading_calendar=self.trading_calendar,
        )
        report = walk_forward.to_validation_report(
            strategy_id=strategy_id,
            version=version,
            market=market,
            paper_trading_days=paper_days,
            signal_engine="volatility_managed_exposure",
        )
        decision = self.strategy_store.apply_validation(
            report, checked_on=checked_on
        )
        return ValidationOutcome(walk_forward, report, decision)

    def validate_opening_range_breakout(
        self,
        *,
        strategy_id: str,
        version: str,
        market: str,
        bars: list[IntradayBar],
        costs: TradingCostProfile,
        checked_on: date,
        candidate_opening_minutes: tuple[int, ...] = (5,),
        train_days: int = 252,
        test_days: int = 126,
    ) -> ValidationOutcome:
        definition = self.strategy_store.get_record(
            strategy_id, version
        ).definition
        if definition.signal_engine != "opening_range_breakout":
            raise ValueError(
                "opening-range validation can only be applied to an "
                "opening_range_breakout engine"
            )
        eligible_bars = [
            bar
            for bar in bars
            if bar.timestamp.astimezone(KOREA_TIMEZONE).date() <= checked_on
        ]
        walk_forward = walk_forward_opening_range_breakout(
            eligible_bars,
            candidate_opening_minutes=candidate_opening_minutes,
            costs=costs,
            train_days=train_days,
            test_days=test_days,
        )
        paper_days = (
            self.intraday_store.qualified_paper_days(
                strategy_id,
                version,
                as_of=checked_on,
                trading_calendar=self.trading_calendar,
            )
            if self.intraday_store is not None
            else 0
        )
        report = walk_forward.to_validation_report(
            strategy_id=strategy_id,
            version=version,
            market=market,
            paper_trading_days=paper_days,
            signal_engine="opening_range_breakout",
        )
        decision = self.strategy_store.apply_validation(
            report, checked_on=checked_on
        )
        return ValidationOutcome(walk_forward, report, decision)
