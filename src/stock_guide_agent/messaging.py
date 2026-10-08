"""Durable, privacy-minimising Telegram update and outbound-message state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Iterator


_BACKOFF_SECONDS = (5, 10, 20, 40, 80, 160, 300)
_MESSAGE_RETENTION = timedelta(hours=24)
_UPDATE_IDLE_RESET_AFTER = timedelta(days=7)
_MAX_PENDING_MESSAGES = 1_000
_MAX_PENDING_MESSAGES_PER_CHAT = 100


@dataclass(frozen=True)
class PendingMessage:
    """A message that the runtime may attempt to send once."""

    id: int
    chat_id: int
    text: str = field(repr=False)
    attempts: int


class SQLiteMessageStore:
    """Persist update de-duplication and a small, expiring outbound queue.

    Update payloads and failure text are deliberately never stored.  Connections are
    short-lived so a restarted process and a second accidental process share the
    same update claim state safely.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # This reduces recoverability of text deleted by normal queue operations.
        # SQLite/WAL copies and filesystem backups can still retain prior bytes.
        connection.execute("PRAGMA secure_delete = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS message_metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound_updates (
                    update_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN (
                        'processing', 'completed', 'failed', 'interrupted'
                    )),
                    claimed_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS outbound_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'sent', 'dead', 'expired'
                    )),
                    created_at TEXT NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbound_messages_due
                    ON outbound_messages(status, next_attempt_at, id);
                """
            )

    def claim_update(self, update_id: int, *, now: datetime) -> bool:
        """Atomically claim one Telegram update and advance its durable offset."""
        _validate_update_id(update_id)
        now_text = _utc_text(now, name="now")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT value FROM message_metadata WHERE key = 'next_offset'"
                ).fetchone()
                next_offset = None if row is None else int(row["value"])
                if next_offset is not None and update_id < next_offset:
                    connection.execute("ROLLBACK")
                    return False
                try:
                    connection.execute(
                        """
                        INSERT INTO inbound_updates(update_id, status, claimed_at)
                        VALUES (?, 'processing', ?)
                        """,
                        (update_id, now_text),
                    )
                except sqlite3.IntegrityError:
                    connection.execute("ROLLBACK")
                    return False
                new_offset = max(next_offset or 0, update_id + 1)
                connection.execute(
                    """
                    INSERT INTO message_metadata(key, value) VALUES ('next_offset', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (new_offset,),
                )
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def finish_update(self, update_id: int, *, status: str, now: datetime) -> None:
        _validate_update_id(update_id)
        if status not in {"completed", "failed"}:
            raise ValueError("status must be 'completed' or 'failed'")
        now_text = _utc_text(now, name="now")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE inbound_updates
                SET status = ?, finished_at = ?
                WHERE update_id = ?
                """,
                (status, now_text, update_id),
            )

    def get_next_offset(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM message_metadata WHERE key = 'next_offset'"
            ).fetchone()
        return None if row is None else int(row["value"])

    def recover_interrupted(self, *, now: datetime) -> tuple[int, ...]:
        """Mark in-flight work interrupted; callers must not auto-run it again."""
        now_text = _utc_text(now, name="now")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT update_id FROM inbound_updates WHERE status = 'processing' ORDER BY update_id"
                ).fetchall()
                ids = tuple(int(row["update_id"]) for row in rows)
                if ids:
                    connection.execute(
                        """
                        UPDATE inbound_updates
                        SET status = 'interrupted', finished_at = ?
                        WHERE status = 'processing'
                        """,
                        (now_text,),
                    )
                connection.execute("COMMIT")
                return ids
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def reset_after_idle(self, *, now: datetime) -> bool:
        """Forget stale Telegram update IDs after a seven-day idle period.

        Telegram does not retain unreceived updates for more than 24 hours, so an
        update cannot reappear after this reset.  This permits the API's possible
        lower update ID after a long idle period without losing the new update.
        """
        now_text = _utc_text(now, name="now")
        cutoff_text = (now - _UPDATE_IDLE_RESET_AFTER).astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM inbound_updates WHERE status = 'processing' LIMIT 1"
                ).fetchone() is not None:
                    connection.execute("ROLLBACK")
                    return False
                row = connection.execute(
                    "SELECT MAX(claimed_at) AS latest_claimed_at FROM inbound_updates"
                ).fetchone()
                if row is None or row["latest_claimed_at"] is None or str(row["latest_claimed_at"]) > cutoff_text:
                    connection.execute("ROLLBACK")
                    return False
                connection.execute("DELETE FROM inbound_updates")
                connection.execute(
                    "DELETE FROM message_metadata WHERE key = 'next_offset'"
                )
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def enqueue_message(self, chat_id: int, text: str, *, now: datetime) -> int:
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
            raise ValueError("chat_id must be a positive integer")
        if not isinstance(text, str) or not text:
            raise ValueError("text must be a non-empty string")
        now_text = _utc_text(now, name="now")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._expire_pending(connection, now_text=now_text, now=now)
                total_pending = int(connection.execute(
                    "SELECT COUNT(*) FROM outbound_messages WHERE status = 'pending'"
                ).fetchone()[0])
                chat_pending = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM outbound_messages
                    WHERE status = 'pending' AND chat_id = ?
                    """,
                    (chat_id,),
                ).fetchone()[0])
                if total_pending >= _MAX_PENDING_MESSAGES:
                    raise ValueError("pending message queue is full")
                if chat_pending >= _MAX_PENDING_MESSAGES_PER_CHAT:
                    raise ValueError("pending message queue is full for this chat")
                cursor = connection.execute(
                    """
                    INSERT INTO outbound_messages(
                        chat_id, text, attempts, status, created_at, next_attempt_at
                    ) VALUES (?, ?, 0, 'pending', ?, ?)
                    """,
                    (chat_id, text, now_text, now_text),
                )
                connection.execute("COMMIT")
                return int(cursor.lastrowid)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def due_messages(self, *, now: datetime, limit: int = 20) -> list[PendingMessage]:
        now_text = _utc_text(now, name="now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._expire_pending(connection, now_text=now_text, now=now)
                rows = connection.execute(
                    """
                    SELECT id, chat_id, text, attempts FROM outbound_messages
                    WHERE status = 'pending' AND text IS NOT NULL AND next_attempt_at <= ?
                    ORDER BY id LIMIT ?
                    """,
                    (now_text, limit),
                ).fetchall()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return [
            PendingMessage(
                id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                text=str(row["text"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    @staticmethod
    def _expire_pending(
        connection: sqlite3.Connection, *, now_text: str, now: datetime
    ) -> None:
        cutoff_text = (now - _MESSAGE_RETENTION).astimezone(timezone.utc).isoformat()
        connection.execute(
            """
            UPDATE outbound_messages
            SET status = 'expired', text = NULL, completed_at = ?
            WHERE status = 'pending' AND created_at <= ?
            """,
            (now_text, cutoff_text),
        )

    def mark_sent(self, message_id: int) -> None:
        _validate_message_id(message_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outbound_messages
                SET status = 'sent', text = NULL, completed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (message_id,),
            )

    def mark_send_failed(self, message_id: int, *, now: datetime) -> None:
        _validate_message_id(message_id)
        now_text = _utc_text(now, name="now")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT attempts FROM outbound_messages WHERE id = ? AND status = 'pending'",
                    (message_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return
                attempts = int(row["attempts"]) + 1
                if attempts >= 5:
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET attempts = ?, status = 'dead', text = NULL, completed_at = ?
                        WHERE id = ?
                        """,
                        (attempts, now_text, message_id),
                    )
                else:
                    delay = _BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
                    next_attempt = (now + timedelta(seconds=delay)).astimezone(timezone.utc)
                    connection.execute(
                        """
                        UPDATE outbound_messages
                        SET attempts = ?, next_attempt_at = ?
                        WHERE id = ?
                        """,
                        (attempts, next_attempt.isoformat(), message_id),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise


def _utc_text(value: datetime, *, name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return value.astimezone(timezone.utc).isoformat()


def _validate_update_id(update_id: int) -> None:
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise ValueError("update_id must be a non-negative integer")


def _validate_message_id(message_id: int) -> None:
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("message_id must be a positive integer")
