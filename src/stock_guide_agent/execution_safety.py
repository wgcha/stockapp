"""Durable owner-controlled execution safety state."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


class SQLiteExecutionSafetyStore:
    """Persist the kill switch in the user's existing backed-up database."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS execution_safety_state (
                        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                        kill_switch_active INTEGER NOT NULL
                            CHECK(kill_switch_active IN (0, 1)),
                        kill_switch_reason TEXT,
                        CHECK(
                            (kill_switch_active = 1 AND kill_switch_reason IS NOT NULL)
                            OR (kill_switch_active = 0 AND kill_switch_reason IS NULL)
                        )
                    )
                    """
                )
        except sqlite3.Error:
            raise RuntimeError("execution safety store is unavailable") from None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load(self) -> tuple[bool, str | None]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT singleton_id, kill_switch_active, kill_switch_reason
                    FROM execution_safety_state
                    """
                ).fetchall()
        except sqlite3.Error:
            raise RuntimeError("execution safety state is unavailable") from None
        if not rows:
            return False, None
        if len(rows) != 1:
            raise RuntimeError("execution safety state is invalid")
        row = rows[0]
        active = row["kill_switch_active"]
        reason = row["kill_switch_reason"]
        if (
            row["singleton_id"] != 1
            or type(active) is not int
            or active not in (0, 1)
            or (active == 1 and (not isinstance(reason, str) or not reason))
            or (active == 0 and reason is not None)
        ):
            raise RuntimeError("execution safety state is invalid")
        return bool(active), reason

    def save(self, active: bool, reason: str | None) -> None:
        if type(active) is not bool or (
            (active and (not isinstance(reason, str) or not reason))
            or (not active and reason is not None)
        ):
            raise ValueError("execution safety state is invalid")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO execution_safety_state(
                        singleton_id, kill_switch_active, kill_switch_reason
                    ) VALUES (1, ?, ?)
                    ON CONFLICT(singleton_id) DO UPDATE SET
                        kill_switch_active = excluded.kill_switch_active,
                        kill_switch_reason = excluded.kill_switch_reason
                    """,
                    (int(active), reason),
                )
        except sqlite3.Error:
            raise RuntimeError("execution safety state could not be saved") from None
