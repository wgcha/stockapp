from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
import hashlib
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_guide_agent.cli import main
from stock_guide_agent.intraday import SQLiteIntradayBarStore
from stock_guide_agent.local_strategy_report import (
    LocalStrategyReportError,
    read_local_strategy_report,
)
from stock_guide_agent.paper_trading import (
    PaperModelObservation,
    SQLitePaperTradingLedger,
)
from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, ValidationReport
from stock_guide_agent.strategy_store import SQLiteStrategyStore
from stock_guide_agent.trading_calendar import TradingSession


KST = ZoneInfo("Asia/Seoul")
DEFINITION = next(
    item for item in PUBLIC_STRATEGY_CATALOG if item.strategy_id == "time_series_momentum"
)


def passing_report(**changes) -> ValidationReport:
    values = {
        "strategy_id": DEFINITION.strategy_id,
        "version": DEFINITION.version,
        "market": "korean_equities",
        "sample_years": 8.0,
        "out_of_sample": True,
        "walk_forward_windows": 6,
        "trades": 300,
        "cost_adjusted_sharpe": 0.8,
        "max_drawdown": 0.18,
        "parameter_stability": 0.75,
        "paper_trading_days": 60,
        "includes_fees_taxes_slippage": True,
        "data_leakage_check_passed": True,
        "signal_engine": "time_series_momentum",
        "cost_source_urls": ("https://example.test/costs",),
        "cost_assumptions": ("slippage_5_bps_each_side",),
    }
    values.update(changes)
    return ValidationReport(**values)


def make_strategy_db(data_dir: Path, *, checked_on: date = date(2026, 10, 6)) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "strategies.sqlite3"
    store = SQLiteStrategyStore(path)
    store.apply_validation(passing_report(), checked_on=checked_on)
    return path


class SessionCalendar:
    def __init__(self, holidays: set[date] | None = None):
        self.holidays = holidays or set()

    def session_for(self, value):
        day = value.date() if isinstance(value, datetime) else value
        if day.weekday() >= 5 or day in self.holidays:
            return None
        opens = datetime.combine(day, datetime.min.time(), tzinfo=KST).replace(hour=9)
        closes = opens.replace(hour=15, minute=30)
        return TradingSession(day, opens, closes)

    def is_after_close_evaluation(self, now):
        return True

    def daily_guide_start(self, now, *, configured_hour, configured_minute):
        return None


class BrokenCalendar(SessionCalendar):
    def session_for(self, value):
        raise RuntimeError("SECRET_CALENDAR_DETAILS")


class LocalStrategyReportTests(unittest.TestCase):
    def test_read_only_report_uses_existing_snapshot_without_credentials_or_runtime(self):
        with tempfile.TemporaryDirectory(prefix="strategy report ") as folder:
            root = Path(folder)
            primary = make_strategy_db(root)
            before = hashlib.sha256(primary.read_bytes()).hexdigest()
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch(
                "sys.argv",
                ["agent", "--strategy-report", DEFINITION.strategy_id, DEFINITION.version, "--data-dir", folder],
            ), patch("stock_guide_agent.cli.build_runtime_from_env") as runtime, patch(
                "stock_guide_agent.cli.run_connectivity_diagnostic"
            ) as connection_check, patch(
                "stock_guide_agent.cli.run_market_connectivity"
            ) as market_check, redirect_stdout(output):
                main()
            runtime.assert_not_called()
            connection_check.assert_not_called()
            market_check.assert_not_called()
            self.assertIn("최근 검증: 2026-10-06", output.getvalue())
            self.assertIn("표본 8.00년", output.getvalue())
            self.assertIn("모의원장 미연결, 확인 불가", output.getvalue())
            self.assertEqual(hashlib.sha256(primary.read_bytes()).hexdigest(), before)
            self.assertFalse((root / "paper_trading.sqlite3").exists())

    def test_connected_ledger_count_uses_current_date_and_calendar_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_strategy_db(root)
            ledger = SQLitePaperTradingLedger(root / "paper_trading.sqlite3")
            for day in (
                date(2026, 10, 5),
                date(2026, 10, 6),  # injected holiday
                date(2026, 10, 10),  # weekend
                date(2026, 10, 12),  # after the report's current date
            ):
                ledger.record_model_observation(
                    PaperModelObservation(
                        DEFINITION.strategy_id,
                        DEFINITION.version,
                        "KOSPI",
                        day,
                        3000,
                        0.4,
                        True,
                    ),
                    initial_equity=10_000_000,
                    buy_cost_rate=0.00015,
                    sell_cost_rate=0.00215,
                )
            ledger_path = root / "paper_trading.sqlite3"
            ledger_before = hashlib.sha256(ledger_path.read_bytes()).hexdigest()

            rendered = read_local_strategy_report(
                root,
                DEFINITION.strategy_id,
                DEFINITION.version,
                now=datetime(2026, 10, 9, 17, tzinfo=KST),
                trading_calendar=SessionCalendar({date(2026, 10, 6)}),
            )

            self.assertIn("정상 모의검증 진행: 1/30일", rendered)
            self.assertIn("보고서 모의검증 60일", rendered)
            self.assertEqual(
                hashlib.sha256(ledger_path.read_bytes()).hexdigest(), ledger_before
            )

    def test_intraday_report_reads_intraday_paper_evidence_from_its_existing_store(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            definition = next(
                item
                for item in PUBLIC_STRATEGY_CATALOG
                if item.strategy_id == "intraday_opening_range_breakout"
            )
            strategy_path = root / "strategies.sqlite3"
            strategies = SQLiteStrategyStore(strategy_path)
            strategies.apply_validation(
                ValidationReport(
                    definition.strategy_id,
                    definition.version,
                    "korean_equities",
                    3,
                    True,
                    4,
                    250,
                    0.7,
                    0.2,
                    0.7,
                    45,
                    True,
                    True,
                    "opening_range_breakout",
                ),
                checked_on=date(2026, 10, 6),
            )
            intraday = SQLiteIntradayBarStore(root / "intraday_bars.sqlite3")
            for day in (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 10)):
                intraday.record_paper_day(
                    strategy_id=definition.strategy_id,
                    version=definition.version,
                    symbol="005930",
                    trading_date=day,
                    opening_range_minutes=5,
                    daily_return_pct=0.5,
                    initial_equity=10_000_000,
                    api_healthy=True,
                    costs_complete=True,
                )
            intraday_path = root / "intraday_bars.sqlite3"
            intraday_before = hashlib.sha256(intraday_path.read_bytes()).hexdigest()

            rendered = read_local_strategy_report(
                root,
                definition.strategy_id,
                definition.version,
                now=datetime(2026, 10, 9, 17, tzinfo=KST),
                trading_calendar=SessionCalendar({date(2026, 10, 6)}),
            )

            self.assertIn("정상 모의검증 진행: 1/30일", rendered)
            self.assertIn("보고서 모의검증 45일", rendered)
            self.assertEqual(
                hashlib.sha256(intraday_path.read_bytes()).hexdigest(), intraday_before
            )

    def test_malformed_optional_db_is_unverified_without_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_strategy_db(root)
            optional = root / "paper_trading.sqlite3"
            optional.write_bytes(b"not a sqlite database")
            before = hashlib.sha256(optional.read_bytes()).hexdigest()

            rendered = read_local_strategy_report(
                root,
                DEFINITION.strategy_id,
                DEFINITION.version,
                now=datetime(2026, 10, 9, 17, tzinfo=KST),
                trading_calendar=SessionCalendar(),
            )

            self.assertIn("모의검증 근거 확인 불가", rendered)
            self.assertNotIn("SECRET_CALENDAR_DETAILS", rendered)
            self.assertEqual(hashlib.sha256(optional.read_bytes()).hexdigest(), before)

    def test_calendar_failure_with_healthy_ledger_is_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_strategy_db(root)
            ledger = SQLitePaperTradingLedger(root / "paper_trading.sqlite3")
            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    date(2026, 10, 8),
                    3000,
                    0.4,
                    True,
                ),
                initial_equity=10_000_000,
                buy_cost_rate=0.00015,
                sell_cost_rate=0.00215,
            )

            rendered = read_local_strategy_report(
                root,
                DEFINITION.strategy_id,
                DEFINITION.version,
                now=datetime(2026, 10, 9, 17, tzinfo=KST),
                trading_calendar=BrokenCalendar(),
            )

            self.assertIn("모의검증 근거 확인 불가", rendered)
            self.assertNotIn("SECRET_CALENDAR_DETAILS", rendered)

    def test_valid_but_incompatible_optional_schema_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_strategy_db(root)
            optional = root / "paper_trading.sqlite3"
            connection = sqlite3.connect(optional)
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.commit()
            connection.close()
            before = hashlib.sha256(optional.read_bytes()).hexdigest()

            rendered = read_local_strategy_report(
                root,
                DEFINITION.strategy_id,
                DEFINITION.version,
                now=datetime(2026, 10, 9, 17, tzinfo=KST),
                trading_calendar=SessionCalendar(),
            )

            self.assertIn("모의검증 근거 확인 불가", rendered)
            self.assertEqual(hashlib.sha256(optional.read_bytes()).hexdigest(), before)
            connection = sqlite3.connect(optional)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            connection.close()
            self.assertEqual(tables, {"unrelated"})

    def test_unknown_strategy_or_version_and_missing_primary_fail_without_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            missing = root / "missing"
            primary = make_strategy_db(root)
            before = hashlib.sha256(primary.read_bytes()).hexdigest()
            for strategy_id, version in (("unknown", "1.0.0"), (DEFINITION.strategy_id, "9.9.9")):
                with self.subTest(strategy_id=strategy_id, version=version), self.assertRaises(LocalStrategyReportError):
                    read_local_strategy_report(root, strategy_id, version)
            self.assertEqual(hashlib.sha256(primary.read_bytes()).hexdigest(), before)
            with self.assertRaises(LocalStrategyReportError):
                read_local_strategy_report(missing, DEFINITION.strategy_id, DEFINITION.version)
            self.assertFalse(missing.exists())

    def test_mode_ro_reads_committed_primary_and_evidence_rows_still_in_wal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            primary = make_strategy_db(root)
            ledger_path = root / "paper_trading.sqlite3"
            ledger = SQLitePaperTradingLedger(ledger_path)
            ledger.record_model_observation(
                PaperModelObservation(
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    "KOSPI",
                    date(2026, 10, 8),
                    3000,
                    0.4,
                    True,
                ),
                initial_equity=10_000_000,
                buy_cost_rate=0.00015,
                sell_cost_rate=0.00215,
            )
            primary_writer = sqlite3.connect(primary)
            ledger_writer = sqlite3.connect(ledger_path)
            try:
                primary_writer.execute("PRAGMA journal_mode=WAL")
                primary_writer.execute("PRAGMA wal_autocheckpoint=0")
                primary_writer.execute(
                    "UPDATE validation_reports SET checked_on='2026-10-07' WHERE strategy_id=? AND version=?",
                    (DEFINITION.strategy_id, DEFINITION.version),
                )
                primary_writer.commit()

                ledger_writer.execute("PRAGMA journal_mode=WAL")
                ledger_writer.execute("PRAGMA wal_autocheckpoint=0")
                ledger_writer.execute(
                    "UPDATE paper_model_days SET api_healthy=0 WHERE strategy_id=? AND version=?",
                    (DEFINITION.strategy_id, DEFINITION.version),
                )
                ledger_writer.commit()
                self.assertTrue(Path(str(primary) + "-wal").exists())
                self.assertTrue(Path(str(ledger_path) + "-wal").exists())

                rendered = read_local_strategy_report(
                    root,
                    DEFINITION.strategy_id,
                    DEFINITION.version,
                    now=datetime(2026, 10, 9, 17, tzinfo=KST),
                    trading_calendar=SessionCalendar(),
                )
            finally:
                primary_writer.close()
                ledger_writer.close()

            self.assertIn("최근 검증: 2026-10-07", rendered)
            self.assertIn("정상 모의검증 진행: 0/30일", rendered)

    def test_cli_masks_primary_database_errors_and_has_mutually_exclusive_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            stderr = io.StringIO()
            with patch.dict(os.environ, {"AGENT_SECRET": "DO_NOT_PRINT"}, clear=True), patch(
                "sys.argv", ["agent", "--strategy-report", DEFINITION.strategy_id, DEFINITION.version, "--data-dir", folder]
            ), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as error:
                    main()
            self.assertEqual(error.exception.code, 1)
            self.assertIn("전략 보고서를 읽을 수 없습니다", stderr.getvalue())
            self.assertNotIn(folder, stderr.getvalue())
            self.assertNotIn("DO_NOT_PRINT", stderr.getvalue())
            self.assertFalse((Path(folder) / "strategies.sqlite3").exists())

        with patch(
            "sys.argv",
            ["agent", "--strategy-report", DEFINITION.strategy_id, DEFINITION.version, "--doctor"],
        ), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)

    def test_explicit_data_dir_precedence_and_calendar_override_are_passed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            from_env = root / "environment-data"
            explicit = root / "explicit-data"
            make_strategy_db(from_env, checked_on=date(2026, 10, 1))
            make_strategy_db(explicit, checked_on=date(2026, 10, 6))
            env_file = root / ".env"
            env_file.write_text(
                f"AGENT_DATA_DIR={from_env}\nKRX_CALENDAR_OVERRIDES={root / 'overrides.json'}\n",
                encoding="utf-8",
            )
            SQLitePaperTradingLedger(explicit / "paper_trading.sqlite3")
            expected_overrides = str(root / "overrides.json")
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch(
                "sys.argv",
                ["agent", "--env-file", str(env_file), "--strategy-report", DEFINITION.strategy_id, DEFINITION.version, "--data-dir", str(explicit)],
            ), patch(
                "stock_guide_agent.local_strategy_report.KrxTradingCalendar",
                return_value=SessionCalendar(),
            ) as calendar_factory, redirect_stdout(output):
                main()
            self.assertIn("최근 검증: 2026-10-06", output.getvalue())
            calendar_factory.assert_called_once_with(overrides_path=expected_overrides)

    def test_read_only_store_connections_reject_direct_writes(self):
        with tempfile.TemporaryDirectory(prefix="readonly # space ") as folder:
            root = Path(folder)
            primary = make_strategy_db(root)
            ledger_path = root / "paper_trading.sqlite3"
            intraday_path = root / "intraday_bars.sqlite3"
            SQLitePaperTradingLedger(ledger_path)
            SQLiteIntradayBarStore(intraday_path)
            stores_and_tables = (
                (SQLiteStrategyStore(primary, read_only=True), "strategy_records"),
                (SQLitePaperTradingLedger(ledger_path, read_only=True), "paper_model_days"),
                (SQLiteIntradayBarStore(intraday_path, read_only=True), "intraday_bars"),
            )
            hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (primary, ledger_path, intraday_path)
            }
            for store, table in stores_and_tables:
                with self.subTest(table=table), store._connect() as connection:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                        connection.execute(f"DELETE FROM {table}")
            self.assertEqual(
                hashes,
                {
                    path: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (primary, ledger_path, intraday_path)
                },
            )

    def test_invalid_approved_snapshot_is_displayed_as_suspended_without_persisted_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = make_strategy_db(root)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE strategy_records SET status='approved' WHERE strategy_id=? AND version=?",
                    (DEFINITION.strategy_id, DEFINITION.version),
                )
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM validation_reports WHERE strategy_id=? AND version=?",
                        (DEFINITION.strategy_id, DEFINITION.version),
                    ).fetchone()[0]
                )
                payload["sample_years"] = float("nan")
                connection.execute(
                    "UPDATE validation_reports SET payload_json=? WHERE strategy_id=? AND version=?",
                    (json.dumps(payload), DEFINITION.strategy_id, DEFINITION.version),
                )
                connection.commit()
            finally:
                connection.close()
            connection = sqlite3.connect(path)
            before = connection.execute(
                "SELECT status FROM strategy_records WHERE strategy_id=? AND version=?",
                (DEFINITION.strategy_id, DEFINITION.version),
            ).fetchone()[0]
            connection.close()

            rendered = read_local_strategy_report(
                root, DEFINITION.strategy_id, DEFINITION.version
            )

            self.assertIn("상태: 중지", rendered)
            self.assertIn("수치나 자료 형식이 유효하지 않아 검증이 거부됐습니다", rendered)
            connection = sqlite3.connect(path)
            after = connection.execute(
                "SELECT status FROM strategy_records WHERE strategy_id=? AND version=?",
                (DEFINITION.strategy_id, DEFINITION.version),
            ).fetchone()[0]
            connection.close()
            self.assertEqual(before, "approved")
            self.assertEqual(after, "approved")


if __name__ == "__main__":
    unittest.main()
