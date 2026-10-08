import unittest
import json
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from stock_guide_agent.trading_calendar import (
    KrxTradingCalendar,
    TradingCalendarUnavailable,
    TradingSession,
)


KST = timezone(timedelta(hours=9))


class Timestamp:
    def __init__(self, value):
        self.value = value

    def to_pydatetime(self):
        return self.value


class FakeXkrx:
    def __init__(self, sessions):
        self.sessions = sessions

    def is_session(self, session_date):
        return session_date in self.sessions

    def session_open(self, session_date):
        return Timestamp(self.sessions[session_date][0])

    def session_close(self, session_date):
        return Timestamp(self.sessions[session_date][1])


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


class TradingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.normal = date(2026, 7, 16)
        self.weekday_holiday = date(2026, 7, 17)
        self.special_open = date(2026, 11, 12)
        self.year_end = date(2026, 12, 31)
        self.calendar = KrxTradingCalendar(
            FakeXkrx(
                {
                    self.normal: (at(self.normal, 9), at(self.normal, 15, 30)),
                    # Explicitly model an exceptional later opening.
                    self.special_open: (at(self.special_open, 10), at(self.special_open, 15, 30)),
                    self.year_end: (at(self.year_end, 9), at(self.year_end, 15, 30)),
                }
            )
        )

    def test_normal_session_uses_krx_open_close_and_ten_minute_evaluation_delay(self):
        session = self.calendar.session_for(at(self.normal, 12))
        self.assertEqual(session, TradingSession(self.normal, at(self.normal, 9), at(self.normal, 15, 30)))
        self.assertFalse(self.calendar.is_after_close_evaluation(at(self.normal, 15, 39)))
        self.assertTrue(self.calendar.is_after_close_evaluation(at(self.normal, 15, 40)))

    def test_weekday_holiday_is_closed_without_weekday_fallback(self):
        self.assertIsNone(self.calendar.session_for(at(self.weekday_holiday, 10)))
        self.assertFalse(self.calendar.is_after_close_evaluation(at(self.weekday_holiday, 16)))
        self.assertIsNone(
            self.calendar.daily_guide_start(
                at(self.weekday_holiday, 8), configured_hour=9, configured_minute=10
            )
        )

    def test_special_opening_delays_legacy_daily_guide_time(self):
        self.assertEqual(
            self.calendar.daily_guide_start(
                at(self.special_open, 8), configured_hour=9, configured_minute=10
            ),
            at(self.special_open, 10, 10),
        )
        self.assertEqual(
            self.calendar.daily_guide_start(
                at(self.special_open, 8), configured_hour=11, configured_minute=0
            ),
            at(self.special_open, 11),
        )

    def test_year_end_is_decided_by_calendar_data(self):
        self.assertEqual(self.calendar.session_for(at(self.year_end, 10)).trading_date, self.year_end)

    def test_calendar_api_error_fails_closed(self):
        class BrokenCalendar:
            def is_session(self, value):
                raise ValueError("out of supported range")

        with self.assertRaises(TradingCalendarUnavailable):
            KrxTradingCalendar(BrokenCalendar()).session_for(at(date(2030, 1, 2), 10))

    def test_installed_xkrx_holidays_year_end_and_first_session(self):
        calendar = KrxTradingCalendar()
        self.assertIsNone(calendar.session_for(date(2026, 5, 5)))
        self.assertIsNone(calendar.session_for(date(2026, 12, 31)))
        self.assertEqual(calendar.session_for(date(2026, 1, 2)).opens_at.hour, 10)
        self.assertEqual(calendar.session_for(date(2026, 7, 16)).closes_at.hour, 15)
        with self.assertRaises(TradingCalendarUnavailable):
            calendar.session_for(date(1900, 1, 1))

    def test_local_sourced_override_for_temporary_closure_and_special_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.json"
            path.write_text(json.dumps({
                "2026-07-16": {"closed": True, "source_url": "https://example.test/notice"},
                "2026-07-17": {"open": "10:00", "close": "16:30", "source_url": "https://example.test/notice"}
            }), encoding="utf-8")
            calendar = KrxTradingCalendar(FakeXkrx({}), overrides_path=str(path))
            self.assertIsNone(calendar.session_for(date(2026, 7, 16)))
            self.assertEqual(calendar.session_for(date(2026, 7, 17)).evaluation_available_at.hour, 16)
            path.write_text('{"bad":{}}', encoding="utf-8")
            with self.assertRaises(TradingCalendarUnavailable):
                KrxTradingCalendar(FakeXkrx({}), overrides_path=str(path))


if __name__ == "__main__":
    unittest.main()
