from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .intent import Allocation, InvestmentIntent
from .brokers import BrokerProvider


KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class Holding:
    user_id: int
    stock_code: str
    quantity: int
    average_price: float
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if not self.stock_code or len(self.stock_code) > 20:
            raise ValueError("stock_code must be 1 to 20 characters")
        if self.quantity < 0 or self.average_price < 0:
            raise ValueError("quantity and average_price cannot be negative")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")


@dataclass(frozen=True)
class DailyGuideRecipient:
    user_id: int
    chat_id: int


class SQLiteUserStateStore:
    """Persist non-secret user intent and holdings with user-scoped keys."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
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
                CREATE TABLE IF NOT EXISTS user_intents (
                    user_id INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    user_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity >= 0),
                    average_price REAL NOT NULL CHECK(average_price >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, stock_code)
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, stock_code)
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    broker_provider TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_account_profiles (
                    user_id INTEGER PRIMARY KEY,
                    account_equity REAL NOT NULL CHECK(account_equity > 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_notification_channels (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    daily_guide_enabled INTEGER NOT NULL DEFAULT 0,
                    last_daily_guide_date TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_executions (
                    proposal_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    trading_date TEXT NOT NULL,
                    executed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_order_executions_user_date
                    ON order_executions(user_id, trading_date);
                """
            )

    def save_intent(
        self, user_id: int, intent: InvestmentIntent, *, updated_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, updated_at)
        payload = json.dumps(asdict(intent), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_intents(user_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (user_id, payload, updated_at.isoformat()),
            )

    def load_intent(
        self, user_id: int, *, as_of: datetime | None = None
    ) -> InvestmentIntent | None:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
            raise ValueError("intent as_of must be timezone-aware")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, updated_at FROM user_intents WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(str(row["payload_json"]))
        payload["allocations"] = [Allocation(**item) for item in payload["allocations"]]
        intent = InvestmentIntent(**payload)
        if as_of is not None and intent.valid_for_days is not None:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
            if as_of >= updated_at + timedelta(days=intent.valid_for_days):
                return None
        return intent

    def upsert_holding(self, holding: Holding) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO holdings(
                    user_id, stock_code, quantity, average_price, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, stock_code) DO UPDATE SET
                    quantity=excluded.quantity,
                    average_price=excluded.average_price,
                    updated_at=excluded.updated_at
                """,
                (
                    holding.user_id,
                    holding.stock_code,
                    holding.quantity,
                    holding.average_price,
                    holding.updated_at.isoformat(),
                ),
            )

    def delete_holding(self, user_id: int, stock_code: str) -> None:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM holdings WHERE user_id = ? AND stock_code = ?",
                (user_id, stock_code),
            )

    def list_holdings(self, user_id: int) -> list[Holding]:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, quantity, average_price, updated_at
                FROM holdings
                WHERE user_id = ?
                ORDER BY stock_code
                """,
                (user_id,),
            ).fetchall()
        return [
            Holding(
                user_id=user_id,
                stock_code=str(row["stock_code"]),
                quantity=int(row["quantity"]),
                average_price=float(row["average_price"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            for row in rows
        ]

    def get_holding(self, user_id: int, stock_code: str) -> Holding | None:
        return next(
            (item for item in self.list_holdings(user_id) if item.stock_code == stock_code),
            None,
        )

    def list_distinct_symbols(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code FROM holdings
                UNION
                SELECT stock_code FROM watchlist
                ORDER BY stock_code
                """
            ).fetchall()
        return [str(row["stock_code"]) for row in rows]

    def list_users_with_guide_targets(self) -> list[int]:
        """Users that have an intent and at least one holding/watchlist symbol."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM user_intents
                WHERE user_id IN (
                    SELECT user_id FROM holdings
                    UNION
                    SELECT user_id FROM watchlist
                )
                ORDER BY user_id
                """
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def list_symbols_for_user(self, user_id: int) -> list[str]:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code FROM holdings WHERE user_id = ?
                UNION
                SELECT stock_code FROM watchlist WHERE user_id = ?
                ORDER BY stock_code
                """,
                (user_id, user_id),
            ).fetchall()
        return [str(row["stock_code"]) for row in rows]

    def add_watchlist_symbols(
        self, user_id: int, symbols: list[str], *, added_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, added_at)
        normalized = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        if not normalized or any(len(symbol) > 20 for symbol in normalized):
            raise ValueError("watchlist symbols must be 1 to 20 characters")
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO watchlist(user_id, stock_code, added_at)
                VALUES (?, ?, ?)
                """,
                [
                    (user_id, symbol, added_at.isoformat())
                    for symbol in sorted(normalized)
                ],
            )

    def replace_watchlist_symbols(
        self, user_id: int, symbols: list[str], *, added_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, added_at)
        normalized = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        if len(normalized) > 50 or any(not symbol or len(symbol) > 20 for symbol in normalized):
            raise ValueError("watchlist symbols must contain at most 50 symbols")
        with self._connect() as connection:
            connection.execute("DELETE FROM watchlist WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO watchlist(user_id, stock_code, added_at) VALUES (?, ?, ?)",
                [(user_id, symbol, added_at.isoformat()) for symbol in sorted(normalized)],
            )

    def seed_mobile_demo_once(self, user_id: int, *, seeded_at: datetime) -> bool:
        """Insert demo-only examples once, in the same transaction as the marker."""
        _validate_user_and_time(user_id, seeded_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mobile_api_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    metadata_value TEXT NOT NULL
                )
                """
            )
            existing = connection.execute(
                "SELECT metadata_value FROM mobile_api_metadata WHERE metadata_key = 'demo_seeded'"
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                """
                INSERT OR IGNORE INTO user_account_profiles(user_id, account_equity, updated_at)
                VALUES (?, ?, ?)
                """,
                (user_id, 10_000_000, seeded_at.isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO holdings(user_id, stock_code, quantity, average_price, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, "005930", 10, 70_000, seeded_at.isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO watchlist(user_id, stock_code, added_at)
                VALUES (?, ?, ?)
                """,
                (user_id, "000660", seeded_at.isoformat()),
            )
            connection.execute(
                "INSERT INTO mobile_api_metadata(metadata_key, metadata_value) VALUES ('demo_seeded', '1')"
            )
        return True

    def list_watchlist_symbols(self, user_id: int) -> list[str]:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code FROM watchlist
                WHERE user_id = ? ORDER BY stock_code
                """,
                (user_id,),
            ).fetchall()
        return [str(row["stock_code"]) for row in rows]

    def list_candidate_symbols(self, user_id: int, current_symbol: str) -> list[str]:
        combined = set(self.list_watchlist_symbols(user_id))
        combined.update(item.stock_code for item in self.list_holdings(user_id))
        combined.discard(current_symbol.upper())
        return sorted(combined)

    def set_broker_provider(
        self,
        user_id: int,
        provider: BrokerProvider,
        *,
        updated_at: datetime,
    ) -> None:
        _validate_user_and_time(user_id, updated_at)
        if provider not in {"toss_invest", "mirae_asset"}:
            raise ValueError("unsupported broker provider")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences(user_id, broker_provider, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    broker_provider=excluded.broker_provider,
                    updated_at=excluded.updated_at
                """,
                (user_id, provider, updated_at.isoformat()),
            )

    def get_broker_provider(
        self, user_id: int, *, default: BrokerProvider = "toss_invest"
    ) -> BrokerProvider:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT broker_provider FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return default
        provider = str(row["broker_provider"])
        if provider not in {"toss_invest", "mirae_asset"}:
            raise ValueError("stored broker provider is invalid")
        return provider  # type: ignore[return-value]

    def record_order_execution(
        self,
        proposal_id: str,
        user_id: int,
        *,
        executed_at: datetime,
    ) -> None:
        _validate_user_and_time(user_id, executed_at)
        if not proposal_id:
            raise ValueError("proposal_id is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO order_executions(
                    proposal_id, user_id, trading_date, executed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    user_id,
                    executed_at.astimezone(KOREA_TIMEZONE).date().isoformat(),
                    executed_at.isoformat(),
                ),
            )

    def set_account_equity(
        self, user_id: int, account_equity: float, *, updated_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, updated_at)
        if account_equity <= 0:
            raise ValueError("account equity must be positive")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_account_profiles(user_id, account_equity, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    account_equity=excluded.account_equity,
                    updated_at=excluded.updated_at
                """,
                (user_id, account_equity, updated_at.isoformat()),
            )

    def get_account_equity(self, user_id: int) -> float | None:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_equity FROM user_account_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return float(row["account_equity"]) if row is not None else None

    def count_order_executions(self, user_id: int, trading_date: date) -> int:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM order_executions
                WHERE user_id = ? AND trading_date = ?
                """,
                (user_id, trading_date.isoformat()),
            ).fetchone()
        return int(row["count"])

    def count_all_order_executions(self, trading_date: date) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM order_executions
                WHERE trading_date = ?
                """,
                (trading_date.isoformat(),),
            ).fetchone()
        return int(row["count"])

    def upsert_user_channel(
        self, user_id: int, chat_id: int, *, updated_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, updated_at)
        if chat_id == 0:
            raise ValueError("chat_id cannot be zero")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_notification_channels(user_id, chat_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, chat_id, updated_at.isoformat()),
            )

    def set_daily_guide_enabled(
        self, user_id: int, enabled: bool, *, updated_at: datetime
    ) -> None:
        _validate_user_and_time(user_id, updated_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_notification_channels
                SET daily_guide_enabled = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (int(enabled), updated_at.isoformat(), user_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("user notification channel is not registered")

    def list_due_daily_guide_recipients(
        self, trading_date: date
    ) -> tuple[DailyGuideRecipient, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, chat_id FROM user_notification_channels
                WHERE daily_guide_enabled = 1
                  AND (last_daily_guide_date IS NULL OR last_daily_guide_date != ?)
                ORDER BY user_id
                """,
                (trading_date.isoformat(),),
            ).fetchall()
        return tuple(
            DailyGuideRecipient(int(row["user_id"]), int(row["chat_id"]))
            for row in rows
        )

    def mark_daily_guide_sent(self, user_id: int, trading_date: date) -> None:
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_notification_channels SET last_daily_guide_date = ?
                WHERE user_id = ? AND daily_guide_enabled = 1
                """,
                (trading_date.isoformat(), user_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("daily guide recipient is not enabled")


def _validate_user_and_time(user_id: int, updated_at: datetime) -> None:
    if user_id <= 0:
        raise ValueError("user_id must be positive")
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError("updated_at must be timezone-aware")
