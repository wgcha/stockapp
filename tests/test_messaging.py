import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_guide_agent.messaging import SQLiteMessageStore


NOW = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)


class SQLiteMessageStoreTests(unittest.TestCase):
    def test_next_offset_persists_and_rejects_old_or_duplicate_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            self.assertIsNone(store.get_next_offset())
            self.assertTrue(store.claim_update(40, now=NOW))
            self.assertEqual(store.get_next_offset(), 41)
            reopened = SQLiteMessageStore(path)
            self.assertEqual(reopened.get_next_offset(), 41)
            self.assertFalse(reopened.claim_update(40, now=NOW))
            self.assertFalse(reopened.claim_update(39, now=NOW))
            self.assertTrue(reopened.claim_update(45, now=NOW))
            self.assertEqual(reopened.get_next_offset(), 46)

    def test_two_store_instances_cannot_claim_the_same_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            first = SQLiteMessageStore(path)
            second = SQLiteMessageStore(path)
            self.assertTrue(first.claim_update(200, now=NOW))
            self.assertFalse(second.claim_update(200, now=NOW))

    def test_processing_updates_become_interrupted_without_reclaiming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            self.assertTrue(store.claim_update(10, now=NOW))
            self.assertTrue(store.claim_update(12, now=NOW))
            store.finish_update(12, status="completed", now=NOW)
            self.assertEqual(store.recover_interrupted(now=NOW + timedelta(minutes=1)), (10,))
            self.assertEqual(store.recover_interrupted(now=NOW + timedelta(minutes=2)), ())
            self.assertFalse(store.claim_update(10, now=NOW))

    def test_idle_reset_at_seven_day_boundary_allows_a_lower_new_update_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            self.assertTrue(store.claim_update(500, now=NOW))
            store.finish_update(500, status="completed", now=NOW)
            self.assertTrue(store.reset_after_idle(now=NOW + timedelta(days=7)))
            self.assertIsNone(store.get_next_offset())
            self.assertTrue(store.claim_update(3, now=NOW + timedelta(days=7)))

    def test_idle_reset_keeps_recent_claims_and_processing_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            self.assertTrue(store.claim_update(500, now=NOW))
            store.finish_update(500, status="completed", now=NOW)
            self.assertFalse(store.reset_after_idle(now=NOW + timedelta(days=6, hours=23)))
            self.assertEqual(store.get_next_offset(), 501)
            self.assertFalse(store.claim_update(3, now=NOW + timedelta(days=6, hours=23)))
            self.assertTrue(store.claim_update(700, now=NOW + timedelta(days=7)))
            self.assertFalse(store.reset_after_idle(now=NOW + timedelta(days=14)))

    def test_outbox_backoff_survives_restart_and_deletes_text_when_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            message_id = store.enqueue_message(123, "guide", now=NOW)
            self.assertEqual(store.due_messages(now=NOW)[0].attempts, 0)
            store.mark_send_failed(message_id, now=NOW)
            self.assertEqual(store.due_messages(now=NOW), [])
            reopened = SQLiteMessageStore(path)
            due = reopened.due_messages(now=NOW + timedelta(seconds=5))
            self.assertEqual([(item.id, item.chat_id, item.text, item.attempts) for item in due], [(message_id, 123, "guide", 1)])
            reopened.mark_sent(message_id)
            self.assertEqual(reopened.due_messages(now=NOW + timedelta(days=1)), [])
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT text FROM outbound_messages WHERE id = ?", (message_id,)).fetchone()[0], None)
            finally:
                connection.close()

    def test_outbox_becomes_dead_and_drops_text_after_fifth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            message_id = store.enqueue_message(123, "guide", now=NOW)
            moment = NOW
            for _ in range(5):
                store.mark_send_failed(message_id, now=moment)
                moment += timedelta(minutes=1)
            self.assertEqual(store.due_messages(now=moment), [])
            connection = sqlite3.connect(path)
            try:
                status, text, attempts = connection.execute(
                    "SELECT status, text, attempts FROM outbound_messages WHERE id = ?", (message_id,)
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual((status, text, attempts), ("dead", None, 5))

    def test_pending_message_expires_after_24_hours_and_drops_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            message_id = store.enqueue_message(123, "old guide", now=NOW)
            self.assertEqual(store.due_messages(now=NOW + timedelta(hours=24)), [])
            connection = sqlite3.connect(path)
            try:
                status, text = connection.execute(
                    "SELECT status, text FROM outbound_messages WHERE id = ?", (message_id,)
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual((status, text), ("expired", None))

    def test_pending_queue_is_bounded_per_chat_and_expiry_recovers_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            for index in range(100):
                store.enqueue_message(123, f"guide {index}", now=NOW)
            with self.assertRaisesRegex(ValueError, "full for this chat"):
                store.enqueue_message(123, "too many", now=NOW)
            recovered_id = store.enqueue_message(
                123, "fresh", now=NOW + timedelta(hours=24)
            )
            self.assertEqual(
                store.due_messages(now=NOW + timedelta(hours=24))[0].id,
                recovered_id,
            )

    def test_pending_queue_has_a_total_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            with patch("stock_guide_agent.messaging._MAX_PENDING_MESSAGES", 2):
                store.enqueue_message(1, "one", now=NOW)
                store.enqueue_message(2, "two", now=NOW)
                with self.assertRaisesRegex(ValueError, "queue is full"):
                    store.enqueue_message(3, "three", now=NOW)

    def test_pending_message_repr_hides_text_and_chat_id_must_be_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMessageStore(Path(directory) / "messages.sqlite3")
            message_id = store.enqueue_message(123, "sensitive guide", now=NOW)
            message = store.due_messages(now=NOW)[0]
            self.assertEqual(message.id, message_id)
            self.assertNotIn("sensitive guide", repr(message))
            for invalid_chat_id in (True, 0, -1):
                with self.assertRaises(ValueError):
                    store.enqueue_message(invalid_chat_id, "x", now=NOW)

    def test_connections_enable_sqlite_secure_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            # The same connection setup is used by every storage operation.
            with store._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_requires_utc_aware_timestamps_and_never_stores_failure_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            store = SQLiteMessageStore(path)
            with self.assertRaises(ValueError):
                store.claim_update(1, now=datetime(2026, 9, 10, 1, 0))
            with self.assertRaises(ValueError):
                store.enqueue_message(1, "x", now=NOW.astimezone(timezone(timedelta(hours=9))))
            self.assertTrue(store.claim_update(1, now=NOW))
            store.finish_update(1, status="failed", now=NOW)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    [item[1] for item in connection.execute("PRAGMA table_info(inbound_updates)")],
                    ["update_id", "status", "claimed_at", "finished_at"],
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
