from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
from typing import Callable, Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .strategies import StrategyRecord


@dataclass(frozen=True)
class SourceFingerprint:
    strategy_id: str
    version: str
    source_url: str
    content_sha256: str
    checked_at: datetime


@dataclass(frozen=True)
class SourceScanResult:
    changed: tuple[StrategyRecord, ...]
    unchanged: tuple[StrategyRecord, ...]
    failures: tuple[str, ...]

    @property
    def successful(self) -> tuple[StrategyRecord, ...]:
        return self.changed + self.unchanged


SourceFetcher = Callable[[str], bytes]


def fetch_source_bytes(url: str, *, max_bytes: int = 2_000_000) -> bytes:
    """Fetch a bounded primary-source document without involving an LLM."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("research source URL must use HTTPS")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("local research source URLs are prohibited")
    request = Request(
        url,
        headers={"User-Agent": "StockGuideSourceMonitor/1.0"},
        method="GET",
    )
    with urlopen(request, timeout=15) as response:
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("research source exceeds the configured byte limit")
    if not payload:
        raise ValueError("research source returned an empty document")
    return payload


class SQLiteSourceFingerprintStore:
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
                CREATE TABLE IF NOT EXISTS strategy_source_fingerprints (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id, version)
                )
                """
            )

    def get(self, strategy_id: str, version: str) -> SourceFingerprint | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_url, content_sha256, checked_at
                FROM strategy_source_fingerprints
                WHERE strategy_id = ? AND version = ?
                """,
                (strategy_id, version),
            ).fetchone()
        if row is None:
            return None
        return SourceFingerprint(
            strategy_id,
            version,
            str(row["source_url"]),
            str(row["content_sha256"]),
            datetime.fromisoformat(str(row["checked_at"])),
        )

    def put(self, value: SourceFingerprint) -> None:
        if value.checked_at.tzinfo is None or value.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_source_fingerprints(
                    strategy_id, version, source_url, content_sha256, checked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, version) DO UPDATE SET
                    source_url=excluded.source_url,
                    content_sha256=excluded.content_sha256,
                    checked_at=excluded.checked_at
                """,
                (
                    value.strategy_id,
                    value.version,
                    value.source_url,
                    value.content_sha256,
                    value.checked_at.isoformat(),
                ),
            )


class StrategySourceMonitor:
    def __init__(
        self,
        store: SQLiteSourceFingerprintStore,
        *,
        fetcher: SourceFetcher = fetch_source_bytes,
    ) -> None:
        self.store = store
        self.fetcher = fetcher

    def scan(
        self, records: list[StrategyRecord], *, now: datetime
    ) -> SourceScanResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        changed: list[StrategyRecord] = []
        unchanged: list[StrategyRecord] = []
        failures: list[str] = []
        for record in records:
            definition = record.definition
            try:
                payload = self.fetcher(definition.source.url)
                digest = hashlib.sha256(payload).hexdigest()
                previous = self.store.get(
                    definition.strategy_id, definition.version
                )
                fingerprint = SourceFingerprint(
                    definition.strategy_id,
                    definition.version,
                    definition.source.url,
                    digest,
                    now,
                )
                self.store.put(fingerprint)
                if (
                    previous is None
                    or previous.content_sha256 != digest
                    or previous.source_url != definition.source.url
                ):
                    changed.append(record)
                else:
                    unchanged.append(record)
            except Exception as exc:
                failures.append(
                    f"{definition.strategy_id}: {type(exc).__name__}: {exc}"
                )
        return SourceScanResult(tuple(changed), tuple(unchanged), tuple(failures))
