from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
import math
from pathlib import Path
import sqlite3
from typing import Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .trading_calendar import TradingCalendar


@dataclass(frozen=True)
class IntradayBar:
    symbol: str
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("intraday symbol is required")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("intraday timestamp must be timezone-aware")
        if not all(
            math.isfinite(value)
            for value in (
                self.open_price,
                self.high_price,
                self.low_price,
                self.close_price,
                self.volume,
            )
        ):
            raise ValueError("intraday bar values must be finite")
        if min(
            self.open_price,
            self.high_price,
            self.low_price,
            self.close_price,
        ) <= 0:
            raise ValueError("intraday prices must be positive")
        if not (
            self.low_price
            <= min(self.open_price, self.close_price)
            <= max(self.open_price, self.close_price)
            <= self.high_price
        ):
            raise ValueError("intraday OHLC relationship is invalid")
        if self.volume < 0:
            raise ValueError("intraday volume cannot be negative")

    @property
    def trading_date(self) -> date:
        return self.timestamp.date()


@dataclass(frozen=True)
class IntradayPaperDay:
    strategy_id: str
    version: str
    symbol: str
    trading_date: date
    opening_range_minutes: int
    daily_return_pct: float
    equity: float
    api_healthy: bool
    costs_complete: bool
    critical_incidents: int


class SQLiteIntradayBarStore:
    """Immutable, conflict-detecting archive for official one-minute bars."""

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS intraday_bars (
                    symbol TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    bar_timestamp TEXT NOT NULL,
                    open_price REAL NOT NULL,
                    high_price REAL NOT NULL,
                    low_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    volume REAL NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, bar_timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_intraday_symbol_date
                    ON intraday_bars(symbol, trading_date, bar_timestamp);
                CREATE TABLE IF NOT EXISTS intraday_paper_days (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    opening_range_minutes INTEGER NOT NULL,
                    daily_return_pct REAL NOT NULL,
                    equity REAL NOT NULL,
                    api_healthy INTEGER NOT NULL,
                    costs_complete INTEGER NOT NULL,
                    critical_incidents INTEGER NOT NULL,
                    PRIMARY KEY(strategy_id, version, symbol, trading_date)
                );
                """
            )

    def record_bars(self, bars: list[IntradayBar], *, fetched_at: datetime) -> int:
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("intraday fetched_at must be timezone-aware")
        if not bars:
            return 0
        inserted = 0
        with self._connect() as connection:
            for bar in bars:
                row = connection.execute(
                    """
                    SELECT open_price, high_price, low_price, close_price, volume
                    FROM intraday_bars
                    WHERE symbol = ? AND bar_timestamp = ?
                    """,
                    (bar.symbol, bar.timestamp.isoformat()),
                ).fetchone()
                values = (
                    bar.open_price,
                    bar.high_price,
                    bar.low_price,
                    bar.close_price,
                    bar.volume,
                )
                if row is not None:
                    stored = tuple(float(row[name]) for name in (
                        "open_price",
                        "high_price",
                        "low_price",
                        "close_price",
                        "volume",
                    ))
                    if stored != values:
                        raise ValueError("immutable intraday bar conflicts with stored data")
                    continue
                connection.execute(
                    """
                    INSERT INTO intraday_bars(
                        symbol, trading_date, bar_timestamp,
                        open_price, high_price, low_price, close_price,
                        volume, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bar.symbol,
                        bar.trading_date.isoformat(),
                        bar.timestamp.isoformat(),
                        *values,
                        fetched_at.isoformat(),
                    ),
                )
                inserted += 1
        return inserted

    def bar_count(self, symbol: str, trading_date: date) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM intraday_bars
                WHERE symbol = ? AND trading_date = ?
                """,
                (symbol, trading_date.isoformat()),
            ).fetchone()
        return int(row["count"])

    def has_complete_day(
        self, symbol: str, trading_date: date, *, minimum_bars: int = 300
    ) -> bool:
        if minimum_bars < 1:
            raise ValueError("minimum intraday bars must be positive")
        return self.bar_count(symbol, trading_date) >= minimum_bars

    def list_bars(self, symbol: str, trading_date: date) -> tuple[IntradayBar, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM intraday_bars
                WHERE symbol = ? AND trading_date = ?
                ORDER BY bar_timestamp
                """,
                (symbol, trading_date.isoformat()),
            ).fetchall()
        return tuple(
            IntradayBar(
                symbol=str(row["symbol"]),
                timestamp=datetime.fromisoformat(str(row["bar_timestamp"])),
                open_price=float(row["open_price"]),
                high_price=float(row["high_price"]),
                low_price=float(row["low_price"]),
                close_price=float(row["close_price"]),
                volume=float(row["volume"]),
            )
            for row in rows
        )

    def complete_day_count(
        self,
        symbol: str,
        *,
        minimum_bars: int = 300,
        as_of: date | None = None,
        trading_calendar: TradingCalendar | None = None,
    ) -> int:
        if minimum_bars < 1:
            raise ValueError("minimum intraday bars must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trading_date FROM intraday_bars
                WHERE symbol = ?
                GROUP BY trading_date
                HAVING COUNT(*) >= ?
                """,
                (symbol, minimum_bars),
            ).fetchall()
        eligible = {
            date.fromisoformat(str(row["trading_date"])) for row in rows
        }
        if as_of is not None:
            eligible = {day for day in eligible if day <= as_of}
        if trading_calendar is not None:
            eligible = {
                day for day in eligible if trading_calendar.session_for(day) is not None
            }
        return len(eligible)

    def complete_dates(
        self, symbol: str, *, minimum_bars: int = 300
    ) -> tuple[date, ...]:
        if minimum_bars < 1:
            raise ValueError("minimum intraday bars must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trading_date FROM intraday_bars
                WHERE symbol = ?
                GROUP BY trading_date
                HAVING COUNT(*) >= ?
                ORDER BY trading_date
                """,
                (symbol, minimum_bars),
            ).fetchall()
        return tuple(date.fromisoformat(str(row["trading_date"])) for row in rows)

    def list_complete_bars(
        self, symbol: str, *, minimum_bars: int = 300
    ) -> list[IntradayBar]:
        return [
            bar
            for trading_date in self.complete_dates(
                symbol, minimum_bars=minimum_bars
            )
            for bar in self.list_bars(symbol, trading_date)
        ]

    def record_paper_day(
        self,
        *,
        strategy_id: str,
        version: str,
        symbol: str,
        trading_date: date,
        opening_range_minutes: int,
        daily_return_pct: float,
        initial_equity: float,
        api_healthy: bool,
        costs_complete: bool,
        critical_incidents: int = 0,
    ) -> IntradayPaperDay:
        if not strategy_id or not version or not symbol:
            raise ValueError("intraday paper identity is required")
        if not 5 <= opening_range_minutes <= 60:
            raise ValueError("intraday paper opening range is invalid")
        if (
            not math.isfinite(initial_equity)
            or not math.isfinite(daily_return_pct)
            or initial_equity <= 0
            or daily_return_pct <= -100
        ):
            raise ValueError("intraday paper return or capital is invalid")
        if critical_incidents < 0:
            raise ValueError("intraday paper incidents cannot be negative")
        existing = self.get_paper_day(
            strategy_id, version, symbol, trading_date
        )
        if existing is not None:
            return existing
        with self._connect() as connection:
            previous = connection.execute(
                """
                SELECT equity FROM intraday_paper_days
                WHERE strategy_id = ? AND version = ? AND symbol = ?
                  AND trading_date < ?
                ORDER BY trading_date DESC LIMIT 1
                """,
                (strategy_id, version, symbol, trading_date.isoformat()),
            ).fetchone()
            previous_equity = (
                float(previous["equity"]) if previous is not None else initial_equity
            )
            equity = previous_equity * (1 + daily_return_pct / 100.0)
            if not math.isfinite(equity):
                raise ValueError("intraday paper equity must be finite")
            connection.execute(
                """
                INSERT INTO intraday_paper_days(
                    strategy_id, version, symbol, trading_date,
                    opening_range_minutes, daily_return_pct, equity,
                    api_healthy, costs_complete, critical_incidents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    version,
                    symbol,
                    trading_date.isoformat(),
                    opening_range_minutes,
                    daily_return_pct,
                    equity,
                    int(api_healthy),
                    int(costs_complete),
                    critical_incidents,
                ),
            )
        return IntradayPaperDay(
            strategy_id,
            version,
            symbol,
            trading_date,
            opening_range_minutes,
            daily_return_pct,
            equity,
            api_healthy,
            costs_complete,
            critical_incidents,
        )

    def get_paper_day(
        self, strategy_id: str, version: str, symbol: str, trading_date: date
    ) -> IntradayPaperDay | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM intraday_paper_days
                WHERE strategy_id = ? AND version = ? AND symbol = ?
                  AND trading_date = ?
                """,
                (strategy_id, version, symbol, trading_date.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return _paper_day_from_row(row)

    def qualified_paper_days(
        self,
        strategy_id: str,
        version: str,
        *,
        as_of: date | None = None,
        trading_calendar: TradingCalendar | None = None,
    ) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT trading_date
                FROM intraday_paper_days
                WHERE strategy_id = ? AND version = ?
                GROUP BY trading_date
                HAVING MIN(api_healthy) = 1
                   AND MIN(costs_complete) = 1
                   AND SUM(critical_incidents) = 0
                   AND MIN(CASE WHEN typeof(daily_return_pct) IN ('integer', 'real')
                                AND daily_return_pct IS NOT NULL
                                AND abs(daily_return_pct) <= 1.7976931348623157e308
                                AND typeof(equity) IN ('integer', 'real')
                                AND equity IS NOT NULL
                                AND abs(equity) <= 1.7976931348623157e308
                                THEN 1 ELSE 0 END) = 1
                """,
                (strategy_id, version),
            ).fetchall()
        eligible = {
            date.fromisoformat(str(row["trading_date"])) for row in rows
        }
        if as_of is not None:
            eligible = {day for day in eligible if day <= as_of}
        if trading_calendar is not None:
            eligible = {
                day for day in eligible if trading_calendar.session_for(day) is not None
            }
        return len(eligible)

    def list_latest_paper_days(
        self, strategy_id: str, version: str
    ) -> tuple[IntradayPaperDay, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT day.* FROM intraday_paper_days AS day
                INNER JOIN (
                    SELECT symbol, MAX(trading_date) AS trading_date
                    FROM intraday_paper_days
                    WHERE strategy_id = ? AND version = ?
                    GROUP BY symbol
                ) AS latest
                  ON day.symbol = latest.symbol
                 AND day.trading_date = latest.trading_date
                WHERE day.strategy_id = ? AND day.version = ?
                ORDER BY day.symbol
                """,
                (strategy_id, version, strategy_id, version),
            ).fetchall()
        return tuple(_paper_day_from_row(row) for row in rows)


def _paper_day_from_row(row: sqlite3.Row) -> IntradayPaperDay:
    return IntradayPaperDay(
        strategy_id=str(row["strategy_id"]),
        version=str(row["version"]),
        symbol=str(row["symbol"]),
        trading_date=date.fromisoformat(str(row["trading_date"])),
        opening_range_minutes=int(row["opening_range_minutes"]),
        daily_return_pct=float(row["daily_return_pct"]),
        equity=float(row["equity"]),
        api_healthy=bool(row["api_healthy"]),
        costs_complete=bool(row["costs_complete"]),
        critical_incidents=int(row["critical_incidents"]),
    )
