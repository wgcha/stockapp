from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


@dataclass(frozen=True)
class CachedPayload:
    source_id: str
    category: str
    cache_key: str
    payload: dict[str, Any]
    observed_at: datetime
    fetched_at: datetime


class SQLiteMarketCache:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_cache (
                    source_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, category, cache_key)
                )
                """
            )

    def put(self, value: CachedPayload) -> None:
        _validate_time(value.observed_at, "observed_at")
        _validate_time(value.fetched_at, "fetched_at")
        if value.fetched_at < value.observed_at:
            raise ValueError("fetched_at cannot precede observed_at")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_cache(
                    source_id, category, cache_key, payload_json,
                    observed_at, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, category, cache_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    observed_at=excluded.observed_at,
                    fetched_at=excluded.fetched_at
                """,
                (
                    value.source_id,
                    value.category,
                    value.cache_key,
                    json.dumps(value.payload, ensure_ascii=False, separators=(",", ":")),
                    value.observed_at.isoformat(),
                    value.fetched_at.isoformat(),
                ),
            )

    def get(
        self, source_id: str, category: str, cache_key: str
    ) -> CachedPayload | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json, observed_at, fetched_at
                FROM market_cache
                WHERE source_id = ? AND category = ? AND cache_key = ?
                """,
                (source_id, category, cache_key),
            ).fetchone()
        if row is None:
            return None
        return CachedPayload(
            source_id,
            category,
            cache_key,
            json.loads(str(row["payload_json"])),
            datetime.fromisoformat(str(row["observed_at"])),
            datetime.fromisoformat(str(row["fetched_at"])),
        )

    def list_category(self, category: str) -> list[CachedPayload]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, cache_key, payload_json, observed_at, fetched_at
                FROM market_cache WHERE category = ? ORDER BY cache_key
                """,
                (category,),
            ).fetchall()
        return [
            CachedPayload(
                str(row["source_id"]),
                category,
                str(row["cache_key"]),
                json.loads(str(row["payload_json"])),
                datetime.fromisoformat(str(row["observed_at"])),
                datetime.fromisoformat(str(row["fetched_at"])),
            )
            for row in rows
        ]


def _validate_time(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
