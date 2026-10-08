"""Read-only, offline strategy reports from the local SQLite evidence stores."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .data_lock import checked_path
from .intraday import SQLiteIntradayBarStore
from .paper_trading import KOREA_TIMEZONE, SQLitePaperTradingLedger
from .strategies import StrategyRecord
from .strategy_reporting import format_strategy_report
from .strategy_store import SQLiteStrategyStore, StrategyNotFound
from .trading_calendar import KrxTradingCalendar, TradingCalendar


class LocalStrategyReportError(ValueError):
    """A safe, fixed-message failure while reading a local strategy report."""


def read_local_strategy_report(
    data_dir: str | Path,
    strategy_id: str,
    version: str,
    *,
    now: datetime | None = None,
    trading_calendar: TradingCalendar | None = None,
    calendar_overrides_path: str | Path | None = None,
) -> str:
    """Render a report without creating, migrating, or modifying any database."""
    if now is None:
        now = datetime.now(KOREA_TIMEZONE)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("report time must be timezone-aware")

    try:
        root = checked_path(data_dir)
        if not root.is_dir():
            raise LocalStrategyReportError("strategy report data directory is unavailable")
        strategy_path = checked_path(root / "strategies.sqlite3")
        if not strategy_path.is_file():
            raise LocalStrategyReportError("strategy report database is unavailable")
        strategy_store = SQLiteStrategyStore(strategy_path, read_only=True)
        record = strategy_store.get_record(strategy_id, version)
        checked_on = strategy_store.latest_validation_on(strategy_id, version)
    except LocalStrategyReportError:
        raise
    except StrategyNotFound:
        raise LocalStrategyReportError("strategy report record is unavailable") from None
    except Exception:
        raise LocalStrategyReportError("strategy report database is unavailable") from None

    connected, current_days = _current_paper_days(
        root,
        record,
        as_of=now.astimezone(KOREA_TIMEZONE).date(),
        trading_calendar=trading_calendar,
        calendar_overrides_path=calendar_overrides_path,
    )
    if not connected:
        progress = "모의원장 미연결, 확인 불가"
    elif current_days is None:
        progress = "모의검증 근거 확인 불가"
    else:
        progress = f"{min(current_days, 30)}/30일"
    return format_strategy_report(
        record,
        checked_on=checked_on,
        paper_progress=progress,
    )


def _current_paper_days(
    root: Path,
    record: StrategyRecord,
    *,
    as_of: date,
    trading_calendar: TradingCalendar | None,
    calendar_overrides_path: str | Path | None,
) -> tuple[bool, int | None]:
    filename = (
        "intraday_bars.sqlite3"
        if record.definition.requires_intraday_data
        else "paper_trading.sqlite3"
    )
    try:
        evidence_path = checked_path(root / filename)
        if not evidence_path.exists():
            return False, None
        if not evidence_path.is_file():
            return True, None
        calendar = trading_calendar or KrxTradingCalendar(
            overrides_path=(str(calendar_overrides_path) if calendar_overrides_path else None)
        )
        if record.definition.requires_intraday_data:
            store = SQLiteIntradayBarStore(evidence_path, read_only=True)
            count = store.qualified_paper_days(
                record.definition.strategy_id,
                record.definition.version,
                as_of=as_of,
                trading_calendar=calendar,
            )
        else:
            store = SQLitePaperTradingLedger(evidence_path, read_only=True)
            count = store.completed_days(
                record.definition.strategy_id,
                record.definition.version,
                as_of=as_of,
                trading_calendar=calendar,
            )
        return True, count
    except Exception:
        return True, None
