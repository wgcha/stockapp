from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterator, TYPE_CHECKING
import hashlib
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .trading_calendar import TradingCalendar


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


def estimate_paper_transaction_cost(
    *,
    broker_provider: str,
    side: str,
    notional: float,
    trading_date: date,
    mirae_commission_rate: float | None = None,
    slippage_bps: float = 5.0,
) -> float | None:
    """Return auditable estimated fees, tax and adverse slippage in KRW."""
    if not math.isfinite(notional) or not math.isfinite(slippage_bps):
        raise ValueError("paper notional and slippage must be finite")
    if side not in {"buy", "sell"} or notional <= 0:
        raise ValueError("valid side and positive notional are required")
    if slippage_bps < 0:
        raise ValueError("slippage cannot be negative")
    if broker_provider == "toss_invest":
        if not date(2026, 1, 1) <= trading_date <= date(2027, 5, 13):
            return None
        commission = 0.00015
    elif broker_provider == "mirae_asset":
        if mirae_commission_rate is None:
            return None
        if not math.isfinite(mirae_commission_rate) or not 0 <= mirae_commission_rate < 0.1:
            raise ValueError("Mirae commission rate must be in [0, 0.1)")
        commission = mirae_commission_rate
    else:
        return None
    sell_tax = 0.002 if side == "sell" else 0.0
    cost = notional * (commission + sell_tax + slippage_bps / 10_000)
    if not math.isfinite(cost):
        raise ValueError("paper transaction cost must be finite")
    return cost


@dataclass(frozen=True)
class PaperTradingDay:
    strategy_id: str
    version: str
    trading_date: date
    orders: int
    realized_pnl_pct: float
    critical_incidents: int = 0

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version:
            raise ValueError("strategy identity is required")
        if self.orders < 0 or self.critical_incidents < 0:
            raise ValueError("orders and incidents cannot be negative")
        if not math.isfinite(self.realized_pnl_pct) or not -100 <= self.realized_pnl_pct <= 1000:
            raise ValueError("paper PnL is outside the supported range")


@dataclass(frozen=True)
class PaperStrategyEvaluation:
    strategy_id: str
    version: str
    user_id: int
    stock_code: str
    action: str
    confidence: float
    evaluated_at: datetime
    api_healthy: bool
    critical_incidents: int = 0

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version or not self.stock_code:
            raise ValueError("paper evaluation identity is required")
        if self.user_id <= 0:
            raise ValueError("paper evaluation user_id must be positive")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("paper evaluation confidence must be in [0, 1]")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("paper evaluation time must be timezone-aware")
        if self.critical_incidents < 0:
            raise ValueError("critical incidents cannot be negative")

    @property
    def evaluation_id(self) -> str:
        raw = "|".join(
            (
                self.strategy_id,
                self.version,
                str(self.user_id),
                self.stock_code,
                self.action,
                self.evaluated_at.isoformat(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaperTradeFill:
    proposal_id: str
    strategy_id: str
    version: str
    user_id: int
    broker_provider: str
    stock_code: str
    side: str
    quantity: int
    fill_price: float
    filled_at: datetime
    transaction_cost: float | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.proposal_id,
                self.strategy_id,
                self.version,
                self.broker_provider,
                self.stock_code,
            )
        ):
            raise ValueError("paper fill identity is required")
        if self.user_id <= 0 or self.quantity <= 0 or not math.isfinite(self.fill_price) or self.fill_price <= 0:
            raise ValueError("paper fill quantity, price, and user must be positive")
        if self.side not in {"buy", "sell"}:
            raise ValueError("paper fill side must be buy or sell")
        if self.transaction_cost is not None and (
            not math.isfinite(self.transaction_cost) or self.transaction_cost < 0
        ):
            raise ValueError("paper fill transaction cost cannot be negative")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("paper fill time must be timezone-aware")


@dataclass(frozen=True)
class PaperPortfolioKey:
    strategy_id: str
    version: str
    user_id: int


@dataclass(frozen=True)
class PaperPortfolioValuation:
    strategy_id: str
    version: str
    user_id: int
    trading_date: date
    initial_cash: float
    cash: float
    market_value: float
    equity: float
    daily_pnl_pct: float
    cumulative_return_pct: float
    closing_prices: dict[str, float]
    api_healthy: bool
    costs_complete: bool
    critical_incidents: int = 0

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version or self.user_id <= 0:
            raise ValueError("paper valuation identity is required")
        if self.initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        if not all(
            math.isfinite(value)
            for value in (
                self.initial_cash,
                self.cash,
                self.market_value,
                self.equity,
                self.daily_pnl_pct,
                self.cumulative_return_pct,
            )
        ):
            raise ValueError("paper valuation amounts and returns must be finite")
        if self.critical_incidents < 0:
            raise ValueError("paper valuation incidents cannot be negative")
        if any(not math.isfinite(price) or price <= 0 for price in self.closing_prices.values()):
            raise ValueError("paper closing prices must be positive")


@dataclass(frozen=True)
class PaperModelObservation:
    strategy_id: str
    version: str
    market_symbol: str
    trading_date: date
    close_price: float
    target_exposure: float
    api_healthy: bool
    critical_incidents: int = 0

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version or not self.market_symbol:
            raise ValueError("paper model identity is required")
        if (
            not math.isfinite(self.close_price)
            or not math.isfinite(self.target_exposure)
            or self.close_price <= 0
            or not 0 <= self.target_exposure <= 1
        ):
            raise ValueError("paper model price and exposure are invalid")
        if self.critical_incidents < 0:
            raise ValueError("paper model incidents cannot be negative")


@dataclass(frozen=True)
class PaperModelDay:
    strategy_id: str
    version: str
    market_symbol: str
    trading_date: date
    close_price: float
    target_exposure: float
    applied_exposure: float
    daily_return_pct: float
    equity: float
    cumulative_return_pct: float
    transaction_cost_rate: float
    api_healthy: bool
    costs_complete: bool
    critical_incidents: int


class SQLitePaperTradingLedger:
    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = str(path)
        self.read_only = read_only
        self._connect_path = (
            Path(path).resolve().as_uri() + "?mode=ro" if read_only else self.path
        )
        if not self.read_only:
            self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._connect_path, uri=self.read_only)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trading_days (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    orders INTEGER NOT NULL CHECK(orders >= 0),
                    realized_pnl_pct REAL NOT NULL,
                    critical_incidents INTEGER NOT NULL CHECK(critical_incidents >= 0),
                    PRIMARY KEY(strategy_id, version, trading_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_model_days (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    market_symbol TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    close_price REAL NOT NULL,
                    target_exposure REAL NOT NULL,
                    applied_exposure REAL NOT NULL,
                    daily_return_pct REAL NOT NULL,
                    equity REAL NOT NULL,
                    cumulative_return_pct REAL NOT NULL,
                    transaction_cost_rate REAL NOT NULL,
                    api_healthy INTEGER NOT NULL,
                    costs_complete INTEGER NOT NULL,
                    critical_incidents INTEGER NOT NULL,
                    PRIMARY KEY(
                        strategy_id, version, market_symbol, trading_date
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_portfolio_valuations (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    trading_date TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    daily_pnl_pct REAL NOT NULL,
                    cumulative_return_pct REAL NOT NULL,
                    closing_prices_json TEXT NOT NULL,
                    api_healthy INTEGER NOT NULL,
                    costs_complete INTEGER NOT NULL,
                    critical_incidents INTEGER NOT NULL,
                    PRIMARY KEY(strategy_id, version, user_id, trading_date)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_strategy_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    api_healthy INTEGER NOT NULL,
                    critical_incidents INTEGER NOT NULL
                        CHECK(critical_incidents >= 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trade_fills (
                    proposal_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    broker_provider TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    fill_price REAL NOT NULL CHECK(fill_price > 0),
                    filled_at TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    transaction_cost REAL
                )
                """
            )
            connection.execute(
                """
                DELETE FROM paper_strategy_evaluations
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM paper_strategy_evaluations
                    GROUP BY strategy_id, version, user_id, stock_code, trading_date
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_paper_evaluation_daily_target
                ON paper_strategy_evaluations(
                    strategy_id, version, user_id, stock_code, trading_date
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(paper_trade_fills)"
                ).fetchall()
            }
            if "transaction_cost" not in columns:
                connection.execute(
                    "ALTER TABLE paper_trade_fills ADD COLUMN transaction_cost REAL"
                )

    def record_day(self, value: PaperTradingDay) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM paper_trading_days
                WHERE strategy_id = ? AND version = ? AND trading_date = ?
                """,
                (value.strategy_id, value.version, value.trading_date.isoformat()),
            ).fetchone()
            if existing is not None:
                raise ValueError("paper trading day is immutable once recorded")
            connection.execute(
                """
                INSERT INTO paper_trading_days(
                    strategy_id, version, trading_date, orders,
                    realized_pnl_pct, critical_incidents
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    value.strategy_id,
                    value.version,
                    value.trading_date.isoformat(),
                    value.orders,
                    value.realized_pnl_pct,
                    value.critical_incidents,
                ),
            )

    def completed_days(
        self,
        strategy_id: str,
        version: str,
        *,
        as_of: date | None = None,
        trading_calendar: TradingCalendar | None = None,
    ) -> int:
        """Count qualified dates with matching user evidence or healthy model evidence."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH evaluation_health_days AS (
                    SELECT trading_date
                    FROM paper_strategy_evaluations
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY trading_date
                    HAVING MIN(api_healthy) = 1
                       AND SUM(critical_incidents) = 0
                       AND MIN(CASE WHEN typeof(confidence) IN ('integer', 'real')
                                    AND confidence IS NOT NULL
                                    AND abs(confidence) <= 1.7976931348623157e308
                                    THEN 1 ELSE 0 END) = 1
                ), evaluation_user_days AS (
                    SELECT trading_date, user_id
                    FROM paper_strategy_evaluations
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY trading_date, user_id
                    HAVING MIN(api_healthy) = 1
                       AND SUM(critical_incidents) = 0
                ), valuation_health_days AS (
                    SELECT trading_date
                    FROM paper_portfolio_valuations
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY trading_date
                    HAVING MIN(api_healthy) = 1
                       AND MIN(costs_complete) = 1
                       AND SUM(critical_incidents) = 0
                       AND MIN(CASE WHEN typeof(initial_cash) IN ('integer', 'real')
                                    AND initial_cash IS NOT NULL
                                    AND abs(initial_cash) <= 1.7976931348623157e308
                                    AND typeof(cash) IN ('integer', 'real')
                                    AND cash IS NOT NULL
                                    AND abs(cash) <= 1.7976931348623157e308
                                    AND typeof(market_value) IN ('integer', 'real')
                                    AND market_value IS NOT NULL
                                    AND abs(market_value) <= 1.7976931348623157e308
                                    AND typeof(equity) IN ('integer', 'real')
                                    AND equity IS NOT NULL
                                    AND abs(equity) <= 1.7976931348623157e308
                                    AND typeof(daily_pnl_pct) IN ('integer', 'real')
                                    AND daily_pnl_pct IS NOT NULL
                                    AND abs(daily_pnl_pct) <= 1.7976931348623157e308
                                    AND typeof(cumulative_return_pct) IN ('integer', 'real')
                                    AND cumulative_return_pct IS NOT NULL
                                    AND abs(cumulative_return_pct) <= 1.7976931348623157e308
                                    AND json_valid(closing_prices_json) = 1
                                    THEN 1 ELSE 0 END) = 1
                ), valuation_user_days AS (
                    SELECT trading_date, user_id
                    FROM paper_portfolio_valuations
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY trading_date, user_id
                    HAVING MIN(api_healthy) = 1
                       AND MIN(costs_complete) = 1
                       AND SUM(critical_incidents) = 0
                ), user_days AS (
                    SELECT evaluation_user_days.trading_date
                    FROM evaluation_user_days
                    INNER JOIN valuation_user_days USING(trading_date, user_id)
                    INNER JOIN evaluation_health_days USING(trading_date)
                    INNER JOIN valuation_health_days USING(trading_date)
                ), model_days AS (
                    SELECT trading_date
                    FROM paper_model_days
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY trading_date
                    HAVING MIN(api_healthy) = 1
                       AND MIN(costs_complete) = 1
                       AND SUM(critical_incidents) = 0
                       AND MIN(CASE WHEN typeof(close_price) IN ('integer', 'real')
                                    AND close_price IS NOT NULL
                                    AND abs(close_price) <= 1.7976931348623157e308
                                    AND typeof(target_exposure) IN ('integer', 'real')
                                    AND target_exposure IS NOT NULL
                                    AND abs(target_exposure) <= 1.7976931348623157e308
                                    AND typeof(applied_exposure) IN ('integer', 'real')
                                    AND applied_exposure IS NOT NULL
                                    AND abs(applied_exposure) <= 1.7976931348623157e308
                                    AND typeof(daily_return_pct) IN ('integer', 'real')
                                    AND daily_return_pct IS NOT NULL
                                    AND abs(daily_return_pct) <= 1.7976931348623157e308
                                    AND typeof(equity) IN ('integer', 'real')
                                    AND equity IS NOT NULL
                                    AND abs(equity) <= 1.7976931348623157e308
                                    AND typeof(cumulative_return_pct) IN ('integer', 'real')
                                    AND cumulative_return_pct IS NOT NULL
                                    AND abs(cumulative_return_pct) <= 1.7976931348623157e308
                                    AND typeof(transaction_cost_rate) IN ('integer', 'real')
                                    AND transaction_cost_rate IS NOT NULL
                                    AND abs(transaction_cost_rate) <= 1.7976931348623157e308
                                    THEN 1 ELSE 0 END) = 1
                )
                SELECT trading_date FROM user_days
                UNION
                SELECT trading_date FROM model_days
                """,
                (
                    strategy_id, version,
                    strategy_id, version,
                    strategy_id, version,
                    strategy_id, version,
                    strategy_id, version,
                ),
            ).fetchall()
        return _count_eligible_evidence_dates(
            (date.fromisoformat(str(row["trading_date"])) for row in rows),
            as_of=as_of,
            trading_calendar=trading_calendar,
        )

    def record_evaluation(self, value: PaperStrategyEvaluation) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_strategy_evaluations(
                    evaluation_id, strategy_id, version, user_id, stock_code,
                    action, confidence, evaluated_at, trading_date,
                    api_healthy, critical_incidents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.evaluation_id,
                    value.strategy_id,
                    value.version,
                    value.user_id,
                    value.stock_code,
                    value.action,
                    value.confidence,
                    value.evaluated_at.isoformat(),
                    value.evaluated_at.astimezone(KOREA_TIMEZONE).date().isoformat(),
                    int(value.api_healthy),
                    value.critical_incidents,
                ),
            )
        return cursor.rowcount == 1

    def evaluation_count(self, strategy_id: str, version: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM paper_strategy_evaluations
                WHERE strategy_id = ? AND version = ?
                """,
                (strategy_id, version),
            ).fetchone()
        return int(row["count"])

    def record_fill(self, value: PaperTradeFill) -> bool:
        """Persist one owner-approved simulated fill, idempotently by proposal."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_trade_fills(
                    proposal_id, strategy_id, version, user_id,
                    broker_provider, stock_code, side, quantity, fill_price,
                    filled_at, trading_date, transaction_cost
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.proposal_id,
                    value.strategy_id,
                    value.version,
                    value.user_id,
                    value.broker_provider,
                    value.stock_code,
                    value.side,
                    value.quantity,
                    value.fill_price,
                    value.filled_at.isoformat(),
                    value.filled_at.astimezone(KOREA_TIMEZONE).date().isoformat(),
                    value.transaction_cost,
                ),
            )
        return cursor.rowcount == 1

    def fill_count(
        self,
        *,
        user_id: int | None = None,
        strategy_id: str | None = None,
        version: str | None = None,
    ) -> int:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("user_id", user_id),
            ("strategy_id", strategy_id),
            ("version", version),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM paper_trade_fills" + where,
                parameters,
            ).fetchone()
        return int(row["count"])

    def list_portfolios(self) -> tuple[PaperPortfolioKey, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT strategy_id, version, user_id FROM paper_trade_fills
                UNION
                SELECT strategy_id, version, user_id
                FROM paper_strategy_evaluations
                ORDER BY strategy_id, version, user_id
                """
            ).fetchall()
        return tuple(
            PaperPortfolioKey(row["strategy_id"], row["version"], row["user_id"])
            for row in rows
        )

    def portfolio_symbols(
        self, key: PaperPortfolioKey, *, through: date
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT stock_code FROM paper_trade_fills
                WHERE strategy_id = ? AND version = ? AND user_id = ?
                  AND trading_date <= ?
                ORDER BY stock_code
                """,
                (key.strategy_id, key.version, key.user_id, through.isoformat()),
            ).fetchall()
        return tuple(str(row["stock_code"]) for row in rows)

    def value_portfolio(
        self,
        key: PaperPortfolioKey,
        *,
        trading_date: date,
        initial_cash: float,
        closing_prices: dict[str, float],
        api_healthy: bool = True,
    ) -> PaperPortfolioValuation:
        if initial_cash <= 0:
            raise ValueError("paper initial cash must be positive")
        existing = self.get_valuation(key, trading_date)
        if existing is not None:
            return existing
        with self._connect() as connection:
            fills = connection.execute(
                """
                SELECT stock_code, side, quantity, fill_price, transaction_cost
                FROM paper_trade_fills
                WHERE strategy_id = ? AND version = ? AND user_id = ?
                  AND trading_date <= ?
                ORDER BY filled_at, proposal_id
                """,
                (key.strategy_id, key.version, key.user_id, trading_date.isoformat()),
            ).fetchall()
            previous = connection.execute(
                """
                SELECT equity FROM paper_portfolio_valuations
                WHERE strategy_id = ? AND version = ? AND user_id = ?
                  AND trading_date < ?
                ORDER BY trading_date DESC LIMIT 1
                """,
                (key.strategy_id, key.version, key.user_id, trading_date.isoformat()),
            ).fetchone()
        cash = float(initial_cash)
        positions: dict[str, int] = {}
        costs_complete = True
        incidents = 0
        for fill in fills:
            symbol = str(fill["stock_code"])
            quantity = int(fill["quantity"])
            notional = quantity * float(fill["fill_price"])
            cost = fill["transaction_cost"]
            if cost is None:
                costs_complete = False
                cost_value = 0.0
            else:
                cost_value = float(cost)
            if fill["side"] == "buy":
                positions[symbol] = positions.get(symbol, 0) + quantity
                cash -= notional + cost_value
            else:
                positions[symbol] = positions.get(symbol, 0) - quantity
                cash += notional - cost_value
                if positions[symbol] < 0:
                    incidents += 1
        if cash < 0:
            incidents += 1
        market_value = 0.0
        used_prices: dict[str, float] = {}
        for symbol, quantity in positions.items():
            if quantity == 0:
                continue
            price = closing_prices.get(symbol)
            if price is None or price <= 0:
                incidents += 1
                continue
            used_prices[symbol] = float(price)
            market_value += quantity * float(price)
        equity = cash + market_value
        base = float(previous["equity"]) if previous is not None else initial_cash
        daily_pnl_pct = ((equity / base) - 1) * 100 if base else -100.0
        cumulative = ((equity / initial_cash) - 1) * 100
        valuation = PaperPortfolioValuation(
            key.strategy_id,
            key.version,
            key.user_id,
            trading_date,
            initial_cash,
            cash,
            market_value,
            equity,
            daily_pnl_pct,
            cumulative,
            used_prices,
            api_healthy,
            costs_complete,
            incidents,
        )
        self.record_valuation(valuation)
        return valuation

    def get_valuation(
        self, key: PaperPortfolioKey, trading_date: date
    ) -> PaperPortfolioValuation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM paper_portfolio_valuations
                WHERE strategy_id = ? AND version = ? AND user_id = ?
                  AND trading_date = ?
                """,
                (
                    key.strategy_id,
                    key.version,
                    key.user_id,
                    trading_date.isoformat(),
                ),
            ).fetchone()
        if row is None:
            return None
        return PaperPortfolioValuation(
            strategy_id=str(row["strategy_id"]),
            version=str(row["version"]),
            user_id=int(row["user_id"]),
            trading_date=date.fromisoformat(str(row["trading_date"])),
            initial_cash=float(row["initial_cash"]),
            cash=float(row["cash"]),
            market_value=float(row["market_value"]),
            equity=float(row["equity"]),
            daily_pnl_pct=float(row["daily_pnl_pct"]),
            cumulative_return_pct=float(row["cumulative_return_pct"]),
            closing_prices={
                str(symbol): float(price)
                for symbol, price in json.loads(row["closing_prices_json"]).items()
            },
            api_healthy=bool(row["api_healthy"]),
            costs_complete=bool(row["costs_complete"]),
            critical_incidents=int(row["critical_incidents"]),
        )

    def record_valuation(self, value: PaperPortfolioValuation) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_portfolio_valuations(
                    strategy_id, version, user_id, trading_date,
                    initial_cash, cash, market_value, equity,
                    daily_pnl_pct, cumulative_return_pct, closing_prices_json,
                    api_healthy, costs_complete, critical_incidents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.strategy_id,
                    value.version,
                    value.user_id,
                    value.trading_date.isoformat(),
                    value.initial_cash,
                    value.cash,
                    value.market_value,
                    value.equity,
                    value.daily_pnl_pct,
                    value.cumulative_return_pct,
                    json.dumps(value.closing_prices, sort_keys=True),
                    int(value.api_healthy),
                    int(value.costs_complete),
                    value.critical_incidents,
                ),
            )
        return cursor.rowcount == 1

    def valuation_count(
        self, strategy_id: str, version: str, *, user_id: int | None = None
    ) -> int:
        clause = " AND user_id = ?" if user_id is not None else ""
        parameters: tuple[object, ...] = (
            (strategy_id, version, user_id)
            if user_id is not None
            else (strategy_id, version)
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM paper_portfolio_valuations
                WHERE strategy_id = ? AND version = ?
                """
                + clause,
                parameters,
            ).fetchone()
        return int(row["count"])

    def list_latest_valuations(
        self, user_id: int
    ) -> tuple[PaperPortfolioValuation, ...]:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT value.*
                FROM paper_portfolio_valuations AS value
                INNER JOIN (
                    SELECT strategy_id, version, user_id,
                           MAX(trading_date) AS trading_date
                    FROM paper_portfolio_valuations
                    WHERE user_id = ?
                    GROUP BY strategy_id, version, user_id
                ) AS latest
                  ON value.strategy_id = latest.strategy_id
                 AND value.version = latest.version
                 AND value.user_id = latest.user_id
                 AND value.trading_date = latest.trading_date
                ORDER BY value.strategy_id, value.version
                """,
                (user_id,),
            ).fetchall()
        return tuple(_valuation_from_row(row) for row in rows)

    def record_model_observation(
        self,
        value: PaperModelObservation,
        *,
        initial_equity: float,
        buy_cost_rate: float | None,
        sell_cost_rate: float | None,
    ) -> PaperModelDay:
        if not math.isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("paper model initial equity must be positive")
        for rate in (buy_cost_rate, sell_cost_rate):
            if rate is not None and (
                not math.isfinite(rate) or not 0 <= rate < 0.1
            ):
                raise ValueError("paper model cost rates must be in [0, 0.1)")
        existing = self.get_model_day(
            value.strategy_id,
            value.version,
            value.market_symbol,
            value.trading_date,
        )
        if existing is not None:
            return existing
        with self._connect() as connection:
            previous = connection.execute(
                """
                SELECT * FROM paper_model_days
                WHERE strategy_id = ? AND version = ? AND market_symbol = ?
                  AND trading_date < ?
                ORDER BY trading_date DESC LIMIT 1
                """,
                (
                    value.strategy_id,
                    value.version,
                    value.market_symbol,
                    value.trading_date.isoformat(),
                ),
            ).fetchone()
        if previous is None:
            applied_exposure = 0.0
            previous_target = 0.0
            previous_equity = initial_equity
            gross_return = 0.0
        else:
            applied_exposure = float(previous["target_exposure"])
            previous_target = applied_exposure
            previous_equity = float(previous["equity"])
            gross_return = applied_exposure * (
                value.close_price / float(previous["close_price"]) - 1.0
            )
        turnover = abs(value.target_exposure - previous_target)
        increasing = value.target_exposure > previous_target
        selected_rate = buy_cost_rate if increasing else sell_cost_rate
        costs_complete = selected_rate is not None
        transaction_cost = turnover * float(selected_rate or 0.0)
        net_return = gross_return - transaction_cost
        incidents = value.critical_incidents
        if net_return <= -1:
            incidents += 1
            equity = 0.0
        else:
            equity = previous_equity * (1.0 + net_return)
        if net_return > -1 and equity <= 0:
            incidents += 1
        day = PaperModelDay(
            value.strategy_id,
            value.version,
            value.market_symbol,
            value.trading_date,
            value.close_price,
            value.target_exposure,
            applied_exposure,
            net_return * 100,
            equity,
            (equity / initial_equity - 1.0) * 100,
            transaction_cost,
            value.api_healthy,
            costs_complete,
            incidents,
        )
        if not all(
            math.isfinite(number)
            for number in (
                day.daily_return_pct,
                day.equity,
                day.cumulative_return_pct,
                day.transaction_cost_rate,
            )
        ):
            raise ValueError("paper model results must be finite")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_model_days(
                    strategy_id, version, market_symbol, trading_date,
                    close_price, target_exposure, applied_exposure,
                    daily_return_pct, equity, cumulative_return_pct,
                    transaction_cost_rate, api_healthy, costs_complete,
                    critical_incidents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day.strategy_id,
                    day.version,
                    day.market_symbol,
                    day.trading_date.isoformat(),
                    day.close_price,
                    day.target_exposure,
                    day.applied_exposure,
                    day.daily_return_pct,
                    day.equity,
                    day.cumulative_return_pct,
                    day.transaction_cost_rate,
                    int(day.api_healthy),
                    int(day.costs_complete),
                    day.critical_incidents,
                ),
            )
        return day

    def get_model_day(
        self,
        strategy_id: str,
        version: str,
        market_symbol: str,
        trading_date: date,
    ) -> PaperModelDay | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM paper_model_days
                WHERE strategy_id = ? AND version = ? AND market_symbol = ?
                  AND trading_date = ?
                """,
                (strategy_id, version, market_symbol, trading_date.isoformat()),
            ).fetchone()
        return _model_day_from_row(row) if row is not None else None

    def list_latest_model_days(self) -> tuple[PaperModelDay, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT day.* FROM paper_model_days AS day
                INNER JOIN (
                    SELECT strategy_id, version, market_symbol,
                           MAX(trading_date) AS trading_date
                    FROM paper_model_days
                    GROUP BY strategy_id, version, market_symbol
                ) AS latest
                  ON day.strategy_id = latest.strategy_id
                 AND day.version = latest.version
                 AND day.market_symbol = latest.market_symbol
                 AND day.trading_date = latest.trading_date
                ORDER BY day.strategy_id, day.version, day.market_symbol
                """
            ).fetchall()
        return tuple(_model_day_from_row(row) for row in rows)

    def total_incidents(self, strategy_id: str, version: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(critical_incidents), 0)
                     FROM paper_trading_days
                     WHERE strategy_id = ? AND version = ?)
                    +
                    (SELECT COALESCE(SUM(critical_incidents), 0)
                     FROM paper_strategy_evaluations
                     WHERE strategy_id = ? AND version = ?)
                    +
                    (SELECT COALESCE(SUM(critical_incidents), 0)
                     FROM paper_model_days
                     WHERE strategy_id = ? AND version = ?) AS count
                """,
                (
                    strategy_id,
                    version,
                    strategy_id,
                    version,
                    strategy_id,
                    version,
                ),
            ).fetchone()
        return int(row["count"])


def _valuation_from_row(row: sqlite3.Row) -> PaperPortfolioValuation:
    return PaperPortfolioValuation(
        strategy_id=str(row["strategy_id"]),
        version=str(row["version"]),
        user_id=int(row["user_id"]),
        trading_date=date.fromisoformat(str(row["trading_date"])),
        initial_cash=float(row["initial_cash"]),
        cash=float(row["cash"]),
        market_value=float(row["market_value"]),
        equity=float(row["equity"]),
        daily_pnl_pct=float(row["daily_pnl_pct"]),
        cumulative_return_pct=float(row["cumulative_return_pct"]),
        closing_prices={
            str(symbol): float(price)
            for symbol, price in json.loads(row["closing_prices_json"]).items()
        },
        api_healthy=bool(row["api_healthy"]),
        costs_complete=bool(row["costs_complete"]),
        critical_incidents=int(row["critical_incidents"]),
    )


def _model_day_from_row(row: sqlite3.Row) -> PaperModelDay:
    return PaperModelDay(
        strategy_id=str(row["strategy_id"]),
        version=str(row["version"]),
        market_symbol=str(row["market_symbol"]),
        trading_date=date.fromisoformat(str(row["trading_date"])),
        close_price=float(row["close_price"]),
        target_exposure=float(row["target_exposure"]),
        applied_exposure=float(row["applied_exposure"]),
        daily_return_pct=float(row["daily_return_pct"]),
        equity=float(row["equity"]),
        cumulative_return_pct=float(row["cumulative_return_pct"]),
        transaction_cost_rate=float(row["transaction_cost_rate"]),
        api_healthy=bool(row["api_healthy"]),
        costs_complete=bool(row["costs_complete"]),
        critical_incidents=int(row["critical_incidents"]),
    )


def _count_eligible_evidence_dates(
    trading_dates: Iterator[date],
    *,
    as_of: date | None,
    trading_calendar: TradingCalendar | None,
) -> int:
    eligible = set(trading_dates)
    if as_of is not None:
        eligible = {day for day in eligible if day <= as_of}
    if trading_calendar is not None:
        eligible = {
            day for day in eligible if trading_calendar.session_for(day) is not None
        }
    return len(eligible)
