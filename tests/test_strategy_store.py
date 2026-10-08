import tempfile
import unittest
import json
import sqlite3
import math
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, ValidationReport
from stock_guide_agent.strategy_store import SQLiteStrategyStore


DEFINITION = PUBLIC_STRATEGY_CATALOG[0]


def report() -> ValidationReport:
    return ValidationReport(
        strategy_id=DEFINITION.strategy_id,
        version=DEFINITION.version,
        market="korean_equities",
        sample_years=8,
        out_of_sample=True,
        walk_forward_windows=6,
        trades=300,
        cost_adjusted_sharpe=0.8,
        max_drawdown=0.18,
        parameter_stability=0.75,
        paper_trading_days=60,
        includes_fees_taxes_slippage=True,
        data_leakage_check_passed=True,
        signal_engine="time_series_momentum",
    )


class StrategyStoreTests(unittest.TestCase):
    def test_legacy_approved_report_without_engine_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.sqlite3"
            store = SQLiteStrategyStore(path)
            legacy = report()
            payload = legacy.__dict__.copy()
            payload.pop("signal_engine")
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO validation_reports(
                        strategy_id, version, payload_json, checked_on
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        DEFINITION.strategy_id,
                        DEFINITION.version,
                        json.dumps(payload),
                        "2026-07-17",
                    ),
                )
                connection.execute(
                    """
                    UPDATE strategy_records SET status = 'approved'
                    WHERE strategy_id = ? AND version = ?
                    """,
                    (DEFINITION.strategy_id, DEFINITION.version),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = SQLiteStrategyStore(path)

            self.assertFalse(
                reopened.is_approved(DEFINITION.strategy_id, DEFINITION.version)
            )
            self.assertEqual(
                reopened.get_record(DEFINITION.strategy_id, DEFINITION.version).status,
                "suspended",
            )

    def test_persisted_non_finite_approval_is_suspended_and_cannot_be_reapproved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.sqlite3"
            SQLiteStrategyStore(path)
            payload = report().__dict__.copy()
            payload["cost_adjusted_sharpe"] = math.nan
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO validation_reports(
                        strategy_id, version, payload_json, checked_on
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        DEFINITION.strategy_id,
                        DEFINITION.version,
                        json.dumps(payload),
                        "2026-07-17",
                    ),
                )
                connection.execute(
                    """
                    UPDATE strategy_records SET status = 'approved'
                    WHERE strategy_id = ? AND version = ?
                    """,
                    (DEFINITION.strategy_id, DEFINITION.version),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = SQLiteStrategyStore(path)
            self.assertEqual(
                reopened.get_record(DEFINITION.strategy_id, DEFINITION.version).status,
                "suspended",
            )
            with self.assertRaisesRegex(ValueError, "validation gates"):
                reopened.approve(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    approved_by=900,
                    approved_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
                )

    def test_catalog_starts_unapproved_and_research_does_not_promote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStrategyStore(Path(directory) / "strategy.sqlite3")
            self.assertFalse(store.is_approved(DEFINITION.strategy_id, DEFINITION.version))

            stored = store.mark_researched(
                DEFINITION.strategy_id,
                DEFINITION.version,
                checked_on=date(2026, 7, 17),
            )

            self.assertEqual(stored.status, "research")
            self.assertEqual(stored.next_research_due, date(2026, 10, 15))
            self.assertFalse(store.is_approved(DEFINITION.strategy_id, DEFINITION.version))

    def test_passing_validation_waits_for_owner_then_persists_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.sqlite3"
            store = SQLiteStrategyStore(path)
            decision = store.apply_validation(report(), checked_on=date(2026, 7, 17))

            reopened = SQLiteStrategyStore(path)
            record = reopened.get_record(DEFINITION.strategy_id, DEFINITION.version)
            self.assertTrue(decision.eligible)
            self.assertEqual(record.status, "validated")
            self.assertEqual(len(record.reports), 1)
            self.assertFalse(reopened.is_approved(DEFINITION.strategy_id, DEFINITION.version))

            approved_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
            reopened.approve(
                DEFINITION.strategy_id,
                DEFINITION.version,
                approved_by=900,
                approved_at=approved_at,
            )
            final = SQLiteStrategyStore(path)
            self.assertTrue(final.is_approved(DEFINITION.strategy_id, DEFINITION.version))
            events = final.list_activation_events(
                DEFINITION.strategy_id, DEFINITION.version
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].actor_user_id, 900)
            self.assertEqual(events[0].occurred_at, approved_at)

    def test_owner_cannot_approve_before_all_validation_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStrategyStore(Path(directory) / "strategy.sqlite3")

            with self.assertRaisesRegex(ValueError, "validation gates"):
                store.approve(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    approved_by=900,
                    approved_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
                )

    def test_failed_revalidation_removes_approval_and_suspend_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.sqlite3"
            store = SQLiteStrategyStore(path)
            store.apply_validation(report(), checked_on=date(2026, 7, 17))
            store.approve(
                DEFINITION.strategy_id,
                DEFINITION.version,
                approved_by=900,
                approved_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            )
            failed = replace(report(), includes_fees_taxes_slippage=False)

            decision = store.apply_validation(failed, checked_on=date(2026, 8, 1))
            self.assertFalse(decision.eligible)
            self.assertFalse(store.is_approved(DEFINITION.strategy_id, DEFINITION.version))

            store.suspend(DEFINITION.strategy_id, DEFINITION.version)
            self.assertEqual(
                SQLiteStrategyStore(path)
                .get_record(DEFINITION.strategy_id, DEFINITION.version)
                .status,
                "suspended",
            )


if __name__ == "__main__":
    unittest.main()
