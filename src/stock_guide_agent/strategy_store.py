from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .strategies import (
    PUBLIC_STRATEGY_CATALOG,
    PromotionDecision,
    StrategyDefinition,
    StrategyRecord,
    StrategyStatus,
    ValidationReport,
    apply_report,
    evaluate_for_promotion,
)


class StrategyNotFound(KeyError):
    pass


@dataclass(frozen=True)
class StrategyActivationEvent:
    strategy_id: str
    version: str
    action: str
    actor_user_id: int | None
    occurred_at: datetime


class SQLiteStrategyStore:
    """Persist validation evidence; research metadata alone never promotes a strategy."""

    def __init__(
        self,
        path: str | Path,
        *,
        definitions: tuple[StrategyDefinition, ...] = PUBLIC_STRATEGY_CATALOG,
        read_only: bool = False,
    ) -> None:
        self.path = str(path)
        self.read_only = read_only
        self._connect_path = (
            Path(path).resolve().as_uri() + "?mode=ro" if read_only else self.path
        )
        self.definitions = {
            (item.strategy_id, item.version): item for item in definitions
        }
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
                CREATE TABLE IF NOT EXISTS strategy_records (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_researched_on TEXT,
                    next_research_due TEXT,
                    PRIMARY KEY(strategy_id, version)
                );
                CREATE TABLE IF NOT EXISTS validation_reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    checked_on TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_activation_audit (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_user_id INTEGER,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            for strategy_id, version in self.definitions:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO strategy_records(
                        strategy_id, version, status
                    ) VALUES (?, ?, 'research')
                    """,
                    (strategy_id, version),
                )

    def get_record(self, strategy_id: str, version: str) -> StrategyRecord:
        definition = self.definitions.get((strategy_id, version))
        if definition is None:
            raise StrategyNotFound((strategy_id, version))
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, last_researched_on, next_research_due
                FROM strategy_records
                WHERE strategy_id = ? AND version = ?
                """,
                (strategy_id, version),
            ).fetchone()
            report_rows = connection.execute(
                """
                SELECT payload_json FROM validation_reports
                WHERE strategy_id = ? AND version = ?
                ORDER BY report_id
                """,
                (strategy_id, version),
            ).fetchall()
        if row is None:
            raise StrategyNotFound((strategy_id, version))
        reports = [
            _validation_report_from_json(str(item["payload_json"]))
            for item in report_rows
        ]
        status = str(row["status"])
        if status == "approved" and (
            not reports
            or not evaluate_for_promotion(definition, reports[-1]).eligible
        ):
            status = "suspended"
        return StrategyRecord(
            definition=definition,
            status=status,  # type: ignore[arg-type]
            reports=reports,
            last_researched_on=(
                date.fromisoformat(str(row["last_researched_on"]))
                if row["last_researched_on"]
                else None
            ),
            next_research_due=(
                date.fromisoformat(str(row["next_research_due"]))
                if row["next_research_due"]
                else None
            ),
        )

    def list_records(self) -> list[StrategyRecord]:
        return [
            self.get_record(strategy_id, version)
            for strategy_id, version in sorted(self.definitions)
        ]

    def due_for_research(self, today: date) -> list[StrategyRecord]:
        return [record for record in self.list_records() if record.research_due(today)]

    def mark_researched(
        self,
        strategy_id: str,
        version: str,
        *,
        checked_on: date,
        cadence_days: int = 90,
    ) -> StrategyRecord:
        record = self.get_record(strategy_id, version)
        record.schedule_next_research(checked_on, cadence_days)
        self._save_record(record)
        return record

    def apply_validation(
        self,
        report: ValidationReport,
        *,
        checked_on: date,
    ) -> PromotionDecision:
        record = self.get_record(report.strategy_id, report.version)
        prior_status = record.status
        decision = apply_report(record, report)
        persisted_status: StrategyStatus = (
            "approved"
            if decision.eligible and prior_status == "approved"
            else decision.status
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO validation_reports(
                    strategy_id, version, payload_json, checked_on
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    report.strategy_id,
                    report.version,
                    json.dumps(asdict(report), separators=(",", ":")),
                    checked_on.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE strategy_records SET status = ?
                WHERE strategy_id = ? AND version = ?
                """,
                (persisted_status, report.strategy_id, report.version),
            )
        return decision

    def approve(
        self,
        strategy_id: str,
        version: str,
        *,
        approved_by: int,
        approved_at: datetime,
    ) -> StrategyRecord:
        if approved_by <= 0:
            raise ValueError("strategy approver must be a positive user id")
        if approved_at.tzinfo is None or approved_at.utcoffset() is None:
            raise ValueError("strategy approval time must be timezone-aware")
        record = self.get_record(strategy_id, version)
        if record.status == "approved":
            return record
        if record.status != "validated" or not record.reports:
            raise ValueError("strategy has not passed all validation gates")
        if not evaluate_for_promotion(record.definition, record.reports[-1]).eligible:
            raise ValueError("latest strategy validation is not eligible")
        record.status = "approved"
        self._save_record(record)
        self._record_activation_event(
            strategy_id,
            version,
            "approved",
            approved_by,
            approved_at,
        )
        return record

    def is_approved(self, strategy_id: str, version: str) -> bool:
        return self.get_record(strategy_id, version).status == "approved"

    def latest_validation_on(self, strategy_id: str, version: str) -> date | None:
        self.get_record(strategy_id, version)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(checked_on) AS checked_on FROM validation_reports
                WHERE strategy_id = ? AND version = ?
                """,
                (strategy_id, version),
            ).fetchone()
        return date.fromisoformat(str(row["checked_on"])) if row["checked_on"] else None

    def suspend(
        self,
        strategy_id: str,
        version: str,
        *,
        actor_user_id: int | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        if occurred_at is not None and (
            occurred_at.tzinfo is None or occurred_at.utcoffset() is None
        ):
            raise ValueError("strategy suspension time must be timezone-aware")
        record = self.get_record(strategy_id, version)
        record.status = "suspended"
        self._save_record(record)
        if occurred_at is not None:
            self._record_activation_event(
                strategy_id,
                version,
                "suspended",
                actor_user_id,
                occurred_at,
            )

    def list_activation_events(
        self, strategy_id: str, version: str
    ) -> tuple[StrategyActivationEvent, ...]:
        self.get_record(strategy_id, version)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT strategy_id, version, action, actor_user_id, occurred_at
                FROM strategy_activation_audit
                WHERE strategy_id = ? AND version = ?
                ORDER BY event_id
                """,
                (strategy_id, version),
            ).fetchall()
        return tuple(
            StrategyActivationEvent(
                strategy_id=str(row["strategy_id"]),
                version=str(row["version"]),
                action=str(row["action"]),
                actor_user_id=(
                    int(row["actor_user_id"])
                    if row["actor_user_id"] is not None
                    else None
                ),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            )
            for row in rows
        )

    def _record_activation_event(
        self,
        strategy_id: str,
        version: str,
        action: str,
        actor_user_id: int | None,
        occurred_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_activation_audit(
                    strategy_id, version, action, actor_user_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    version,
                    action,
                    actor_user_id,
                    occurred_at.isoformat(),
                ),
            )

    def _save_record(self, record: StrategyRecord) -> None:
        status: StrategyStatus = record.status
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE strategy_records SET
                    status = ?, last_researched_on = ?, next_research_due = ?
                WHERE strategy_id = ? AND version = ?
                """,
                (
                    status,
                    record.last_researched_on.isoformat()
                    if record.last_researched_on
                    else None,
                    record.next_research_due.isoformat()
                    if record.next_research_due
                    else None,
                    record.definition.strategy_id,
                    record.definition.version,
                ),
            )


def _validation_report_from_json(raw: str) -> ValidationReport:
    payload = json.loads(raw)
    for name in ("cost_source_urls", "cost_assumptions"):
        if name in payload:
            payload[name] = tuple(payload[name])
    return ValidationReport(**payload)
