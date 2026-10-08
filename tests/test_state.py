import tempfile
import unittest
from datetime import timedelta
from datetime import date, datetime, timezone
from pathlib import Path

from stock_guide_agent.holdings import parse_holding_message
from stock_guide_agent.intent import parse_investment_intent
from stock_guide_agent.state import Holding, SQLiteUserStateStore


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class UserStateTests(unittest.TestCase):
    def test_temporary_intent_expires_but_permanent_intent_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "state.sqlite3")
            temporary = parse_investment_intent(
                "이번 한주는 매일 3%, 스윙 100%, 하루 손실 1%"
            )
            permanent = parse_investment_intent(
                "장기 100%, 연간 8%, 하루 손실 1%"
            )
            store.save_intent(30, temporary, updated_at=NOW)
            store.save_intent(40, permanent, updated_at=NOW)

            self.assertIsNotNone(
                store.load_intent(30, as_of=NOW + timedelta(days=6))
            )
            self.assertIsNone(
                store.load_intent(30, as_of=NOW + timedelta(days=7))
            )
            self.assertIsNotNone(
                store.load_intent(40, as_of=NOW + timedelta(days=365))
            )

    def test_holding_message_is_simple_and_strict(self) -> None:
        parsed = parse_holding_message("보유 005930 10주 평단 70,000원")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.stock_code, "005930")
        self.assertEqual(parsed.quantity, 10)
        self.assertEqual(parsed.average_price, 70000)
        natural = parse_holding_message("005930 10주를 7만 5000원에 갖고 있어")
        self.assertIsNotNone(natural)
        assert natural is not None
        self.assertEqual(natural.average_price, 75000)
        concise = parse_holding_message("005930 3주 평단 7.5만원 보유 중")
        self.assertIsNotNone(concise)
        assert concise is not None
        self.assertEqual(concise.average_price, 75000)
        self.assertIsNone(parse_holding_message("005930 살까?"))

    def test_intent_and_holdings_are_isolated_and_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            store = SQLiteUserStateStore(path)
            store.save_intent(
                30,
                parse_investment_intent("장기안정 60% 단타 40%, 매일 3% 목표"),
                updated_at=NOW,
            )
            store.save_intent(
                40,
                parse_investment_intent("장기 100%, 연간 8% 목표"),
                updated_at=NOW,
            )
            store.upsert_holding(Holding(30, "005930", 10, 70000, NOW))
            store.upsert_holding(Holding(40, "000660", 2, 180000, NOW))

            reopened = SQLiteUserStateStore(path)
            self.assertEqual(reopened.load_intent(30).allocations[0].weight_pct, 60)
            self.assertEqual(reopened.load_intent(40).target_return_pct, 8)
            self.assertEqual([h.stock_code for h in reopened.list_holdings(30)], ["005930"])
            self.assertEqual([h.stock_code for h in reopened.list_holdings(40)], ["000660"])

    def test_same_symbol_is_scoped_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "agent.sqlite3")
            store.upsert_holding(Holding(30, "005930", 10, 70000, NOW))
            store.upsert_holding(Holding(40, "005930", 1, 80000, NOW))

            self.assertEqual(store.get_holding(30, "005930").quantity, 10)
            self.assertEqual(store.get_holding(40, "005930").quantity, 1)

    def test_watchlist_and_holdings_form_user_scoped_alternative_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "agent.sqlite3")
            store.upsert_holding(Holding(30, "005930", 10, 70000, NOW))
            store.add_watchlist_symbols(30, ["000660", "035420"], added_at=NOW)
            store.add_watchlist_symbols(40, ["051910"], added_at=NOW)

            self.assertEqual(
                store.list_candidate_symbols(30, "005930"), ["000660", "035420"]
            )
            self.assertEqual(store.list_candidate_symbols(40, "005930"), ["051910"])

    def test_broker_selection_is_persisted_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            store = SQLiteUserStateStore(path)
            store.set_broker_provider(30, "mirae_asset", updated_at=NOW)
            store.set_broker_provider(40, "toss_invest", updated_at=NOW)

            reopened = SQLiteUserStateStore(path)
            self.assertEqual(reopened.get_broker_provider(30), "mirae_asset")
            self.assertEqual(reopened.get_broker_provider(40), "toss_invest")
            self.assertEqual(reopened.get_broker_provider(50), "toss_invest")

    def test_account_equity_is_persisted_and_isolated_per_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            store = SQLiteUserStateStore(path)
            store.set_account_equity(30, 10_000_000, updated_at=NOW)
            store.set_account_equity(40, 50_000_000, updated_at=NOW)

            reopened = SQLiteUserStateStore(path)
            self.assertEqual(reopened.get_account_equity(30), 10_000_000)
            self.assertEqual(reopened.get_account_equity(40), 50_000_000)
            self.assertIsNone(reopened.get_account_equity(50))

    def test_daily_guide_recipients_are_opt_in_and_idempotent_by_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "agent.sqlite3")
            store.upsert_user_channel(30, 3000, updated_at=NOW)
            store.upsert_user_channel(40, 4000, updated_at=NOW)
            store.set_daily_guide_enabled(30, True, updated_at=NOW)

            due = store.list_due_daily_guide_recipients(NOW.date())
            self.assertEqual([(item.user_id, item.chat_id) for item in due], [(30, 3000)])

            store.mark_daily_guide_sent(30, NOW.date())
            self.assertEqual(store.list_due_daily_guide_recipients(NOW.date()), ())
            self.assertEqual(
                [item.user_id for item in store.list_due_daily_guide_recipients(
                    NOW.date() + timedelta(days=1)
                )],
                [30],
            )

    def test_order_execution_count_is_user_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "agent.sqlite3")
            store.record_order_execution("p1", 30, executed_at=NOW)
            store.record_order_execution("p1", 30, executed_at=NOW)
            store.record_order_execution("p2", 40, executed_at=NOW)

            self.assertEqual(store.count_order_executions(30, NOW.date()), 1)
            self.assertEqual(store.count_order_executions(40, NOW.date()), 1)
            self.assertEqual(store.count_all_order_executions(NOW.date()), 2)

    def test_order_execution_uses_korean_calendar_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteUserStateStore(Path(directory) / "agent.sqlite3")
            # 16:00 UTC is 01:00 on the following day in Korea.
            executed = datetime(2026, 7, 17, 16, tzinfo=timezone.utc)
            store.record_order_execution("p1", 30, executed_at=executed)

            self.assertEqual(store.count_order_executions(30, date(2026, 7, 17)), 0)
            self.assertEqual(store.count_order_executions(30, date(2026, 7, 18)), 1)


if __name__ == "__main__":
    unittest.main()
