import unittest

from stock_guide_agent.connectivity import (
    run_connectivity_diagnostic,
    run_market_connectivity,
)
from stock_guide_agent.http import HttpRequest, HttpResponse


ENV = {
    "TELEGRAM_BOT_TOKEN": "telegram-secret",
    "TELEGRAM_OWNER_USER_ID": "900",
    "TOSSINVEST_CLIENT_ID": "client-secret-id",
    "TOSSINVEST_CLIENT_SECRET": "toss-secret",
}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ConnectivityTests(unittest.TestCase):
    def test_missing_telegram_keys_fail_without_network(self):
        transport = FakeTransport([])

        report = run_connectivity_diagnostic({}, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(transport.requests, [])
        self.assertIn("network", {check.check_id for check in report.checks})

    def test_checks_only_safe_telegram_metadata(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"ok": True, "result": {"id": 1, "is_bot": True}}),
            HttpResponse(200, {}, {"ok": True, "result": {"url": ""}}),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        self.assertTrue(report.ready)
        self.assertEqual([request.url.rsplit("/", 1)[-1] for request in transport.requests],
                         ["getMe", "getWebhookInfo"])
        self.assertTrue(all(request.url.startswith("https://api.telegram.org/")
                            for request in transport.requests[:2]))

    def test_webhook_conflict_is_reported_without_polling(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"ok": True, "result": {"id": 1, "is_bot": True}}),
            HttpResponse(200, {}, {"ok": True, "result": {"url": "https://example.test/hook"}}),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        webhook = next(check for check in report.checks if check.check_id == "telegram_webhook")
        self.assertEqual(webhook.status, "error")
        self.assertIn("polling", webhook.message)
        self.assertFalse(report.ready)
        self.assertFalse(any("getUpdates" in request.url for request in transport.requests))

    def test_market_check_authenticates_then_uses_only_known_price_probe(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"access_token": "access-secret", "token_type": "Bearer"}),
            HttpResponse(200, {}, {"result": [{"symbol": "005930", "timestamp": "2026-01-01T00:00:00+00:00", "lastPrice": "80000", "currency": "KRW"}]}),
        ])

        report = run_market_connectivity(ENV, transport=transport)

        self.assertTrue(report.ready)
        request = transport.requests[-1]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url, "https://openapi.tossinvest.com/api/v1/prices")
        self.assertEqual(request.query, {"symbols": "005930"})

    def test_market_missing_credentials_skip_network(self):
        transport = FakeTransport([])

        report = run_market_connectivity({}, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(transport.requests, [])

    def test_market_rejects_a_success_response_without_documented_shape(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"access_token": "access-secret", "token_type": "Bearer"}),
            HttpResponse(200, {}, {"result": []}),
        ])

        report = run_market_connectivity(ENV, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(next(item.status for item in report.checks if item.check_id == "toss_market"), "error")

    def test_error_details_do_not_expose_secrets_or_payload(self):
        transport = FakeTransport([
            RuntimeError("telegram-secret toss-secret raw response"),
            RuntimeError("telegram-secret raw response"),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        text = " ".join(check.message for check in report.checks)
        self.assertFalse(report.ready)
        for secret in ("telegram-secret", "toss-secret", "client-secret-id", "raw response"):
            self.assertNotIn(secret, text)

    def test_malformed_telegram_identity_fails_readiness(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"ok": True, "result": {"id": True, "is_bot": True}}),
            HttpResponse(200, {}, {"ok": True, "result": {"url": ""}}),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(next(item.status for item in report.checks if item.check_id == "telegram_identity"), "error")

    def test_malformed_webhook_metadata_fails_readiness(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"ok": True, "result": {"id": 1, "is_bot": True}}),
            HttpResponse(200, {}, {"ok": True, "result": {}}),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(next(item.status for item in report.checks if item.check_id == "telegram_webhook"), "error")

    def test_webhook_conflict_blocks_readiness(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"ok": True, "result": {"id": 1, "is_bot": True}}),
            HttpResponse(200, {}, {"ok": True, "result": {"url": "https://example.test/hook"}}),
        ])

        report = run_connectivity_diagnostic(ENV, transport=transport)

        self.assertFalse(report.ready)

    def test_invalid_telegram_settings_skip_network(self):
        transport = FakeTransport([])

        report = run_connectivity_diagnostic(
            {**ENV, "TELEGRAM_BOT_TOKEN": "bad/token", "TELEGRAM_OWNER_USER_ID": "zero"},
            transport=transport,
        )

        self.assertFalse(report.ready)
        self.assertEqual(transport.requests, [])

    def test_market_rejects_nan_price_and_invalid_timestamp(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"access_token": "access-secret", "token_type": "Bearer"}),
            HttpResponse(200, {}, {"result": [{"symbol": "005930", "timestamp": "2026-01-01", "lastPrice": "NaN", "currency": "KRW"}]}),
        ])

        report = run_market_connectivity(ENV, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(next(item.status for item in report.checks if item.check_id == "toss_market"), "error")

    def test_market_rejects_empty_documented_fields(self):
        transport = FakeTransport([
            HttpResponse(200, {}, {"access_token": "access-secret", "token_type": "Bearer"}),
            HttpResponse(200, {}, {"result": [{"symbol": "005930", "timestamp": "2026-01-01T00:00:00+00:00", "lastPrice": "", "currency": ""}]}),
        ])

        report = run_market_connectivity(ENV, transport=transport)

        self.assertFalse(report.ready)
        self.assertEqual(next(item.status for item in report.checks if item.check_id == "toss_market"), "error")

if __name__ == "__main__":
    unittest.main()
