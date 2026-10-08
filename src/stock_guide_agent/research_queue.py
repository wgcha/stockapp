from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Iterator, Literal

from .strategies import StrategyRecord
from .academic_discovery import AcademicStrategyCandidate


ResearchStatus = Literal["pending", "in_progress", "completed", "failed"]


@dataclass(frozen=True)
class ResearchTask:
    task_id: int
    strategy_id: str
    version: str
    source_url: str
    status: ResearchStatus
    created_at: datetime


@dataclass(frozen=True)
class AcademicCandidateTask:
    doi: str
    title: str
    published_on: str
    landing_url: str
    authors: str
    matched_terms: str
    status: ResearchStatus
    created_at: datetime


class SQLiteResearchQueue:
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
                CREATE TABLE IF NOT EXISTS research_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(strategy_id, version, status)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS academic_candidate_tasks (
                    doi TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    published_on TEXT NOT NULL,
                    landing_url TEXT NOT NULL,
                    authors TEXT NOT NULL,
                    matched_terms TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    review_notes TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS research_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )

    def enqueue_due(self, records: list[StrategyRecord], *, now: datetime) -> int:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        inserted = 0
        with self._connect() as connection:
            for record in records:
                try:
                    connection.execute(
                        """
                        INSERT INTO research_tasks(
                            strategy_id, version, source_url, status, created_at
                        ) VALUES (?, ?, ?, 'pending', ?)
                        """,
                        (
                            record.definition.strategy_id,
                            record.definition.version,
                            record.definition.source.url,
                            now.isoformat(),
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
        return inserted

    def list_pending(self) -> list[ResearchTask]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, strategy_id, version, source_url, status, created_at
                FROM research_tasks WHERE status = 'pending' ORDER BY task_id
                """
            ).fetchall()
        return [
            ResearchTask(
                int(row["task_id"]),
                str(row["strategy_id"]),
                str(row["version"]),
                str(row["source_url"]),
                str(row["status"]),  # type: ignore[arg-type]
                datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    def enqueue_academic_candidates(
        self,
        candidates: tuple[AcademicStrategyCandidate, ...],
        *,
        now: datetime,
    ) -> int:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        inserted = 0
        with self._connect() as connection:
            for candidate in candidates:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO academic_candidate_tasks(
                        doi, title, published_on, landing_url, authors,
                        matched_terms, source, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        candidate.doi.lower(),
                        candidate.title,
                        candidate.published_on.isoformat(),
                        candidate.landing_url,
                        " | ".join(candidate.authors),
                        " | ".join(candidate.matched_terms),
                        candidate.source,
                        now.isoformat(),
                    ),
                )
                inserted += int(cursor.rowcount == 1)
        return inserted

    def list_pending_academic_candidates(self) -> list[AcademicCandidateTask]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT doi, title, published_on, landing_url, authors,
                       matched_terms, status, created_at
                FROM academic_candidate_tasks
                WHERE status = 'pending'
                ORDER BY published_on DESC, doi
                """
            ).fetchall()
        return [
            AcademicCandidateTask(
                str(row["doi"]),
                str(row["title"]),
                str(row["published_on"]),
                str(row["landing_url"]),
                str(row["authors"]),
                str(row["matched_terms"]),
                str(row["status"]),  # type: ignore[arg-type]
                datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        ]

    def claim_next(self, *, now: datetime) -> ResearchTask | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, strategy_id, version, source_url, created_at
                FROM research_tasks WHERE status = 'pending'
                ORDER BY task_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE research_tasks SET status = 'in_progress' WHERE task_id = ?",
                (int(row["task_id"]),),
            )
        return ResearchTask(
            int(row["task_id"]),
            str(row["strategy_id"]),
            str(row["version"]),
            str(row["source_url"]),
            "in_progress",
            datetime.fromisoformat(str(row["created_at"])),
        )

    def complete(
        self,
        task_id: int,
        *,
        succeeded: bool,
        notes: str,
        now: datetime,
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not notes.strip():
            raise ValueError("research completion notes are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT strategy_id, version, source_url, status, created_at
                FROM research_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("research task does not exist")
            if str(row["status"]) != "in_progress":
                raise ValueError("research task must be claimed before completion")
            connection.execute(
                """
                INSERT INTO research_history(
                    strategy_id, version, source_url, outcome, notes,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["strategy_id"]),
                    str(row["version"]),
                    str(row["source_url"]),
                    "completed" if succeeded else "failed",
                    notes.strip(),
                    str(row["created_at"]),
                    now.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM research_tasks WHERE task_id = ?", (task_id,)
            )

    def history_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM research_history"
            ).fetchone()
        return int(row["count"])
