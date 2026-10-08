import tempfile
import io
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from stock_guide_agent.doctor import run_preflight
from stock_guide_agent.http import HttpRequest, HttpResponse, TransportError, urllib_json_transport
from stock_guide_agent.messaging import SQLiteMessageStore
from stock_guide_agent.telegram import TelegramApiError, TelegramBotClient, TelegramConfigurationError, parse_telegram_update
from stock_guide_agent.toss import TossInvestToken
from stock_guide_agent.cli import main


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class DeliverySecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = SQLiteMessageStore(self.path)

    def test_reply_is_persisted_before_network_and_retried_after_restart(self):
        calls = []

        def transport(request):
            calls.append(request)
            if len(calls) == 1:
                raise RuntimeError("SECRET_TOKEN, account=123, user text")
            return HttpResponse(200, {}, {"ok": True})

        bot = TelegramBotClient("TOKEN", 900, allowed_user_ids={30}, transport=transport,
                                message_store=self.store)
        bot.send_text(30, "개인 가이드")
        self.assertEqual(calls, [])
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.assertEqual(bot.flush_outbox(now=now), 1)
        bot = TelegramBotClient("TOKEN", 900, allowed_user_ids={30}, transport=transport,
                                message_store=SQLiteMessageStore(self.path))
        self.assertEqual(bot.flush_outbox(now=now + timedelta(seconds=4)), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(bot.flush_outbox(now=now + timedelta(seconds=5)), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.due_messages(now=now + timedelta(days=1)), [])

    def test_removed_member_queued_reply_is_discarded_without_sending(self):
        self.store.enqueue_message(30, "개인 보유정보", now=NOW)
        bot = TelegramBotClient("TOKEN", 900, message_store=self.store,
                                transport=lambda request: self.fail("unauthorized recipient"))
        self.assertEqual(bot.flush_outbox(now=NOW), 0)
        self.assertEqual(self.store.due_messages(now=NOW), [])
        with self.assertRaises(TelegramConfigurationError):
            bot.send_text(30, "new reply")

    def test_reply_errors_do_not_echo_provider_description(self):
        bot = TelegramBotClient("SECRET_TOKEN", 900, transport=lambda request:
            HttpResponse(400, {}, {"ok": False, "description": "SECRET_TOKEN account=123"}))
        for action in (lambda: bot.get_updates(timeout_seconds=0), lambda: bot.send_text(900, "hi")):
            with self.assertRaises(TelegramApiError) as caught:
                action()
            self.assertNotIn("SECRET_TOKEN", str(caught.exception))
            self.assertNotIn("account", str(caught.exception))

    def test_private_chat_cannot_redirect_another_users_reply(self):
        for sender_id, chat_id, is_bot in ((30, 40, False), (30, -1, False), (0, 0, False), (30, 30, True)):
            self.assertIsNone(parse_telegram_update({"update_id": 1, "message": {
                "text": "내 보유", "from": {"id": sender_id, "is_bot": is_bot},
                "chat": {"id": chat_id, "type": "private"}}}))
        with self.assertRaises(TelegramConfigurationError):
            TelegramBotClient("TOKEN", 900, owner_chat_id=901)

    def test_doctor_rejects_owner_chat_misdirection_without_echoing_input(self):
        report = run_preflight({"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_OWNER_USER_ID": "900",
                               "TELEGRAM_OWNER_CHAT_ID": "901", "TOSSINVEST_CLIENT_ID": "ID",
                               "TOSSINVEST_CLIENT_SECRET": "SECRET"})
        self.assertFalse(report.ready)
        check = next(item for item in report.checks if item.check_id == "telegram_user_ids")
        self.assertEqual(check.status, "error")
        self.assertNotIn("901", check.message)

    def test_request_repr_redacts_credentials_and_private_body_recursively(self):
        request = HttpRequest("POST", "https://host/api/SECRET_TOKEN?client_secret=SECRET_VALUE",
                              headers={"Authorization": "Bearer ACCESS", "X-Tossinvest-Account": "ACCOUNT"},
                              json_body={"nested": [{"text": "PRIVATE", "chat_id": 30}], "echo": "SECRET_TOKEN"},
                              sensitive_values=("SECRET_TOKEN",))
        rendered = repr(request)
        for secret in ("SECRET_TOKEN", "SECRET_VALUE", "Bearer ACCESS", "ACCOUNT", "PRIVATE"):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("ACCESS", repr(TossInvestToken("ACCESS", "Bearer", None)))
        self.assertNotIn("SECRET", repr(HttpResponse(200, {"cookie": "SECRET"}, {"token": "SECRET"})))

    def test_http_transport_failure_does_not_leak_credentials_in_traceback(self):
        request = HttpRequest("GET", "https://example.test/SECRET_TOKEN")
        with patch("stock_guide_agent.http.urlopen", side_effect=RuntimeError("URL SECRET_TOKEN")):
            try:
                urllib_json_transport(request)
            except TransportError:
                rendered = traceback.format_exc()
            else:
                self.fail("expected a transport failure")
        self.assertNotIn("SECRET_TOKEN", rendered)
        self.assertIn("HTTP request failed", rendered)

    def test_cli_startup_error_is_safe_and_failure_loop_yields(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as data_dir:
            with patch("sys.argv", ["agent", "--run-bot", "--data-dir", data_dir]), \
                 patch("stock_guide_agent.cli.build_runtime_from_env", side_effect=ValueError("SECRET_TOKEN")), \
                 redirect_stderr(output):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 1)
            self.assertNotIn("SECRET_TOKEN", output.getvalue())
            self.assertIn("--doctor", output.getvalue())
            with patch("sys.argv", ["agent", "--run-bot", "--data-dir", data_dir]), \
                 patch("stock_guide_agent.cli.build_runtime_from_env") as build, \
                 patch("stock_guide_agent.lifecycle.threading.Event.wait", side_effect=KeyboardInterrupt) as sleep:
                with self.assertRaises(KeyboardInterrupt):
                    main()
        build.return_value.poll_once.assert_called_once()
        build.return_value.close.assert_called_once()
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
