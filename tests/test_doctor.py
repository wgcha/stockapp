import unittest

from stock_guide_agent.doctor import run_preflight


BASE = {
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_OWNER_USER_ID": "900",
    "TELEGRAM_ALLOWED_USER_IDS": "30,40",
    "TOSSINVEST_CLIENT_ID": "client",
    "TOSSINVEST_CLIENT_SECRET": "secret",
    "AGENT_EXECUTION_ENV": "mock",
}


class DoctorTests(unittest.TestCase):
    def test_invalid_intraday_archive_limits_fail_preflight(self) -> None:
        report = run_preflight(
            {
                **BASE,
                "INTRADAY_MINIMUM_BARS": "20",
                "INTRADAY_ARCHIVE_MAX_SYMBOLS": "100",
            }
        )

        self.assertFalse(report.ready)
        errors = {item.check_id for item in report.checks if item.status == "error"}
        self.assertIn("intraday_minimum_bars", errors)
        self.assertIn("intraday_archive_symbols", errors)

    def test_invalid_daily_guide_time_fails_preflight(self) -> None:
        report = run_preflight(
            {
                **BASE,
                "DAILY_GUIDE_HOUR_KST": "24",
                "DAILY_GUIDE_MINUTE_KST": "60",
            }
        )

        self.assertFalse(report.ready)
        errors = {item.check_id for item in report.checks if item.status == "error"}
        self.assertIn("daily_guide_hour", errors)
        self.assertIn("daily_guide_minute", errors)

    def test_invalid_paper_capital_and_mirae_commission_fail_preflight(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "TOKEN",
            "TELEGRAM_OWNER_USER_ID": "900",
            "TOSSINVEST_CLIENT_ID": "CLIENT",
            "TOSSINVEST_CLIENT_SECRET": "SECRET",
            "PAPER_INITIAL_CAPITAL_KRW": "0",
            "MIRAE_PAPER_COMMISSION_RATE": "10%",
        }

        report = run_preflight(environment)

        self.assertFalse(report.ready)
        errors = {item.check_id for item in report.checks if item.status == "error"}
        self.assertIn("paper_initial_cash", errors)
        self.assertIn("mirae_paper_cost", errors)

    def test_invalid_crossref_discovery_settings_fail_preflight(self) -> None:
        report = run_preflight(
            {
                **BASE,
                "CROSSREF_DISCOVERY_ENABLED": "sometimes",
                "CROSSREF_DISCOVERY_ROWS": "500",
            }
        )

        self.assertFalse(report.ready)
        check = next(
            item for item in report.checks if item.check_id == "crossref_discovery"
        )
        self.assertEqual(check.status, "error")

    def test_minimal_mock_configuration_is_ready_with_optional_warnings(self):
        report = run_preflight(BASE)

        self.assertTrue(report.ready)
        self.assertTrue(any(item.status == "warning" for item in report.checks))

    def test_live_mode_fails_even_with_all_former_enablement_values(self):
        report = run_preflight(
            {
                **BASE,
                "AGENT_EXECUTION_ENV": "live",
                "AGENT_LIVE_TRADING_ENABLED": "true",
                "TOSSINVEST_LIVE_ORDERS": "true",
                "TOSSINVEST_ACCOUNT_SEQ": "1",
                "AGENT_ACCOUNT_EQUITY_KRW": "10000000",
            }
        )

        self.assertFalse(report.ready)
        gate = next(item for item in report.checks if item.check_id == "live_trading_gate")
        self.assertEqual(gate.status, "error")
        self.assertIn("not supported", gate.message)

    def test_invalid_account_equity_fails_even_in_mock_mode(self) -> None:
        report = run_preflight({**BASE, "AGENT_ACCOUNT_EQUITY_KRW": "-1"})

        self.assertFalse(report.ready)
        check = next(
            item for item in report.checks if item.check_id == "account_equity"
        )
        self.assertEqual(check.status, "error")

    def test_partial_optional_credentials_fail_preflight(self):
        report = run_preflight({**BASE, "NAVER_API_HUB_CLIENT_ID": "id"})

        self.assertFalse(report.ready)
        news = next(item for item in report.checks if item.check_id == "news")
        self.assertEqual(news.status, "error")


if __name__ == "__main__":
    unittest.main()
