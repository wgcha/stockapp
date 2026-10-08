"""KRX trading-session boundaries used by scheduled work.

The application deliberately does not infer Korean exchange sessions from a
weekday.  `exchange_calendars` owns holiday and exceptional-session data; when
that data is unavailable or does not cover a date, callers must fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
import json
from pathlib import Path
from typing import Protocol

from .paper_trading import KOREA_TIMEZONE


class TradingCalendarUnavailable(RuntimeError):
    """The installed market calendar cannot make a safe session decision."""


@dataclass(frozen=True)
class TradingSession:
    trading_date: date
    opens_at: datetime
    closes_at: datetime

    @property
    def evaluation_available_at(self) -> datetime:
        """Allow ten minutes for the final close data to settle."""
        return self.closes_at + timedelta(minutes=10)


class TradingCalendar(Protocol):
    def session_for(self, value: datetime | date) -> TradingSession | None: ...

    def is_after_close_evaluation(self, now: datetime) -> bool: ...

    def daily_guide_start(
        self, now: datetime, *, configured_hour: int, configured_minute: int
    ) -> datetime | None: ...


class KrxTradingCalendar:
    """Adapter for the XKRX calendar supplied by ``exchange_calendars``.

    Version 4.12 is pinned because it includes the XKRX 2026 calendar.  The
    adapter intentionally turns dependency/range/API errors into a single
    fail-closed exception instead of assuming a weekday session.
    """

    def __init__(self, calendar: object | None = None, *, overrides_path: str | None = None) -> None:
        if calendar is None:
            try:
                import exchange_calendars

                calendar = exchange_calendars.get_calendar("XKRX")
            except Exception as exc:  # Import error and calendar setup are unsafe.
                raise TradingCalendarUnavailable(
                    "XKRX calendar is unavailable; scheduled market work is blocked"
                ) from exc
        self._calendar = calendar
        self._overrides: dict[date, TradingSession | None] = {}
        if overrides_path:
            try:
                path = Path(overrides_path)
                if path.stat().st_size > 100000:
                    raise ValueError
                overrides = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(overrides, dict):
                    raise ValueError
                for label, entry in overrides.items():
                    day = date.fromisoformat(label)
                    if not isinstance(entry, dict) or not str(entry.get("source_url", "")).startswith("https://"):
                        raise ValueError
                    if entry.get("closed") is True:
                        self._overrides[day] = None
                    else:
                        open_time, close_time = time.fromisoformat(entry["open"]), time.fromisoformat(entry["close"])
                        if open_time.tzinfo is not None or close_time.tzinfo is not None:
                            raise ValueError
                        opens = datetime.combine(day, open_time, tzinfo=KOREA_TIMEZONE)
                        closes = datetime.combine(day, close_time, tzinfo=KOREA_TIMEZONE)
                        if closes <= opens:
                            raise ValueError
                        self._overrides[day] = TradingSession(day, opens, closes)
            except Exception:
                raise TradingCalendarUnavailable("Invalid local KRX session overrides") from None

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trading calendar datetimes must be timezone-aware")

    @staticmethod
    def _to_datetime(value: object) -> datetime:
        converter = getattr(value, "to_pydatetime", None)
        converted = converter() if callable(converter) else value
        if not isinstance(converted, datetime) or converted.tzinfo is None:
            raise TradingCalendarUnavailable("XKRX calendar returned an invalid timestamp")
        return converted.astimezone(KOREA_TIMEZONE)

    def session_for(self, value: datetime | date) -> TradingSession | None:
        if isinstance(value, datetime):
            self._require_aware(value)
            session_date = value.astimezone(KOREA_TIMEZONE).date()
        else:
            session_date = value
        if session_date in self._overrides:
            return self._overrides[session_date]
        try:
            # exchange_calendars accepts a date-like session label. Avoid pandas
            # imports here; it is a transitive dependency of the pinned package.
            if not bool(self._calendar.is_session(session_date)):
                return None
            opens_at = self._to_datetime(self._calendar.session_open(session_date))
            closes_at = self._to_datetime(self._calendar.session_close(session_date))
        except TradingCalendarUnavailable:
            raise
        except Exception as exc:
            raise TradingCalendarUnavailable(
                f"XKRX calendar cannot determine {session_date.isoformat()}"
            ) from exc
        if opens_at.date() != session_date or closes_at.date() != session_date:
            raise TradingCalendarUnavailable(
                f"XKRX calendar returned an invalid session boundary for {session_date.isoformat()}"
            )
        if closes_at <= opens_at:
            raise TradingCalendarUnavailable("XKRX calendar close must follow open")
        return TradingSession(session_date, opens_at, closes_at)

    def is_after_close_evaluation(self, now: datetime) -> bool:
        self._require_aware(now)
        session = self.session_for(now)
        return session is not None and now.astimezone(KOREA_TIMEZONE) >= session.evaluation_available_at

    def daily_guide_start(
        self, now: datetime, *, configured_hour: int, configured_minute: int
    ) -> datetime | None:
        self._require_aware(now)
        if not 0 <= configured_hour <= 23 or not 0 <= configured_minute <= 59:
            raise ValueError("daily guide time is invalid")
        session = self.session_for(now)
        if session is None:
            return None
        configured = datetime(
            session.trading_date.year,
            session.trading_date.month,
            session.trading_date.day,
            configured_hour,
            configured_minute,
            tzinfo=KOREA_TIMEZONE,
        )
        return max(configured, session.opens_at + timedelta(minutes=10))
