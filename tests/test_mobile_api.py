from __future__ import annotations

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from stock_guide_agent.guidance import GuideInput
from stock_guide_agent.market import Evidence, MarketSnapshot
from stock_guide_agent.mobile_api import MobileApi, build_mobile_api, make_handler
from stock_guide_agent.state import Holding, SQLiteUserStateStore


TOKEN = "unit-test-mobile-token-0123456789abcdef"


class MobileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = SQLiteUserStateStore(self.directory / "users.sqlite3")
        self.api = MobileApi(self.store, token=TOKEN)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: object | None = None,
        token: str | None = TOKEN,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            body = json.dumps(payload, allow_nan=True).encode("utf-8")
            headers["Content-Type"] = content_type
        elif raw_body is not None:
            body = raw_body
            headers["Content-Type"] = content_type
        else:
            body = None
        request = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_is_public_and_user_state_requires_bearer_key(self) -> None:
        self.assertEqual(
            self.request("/health", token=None),
            (200, {"status": "ok", "api_version": 1}),
        )
        status, payload = self.request("/v1/state", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["message"], "인증 정보를 확인해주세요.")
        self.assertNotIn(TOKEN, json.dumps(payload))
        self.assertEqual(self.request("/v1/state", token="incorrect-token")[0], 401)
        self.assertEqual(
            self.request("/v1/state")[1],
            {
                "mode": "server",
                "guide_only": True,
                "account_equity_krw": None,
                "holdings": [],
                "watchlist": [],
                "market": {"status": "unconfigured", "message": "시세 연결 정보가 없습니다."},
            },
        )

    def test_crud_is_owner_scoped_atomic_and_persistent(self) -> None:
        self.store.upsert_holding(Holding(2, "111111", 4, 12.5, datetime.now(timezone.utc)))
        status, profile = self.request("/v1/profile", method="PUT", payload={"account_equity_krw": 1_000_000})
        self.assertEqual(status, 200)
        self.assertEqual(profile["account_equity_krw"], 1_000_000)
        status, holdings = self.request(
            "/v1/holdings/005930", method="PUT", payload={"quantity": 10, "average_price": 70000}
        )
        self.assertEqual(status, 200)
        self.assertEqual(holdings["holdings"], [{"stock_code": "005930", "quantity": 10, "average_price": 70000.0}])
        status, watchlist = self.request(
            "/v1/watchlist", method="PUT", payload={"symbols": ["000660", "005930", "000660"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(watchlist["watchlist"], ["000660", "005930"])
        self.assertNotIn("111111", json.dumps(watchlist))

        connection = sqlite3.connect(self.store.path)
        try:
            connection.execute(
                "CREATE TRIGGER reject_test_symbol BEFORE INSERT ON watchlist "
                "WHEN NEW.stock_code = '999999' BEGIN SELECT RAISE(ABORT, 'private db detail'); END"
            )
            connection.commit()
        finally:
            connection.close()
        status, failed = self.request("/v1/watchlist", method="PUT", payload={"symbols": ["000660", "999999"]})
        self.assertEqual(status, 500)
        self.assertNotIn("private db detail", json.dumps(failed))
        self.assertEqual(self.request("/v1/state")[1]["watchlist"], ["000660", "005930"])

        self.assertEqual(self.request("/v1/holdings/005930", method="DELETE")[0], 200)
        self.assertEqual(self.request("/v1/holdings/005930", method="DELETE")[1]["holdings"], [])
        restarted = MobileApi(SQLiteUserStateStore(self.store.path), token=TOKEN)
        self.assertEqual(restarted.state_payload()["account_equity_krw"], 1_000_000)
        self.assertEqual(restarted.state_payload()["watchlist"], ["000660", "005930"])

    def test_invalid_path_method_fields_and_json_are_rejected(self) -> None:
        cases = [
            ("/v1/holdings/5930", "PUT", {"quantity": 1, "average_price": 2}, 404),
            ("/v1/state", "POST", None, 405),
            ("/v1/profile", "PUT", {"account_equity_krw": True}, 400),
            ("/v1/profile", "PUT", {"account_equity_krw": float("nan")}, 400),
            ("/v1/profile", "PUT", {"account_equity_krw": float("inf")}, 400),
            ("/v1/profile", "PUT", {"account_equity_krw": 100, "secret": "x"}, 400),
            ("/v1/holdings/005930", "PUT", {"quantity": True, "average_price": 2}, 400),
            ("/v1/holdings/005930", "PUT", {"quantity": 1, "average_price": "2"}, 400),
            ("/v1/watchlist", "PUT", {"symbols": ["005930", "invalid"]}, 400),
        ]
        for path, method, payload, expected in cases:
            with self.subTest(path=path, method=method, payload=payload):
                status, _ = self.request(path, method=method, payload=payload)
                self.assertEqual(status, expected)
        self.assertEqual(
            self.request("/v1/profile", method="PUT", raw_body=b'{"account_equity_krw":NaN}')[0],
            400,
        )
        self.assertEqual(
            self.request("/v1/profile", method="PUT", raw_body=b'{"account_equity_krw":1,"account_equity_krw":2}')[0],
            400,
        )
        self.assertEqual(
            self.request("/v1/profile", method="PUT", payload={"account_equity_krw": 1}, content_type="text/plain")[0],
            415,
        )
        status, _ = self.request("/v1/profile", method="PUT", raw_body=b"x" * (16 * 1024 + 1))
        self.assertEqual(status, 413)
        self.assertIsNone(self.store.get_account_equity(1))

    def test_missing_market_keys_and_provider_failures_are_safe_holds(self) -> None:
        result = self.api.guide_payload("005930")
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIsNone(result["current_price"])
        self.assertEqual(result["market_status"], "unconfigured")

        class FailingFactory:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("secret=do-not-return")

        failing = MobileApi(
            self.store,
            token=TOKEN,
            market_configured=True,
            guide_input_factory=FailingFactory(),
        )
        result = failing.guide_payload("005930")
        self.assertEqual(result["market_status"], "error")
        self.assertEqual(result["action"], "hold")
        self.assertIsNone(result["current_price"])
        self.assertNotIn("do-not-return", result["text"])

    def test_existing_factory_can_return_quote_but_missing_profile_never_suggests_entry(self) -> None:
        now = datetime.now(timezone.utc)
        price_evidence = Evidence(
            "toss_invest_prices", "price", "current_price", 0.5, 0.2, 1,
            "current quote", "https://developers.tossinvest.com/docs",
        )
        market = MarketSnapshot(now, "neutral", 0.4, 0.8, {}, [price_evidence])

        class QuoteFactory:
            called = False

            def __call__(self, user_id, code, intent, holding, at):
                self.called = True
                self.assertion = (user_id, code, intent.approval_level)
                return GuideInput(
                    stock_code=code,
                    market=market,
                    strategy_id="time_series_momentum",
                    strategy_version="1.0.0",
                    strategy_approved=True,
                    signal_score=0.9,
                    signal_confidence=0.9,
                    max_trade_loss_pct=0.5,
                    max_daily_loss_pct=1.5,
                    daily_pnl_pct=0,
                    current_price=12345,
                )

        factory = QuoteFactory()
        configured = MobileApi(
            self.store,
            token=TOKEN,
            market_configured=True,
            guide_input_factory=factory,
        )
        result = configured.guide_payload("005930")
        self.assertTrue(factory.called)
        self.assertEqual(factory.assertion, (1, "005930", "guide_only"))
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["current_price"], 12345)
        self.assertIsNotNone(result["price_as_of"])
        self.assertEqual(result["market_status"], "configured")

    def test_stale_price_evidence_cannot_be_promoted_to_a_guide(self) -> None:
        now = datetime.now(timezone.utc)
        stale = Evidence(
            "toss_invest_prices", "price", "current_price", 0.5, 0.01, 300,
            "old quote", "https://developers.tossinvest.com/docs",
        )
        market = MarketSnapshot(now, "neutral", 0.4, 0.8, {}, [stale])

        class StaleFactory:
            def __call__(self, *args, **kwargs):
                return GuideInput(
                    stock_code="005930", market=market, strategy_id="time_series_momentum",
                    strategy_approved=True, signal_score=0.9, signal_confidence=0.9,
                    max_trade_loss_pct=0.5, max_daily_loss_pct=1.5, daily_pnl_pct=0,
                    current_price=12345,
                )

        configured = MobileApi(
            self.store, token=TOKEN, market_configured=True,
            guide_input_factory=StaleFactory(),
        )
        result = configured.guide_payload("005930")
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["confidence"], 0.0)
        self.assertIsNone(result["current_price"])
        self.assertIsNone(result["price_as_of"])

    def test_demo_seeds_only_when_explicit_and_marks_responses(self) -> None:
        empty_dir = self.directory / "empty-server"
        empty = build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, empty_dir)
        self.assertEqual(empty.state_payload()["holdings"], [])
        demo = build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, self.directory / "demo", demo=True)
        self.assertEqual(demo.state_payload()["mode"], "demo")
        self.assertEqual(demo.state_payload()["holdings"][0]["stock_code"], "005930")
        self.assertTrue(demo.guide_payload("005930")["is_demo"])
        self.assertEqual(demo.guide_payload("005930")["confidence"], 0.0)

    def test_demo_mode_is_persistent_and_seed_is_not_recreated_after_deletion(self) -> None:
        demo_dir = self.directory / "mode-protected-demo"
        demo = build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, demo_dir, demo=True)
        demo.store.delete_holding(1, "005930")
        demo.store.replace_watchlist_symbols(1, [], added_at=datetime.now(timezone.utc))
        connection = sqlite3.connect(demo.store.path)
        try:
            connection.execute("DELETE FROM user_account_profiles WHERE user_id = 1")
            connection.commit()
        finally:
            connection.close()
        restarted_demo = build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, demo_dir, demo=True)
        self.assertEqual(restarted_demo.state_payload()["holdings"], [])
        self.assertEqual(restarted_demo.state_payload()["watchlist"], [])
        self.assertIsNone(restarted_demo.state_payload()["account_equity_krw"])
        with self.assertRaisesRegex(ValueError, "모바일 데이터 폴더 모드"):
            build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, demo_dir, demo=False)

    def test_demo_refuses_preexisting_database_without_mode_marker(self) -> None:
        target = self.directory / "legacy-db-folder"
        target.mkdir()
        connection = sqlite3.connect(target / "existing.sqlite3")
        connection.close()
        with self.assertRaisesRegex(ValueError, "모바일 데이터 폴더 모드"):
            build_mobile_api({"MOBILE_API_TOKEN": TOKEN}, target, demo=True)
        self.assertFalse((target / "users.sqlite3").exists())

    def test_unauthenticated_body_wait_does_not_block_other_http_requests(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        with client:
            client.sendall(
                b"PUT /v1/profile HTTP/1.0\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 1\r\n\r\n"
            )
            response = client.recv(4096)
        self.assertIn(b"401", response)
        self.assertEqual(self.request("/v1/state")[0], 200)

    def test_api_key_validation_happens_before_database_creation(self) -> None:
        target = self.directory / "should-not-exist"
        with self.assertRaises(ValueError):
            build_mobile_api({"MOBILE_API_TOKEN": "short"}, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
