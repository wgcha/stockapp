import sqlite3
import tempfile
import unittest
from pathlib import Path

from stock_guide_agent.backup import backup_databases, restore_databases
from stock_guide_agent.execution import ExecutionState, OrderRejected
from stock_guide_agent.execution_safety import SQLiteExecutionSafetyStore


class ExecutionSafetyStoreTests(unittest.TestCase):
    def test_fresh_store_defaults_inactive_and_restart_restores_kill_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.sqlite3"
            store = SQLiteExecutionSafetyStore(path)
            self.assertEqual(store.load(), (False, None))

            state = ExecutionState(_kill_switch_persist=store.save)
            state.activate_kill_switch("owner emergency stop")
            self.assertEqual(
                SQLiteExecutionSafetyStore(path).load(),
                (True, "owner emergency stop"),
            )

            restarted_store = SQLiteExecutionSafetyStore(path)
            active, reason = restarted_store.load()
            restarted = ExecutionState(
                kill_switch_active=active,
                kill_switch_reason=reason,
                _kill_switch_persist=restarted_store.save,
            )
            with self.assertRaises(OrderRejected):
                restarted.reset_kill_switch("reset")
            self.assertTrue(restarted.kill_switch_active)
            restarted.reset_kill_switch("RESET KILL SWITCH")
            self.assertEqual(SQLiteExecutionSafetyStore(path).load(), (False, None))

    def test_activate_write_failure_keeps_memory_blocked_and_hides_error(self):
        def fail_write(active, reason):
            raise OSError("SECRET database path")

        state = ExecutionState(_kill_switch_persist=fail_write)
        with self.assertRaisesRegex(RuntimeError, "could not be persisted safely") as caught:
            state.activate_kill_switch("sensitive reason")
        self.assertTrue(state.kill_switch_active)
        self.assertEqual(state.kill_switch_reason, "sensitive reason")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_reset_write_failure_leaves_existing_switch_active(self):
        def fail_write(active, reason):
            raise OSError("SECRET database path")

        state = ExecutionState(
            kill_switch_active=True,
            kill_switch_reason="sensitive reason",
            _kill_switch_persist=fail_write,
        )
        with self.assertRaisesRegex(RuntimeError, "could not be reset safely") as caught:
            state.reset_kill_switch("RESET KILL SWITCH")
        self.assertTrue(state.kill_switch_active)
        self.assertEqual(state.kill_switch_reason, "sensitive reason")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_corrupt_stored_state_fails_closed_with_fixed_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE execution_safety_state (
                        singleton_id INTEGER,
                        kill_switch_active,
                        kill_switch_reason
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO execution_safety_state VALUES (1, 9, 'SECRET')"
                )
                connection.commit()
            finally:
                connection.close()
            store = SQLiteExecutionSafetyStore(path)
            with self.assertRaisesRegex(RuntimeError, "execution safety state is invalid") as caught:
                store.load()
            self.assertNotIn("SECRET", str(caught.exception))

    def test_users_database_backup_and_restore_preserves_active_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            SQLiteExecutionSafetyStore(data / "users.sqlite3").save(
                True, "owner emergency stop"
            )
            backup = root / "backup"
            backup_databases(data, backup)
            restored = root / "restored"
            restore_databases(backup, restored)
            self.assertEqual(
                SQLiteExecutionSafetyStore(restored / "users.sqlite3").load(),
                (True, "owner emergency stop"),
            )


if __name__ == "__main__":
    unittest.main()
