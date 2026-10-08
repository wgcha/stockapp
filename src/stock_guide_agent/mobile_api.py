"""Small, Telegram-independent JSON API for the private Android client."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .env_file import EnvFileError, load_environment_file
from .execution import ExecutionState
from .guidance import GuideInput, format_trade_guide, generate_trade_guide
from .intent import Allocation, InvestmentIntent
from .market import MAX_AGE_SECONDS
from .state import Holding, SQLiteUserStateStore


MAX_BODY_BYTES = 16 * 1024
_SYMBOL = re.compile(r"^[0-9]{6}$")
_HOLDING_PATH = re.compile(r"^/v1/holdings/([0-9]{6})$")
_GUIDE_PATH = re.compile(r"^/v1/guides/([0-9]{6})$")


class MobileApi:
    def __init__(
        self,
        store: SQLiteUserStateStore,
        *,
        token: str,
        owner_user_id: int = 1,
        market_configured: bool = False,
        demo: bool = False,
        guide_input_factory: Callable[..., GuideInput] | None = None,
        strategy_store: Any | None = None,
    ) -> None:
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("모바일 API 키는 32자 이상이어야 합니다.")
        if owner_user_id <= 0:
            raise ValueError("소유자 ID 설정이 올바르지 않습니다.")
        self.store = store
        self.token = token
        self.owner_user_id = owner_user_id
        self.market_configured = market_configured
        self.demo = demo
        self.guide_input_factory = guide_input_factory
        self.strategy_store = strategy_store

    def handle(
        self,
        method: str,
        path: str,
        authorization: str | None,
        body: bytes,
        content_type: str | None,
    ) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(path)
        route = parsed.path
        if route == "/health":
            if method != "GET" or parsed.query or parsed.fragment:
                return _error(405, "method_not_allowed", "요청한 기능을 지원하지 않습니다.")
            return 200, {"status": "ok", "api_version": 1}
        if not self._authorized(authorization):
            return _error(401, "unauthorized", "인증 정보를 확인해주세요.")
        if parsed.query or parsed.fragment:
            return _error(400, "invalid_path", "요청 경로가 올바르지 않습니다.")

        if route == "/v1/state" and method == "GET":
            return 200, self.state_payload()
        if route == "/v1/profile" and method == "PUT":
            payload, error = _read_payload(body, content_type)
            if error:
                return error
            if not _has_only(payload, {"account_equity_krw"}):
                return _error(400, "invalid_fields", "입력 항목을 확인해주세요.")
            value = payload.get("account_equity_krw")
            if not _finite_number(value) or not 0 < value <= 1e15:
                return _error(400, "invalid_value", "투자금은 0보다 크고 허용 범위 이내여야 합니다.")
            try:
                self.store.set_account_equity(
                    self.owner_user_id, float(value), updated_at=_now()
                )
            except Exception:
                return _error(500, "storage_error", "저장하지 못했습니다. 잠시 후 다시 시도해주세요.")
            return 200, self.state_payload()

        holding_match = _HOLDING_PATH.fullmatch(route)
        if holding_match and method == "PUT":
            payload, error = _read_payload(body, content_type)
            if error:
                return error
            if not _has_only(payload, {"quantity", "average_price"}):
                return _error(400, "invalid_fields", "입력 항목을 확인해주세요.")
            quantity = payload.get("quantity")
            average_price = payload.get("average_price")
            if type(quantity) is not int or not 1 <= quantity <= 1_000_000_000:
                return _error(400, "invalid_value", "보유 수량은 허용 범위의 정수여야 합니다.")
            if not _finite_number(average_price) or not 0 < average_price <= 1e12:
                return _error(400, "invalid_value", "평단은 0보다 크고 허용 범위 이내여야 합니다.")
            try:
                self.store.upsert_holding(
                    Holding(
                        user_id=self.owner_user_id,
                        stock_code=holding_match.group(1),
                        quantity=quantity,
                        average_price=float(average_price),
                        updated_at=_now(),
                    )
                )
            except Exception:
                return _error(500, "storage_error", "저장하지 못했습니다. 잠시 후 다시 시도해주세요.")
            return 200, self.state_payload()
        if holding_match and method == "DELETE":
            try:
                self.store.delete_holding(self.owner_user_id, holding_match.group(1))
            except Exception:
                return _error(500, "storage_error", "저장된 정보를 변경하지 못했습니다.")
            return 200, self.state_payload()

        if route == "/v1/watchlist" and method == "PUT":
            payload, error = _read_payload(body, content_type)
            if error:
                return error
            if not _has_only(payload, {"symbols"}):
                return _error(400, "invalid_fields", "입력 항목을 확인해주세요.")
            symbols = payload.get("symbols")
            if (
                not isinstance(symbols, list)
                or len(symbols) > 50
                or any(not isinstance(value, str) or not _SYMBOL.fullmatch(value) for value in symbols)
            ):
                return _error(400, "invalid_value", "관심 종목은 숫자 6자리 코드 최대 50개로 입력해주세요.")
            try:
                self.store.replace_watchlist_symbols(
                    self.owner_user_id, list(dict.fromkeys(symbols)), added_at=_now()
                )
            except Exception:
                return _error(500, "storage_error", "저장하지 못했습니다. 잠시 후 다시 시도해주세요.")
            return 200, self.state_payload()

        guide_match = _GUIDE_PATH.fullmatch(route)
        if guide_match and method == "GET":
            return 200, self.guide_payload(guide_match.group(1))

        if route in {"/v1/state", "/v1/profile", "/v1/watchlist"} or holding_match or guide_match:
            return _error(405, "method_not_allowed", "요청한 기능을 지원하지 않습니다.")
        return _error(404, "not_found", "요청한 주소를 찾을 수 없습니다.")

    def _authorized(self, header: str | None) -> bool:
        candidate = ""
        if isinstance(header, str) and header.startswith("Bearer "):
            candidate = header[7:]
        return hmac.compare_digest(candidate.encode("utf-8"), self.token.encode("utf-8"))

    def is_authorized(self, header: str | None) -> bool:
        return self._authorized(header)

    def state_payload(self) -> dict[str, Any]:
        holdings = self.store.list_holdings(self.owner_user_id)
        configured = self.market_configured and not self.demo
        return {
            "mode": "demo" if self.demo else "server",
            "guide_only": True,
            "account_equity_krw": self.store.get_account_equity(self.owner_user_id),
            "holdings": [
                {
                    "stock_code": item.stock_code,
                    "quantity": item.quantity,
                    "average_price": item.average_price,
                }
                for item in holdings
            ],
            "watchlist": self.store.list_watchlist_symbols(self.owner_user_id),
            "market": {
                "status": "configured" if configured else "unconfigured",
                "message": (
                    "시세 연결 설정이 있습니다. 실제 연결 상태는 가이드 요청 때 확인합니다."
                    if configured
                    else "시세 연결 정보가 없습니다."
                ),
            },
        }

    def guide_payload(self, stock_code: str) -> dict[str, Any]:
        now = _now()
        base = {
            "stock_code": stock_code,
            "action": "hold",
            "label": "유지 / 대기",
            "confidence": 0.0,
            "text": "시세 또는 투자 설정 자료가 부족해 판단을 보류했습니다.",
            "current_price": None,
            "price_as_of": None,
            "generated_at": now.isoformat(),
            "market_status": "unconfigured",
            "is_demo": self.demo,
        }
        if self.demo:
            base["text"] = "데모 자료입니다. 실제 시세가 연결되지 않아 판단을 보류했습니다."
            return base
        if not self.market_configured or self.guide_input_factory is None:
            return base

        intent = self.store.load_intent(self.owner_user_id, as_of=now)
        missing_intent = intent is None
        if intent is None:
            # A missing app profile must never imply permission or allocation.
            intent = InvestmentIntent(
                raw_text="모바일 가이드 조회 전용 기본 설정",
                allocations=[Allocation(strategy="swing", weight_pct=0.0)],
                approval_level="guide_only",
            )
        try:
            value = self.guide_input_factory(
                self.owner_user_id,
                stock_code,
                intent,
                self.store.get_holding(self.owner_user_id, stock_code),
                now,
            )
            price_evidence = next(
                (
                    item
                    for item in value.market.evidence
                    if item.category == "price"
                    and item.source_id == "toss_invest_prices"
                    and 0 <= item.age_seconds < MAX_AGE_SECONDS["price"]
                ),
                None,
            )
            if (
                price_evidence is None
                or not _finite_number(value.current_price)
                or value.current_price <= 0
            ):
                return {
                    **base,
                    "text": "유효한 최신 시세를 확인하지 못해 판단을 보류했습니다.",
                    "market_status": "configured",
                }
            if missing_intent:
                # No saved allocation may produce a market quote, never a suggested entry.
                value = replace(value, strategy_allocation_pct=0.0)
            guide = generate_trade_guide(value, now=now)
            if not _finite_number(guide.confidence) or not 0 <= guide.confidence <= 1:
                return {
                    **base,
                    "text": "시세 판단 자료를 확인하지 못해 판단을 보류했습니다.",
                    "market_status": "error",
                }
            return {
                **base,
                "action": guide.action,
                "label": _action_label(guide.action),
                "confidence": guide.confidence,
                "text": format_trade_guide(guide),
                "current_price": value.current_price,
                "price_as_of": (
                    value.market.as_of - timedelta(seconds=price_evidence.age_seconds)
                ).isoformat(),
                "market_status": "configured",
            }
        except Exception:
            # Provider errors and payload details can contain credentials or account data.
            return {
                **base,
                "text": "시세 정보를 확인하지 못해 판단을 보류했습니다.",
                "market_status": "error",
            }


def make_handler(api: MobileApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MobileGuideAPI/1"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5.0)

        def _dispatch(self) -> None:
            body = b""
            content_type = self.headers.get("Content-Type")
            public_health = self.command == "GET" and urlsplit(self.path).path == "/health"
            if not public_health and not api.is_authorized(self.headers.get("Authorization")):
                self._write(*_error(401, "unauthorized", "인증 정보를 확인해주세요."))
                return
            if self.headers.get("Transfer-Encoding") is not None:
                status, payload = _error(400, "invalid_body", "요청 형식을 확인해주세요.")
                self._write(status, payload)
                return
            length = self.headers.get("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    status, payload = _error(400, "invalid_length", "요청 크기를 확인해주세요.")
                    self._write(status, payload)
                    return
                if len(length) > 5:
                    status, payload = _error(413, "body_too_large", "요청 내용이 너무 큽니다.")
                    self._write(status, payload)
                    return
                size = int(length)
                if size > MAX_BODY_BYTES:
                    status, payload = _error(413, "body_too_large", "요청 내용이 너무 큽니다.")
                    self._write(status, payload)
                    return
                if size and self.command not in {"PUT", "POST", "PATCH"}:
                    status, payload = _error(400, "invalid_body", "요청 형식을 확인해주세요.")
                    self._write(status, payload)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    status, payload = _error(400, "invalid_body", "요청 형식을 확인해주세요.")
                    self._write(status, payload)
                    return
            try:
                status, payload = api.handle(
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    body,
                    content_type,
                )
            except Exception:
                status, payload = _error(500, "internal_error", "요청을 처리하지 못했습니다.")
            self._write(status, payload)

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_POST = _dispatch
        do_PATCH = _dispatch
        do_OPTIONS = _dispatch

        def __getattr__(self, name: str) -> Any:
            if name.startswith("do_"):
                return self._dispatch
            raise AttributeError(name)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def build_mobile_api(
    environment: Mapping[str, str], data_dir: str | Path, *, demo: bool = False
) -> MobileApi:
    token = environment.get("MOBILE_API_TOKEN", "").strip()
    if len(token) < 32:
        raise ValueError("모바일 API 키는 32자 이상이어야 합니다.")
    owner_text = environment.get("MOBILE_OWNER_USER_ID", "1")
    if not owner_text.isascii() or not owner_text.isdecimal() or int(owner_text) <= 0:
        raise ValueError("소유자 ID 설정이 올바르지 않습니다.")
    owner_id = int(owner_text)
    directory = Path(data_dir)
    _ensure_data_mode(directory, "demo" if demo else "server")
    user_store = SQLiteUserStateStore(directory / "users.sqlite3")
    if demo:
        user_store.seed_mobile_demo_once(owner_id, seeded_at=_now())

    token = token.strip()
    strategy_store = None
    factory = None
    client_id = environment.get("TOSSINVEST_CLIENT_ID", "").strip()
    client_secret = environment.get("TOSSINVEST_CLIENT_SECRET", "").strip()
    market_configured = bool(client_id and client_secret)
    if market_configured and not demo:
        from .cache import SQLiteMarketCache
        from .strategy_store import SQLiteStrategyStore
        from .supplemental import CachedSupplementalObservationProvider
        from .tokens import TossTokenManager
        from .toss import TossInvestClient
        from .toss_market import TossGuideInputFactory

        strategy_store = SQLiteStrategyStore(directory / "strategies.sqlite3")
        cache = SQLiteMarketCache(directory / "market_cache.sqlite3")
        client = TossInvestClient(
            client_id,
            client_secret,
            account_sequence=environment.get("TOSSINVEST_ACCOUNT_SEQ") or None,
            allow_live_orders=False,
        )
        execution_state = ExecutionState()
        factory = TossGuideInputFactory(
            client,
            TossTokenManager(client),
            execution_state,
            strategy_id="time_series_momentum",
            strategy_version="1.0.0",
            strategy_approved=lambda: strategy_store.is_approved("time_series_momentum", "1.0.0"),
            supplemental_observations=CachedSupplementalObservationProvider(cache),
            alternative_symbols=user_store.list_candidate_symbols,
            long_term_strategy_id="long_term_absolute_momentum",
            long_term_strategy_version="1.0.0",
            long_term_strategy_approved=lambda: strategy_store.is_approved("long_term_absolute_momentum", "1.0.0"),
        )
    return MobileApi(
        user_store,
        token=token,
        owner_user_id=owner_id,
        market_configured=market_configured,
        demo=demo,
        guide_input_factory=factory,
        strategy_store=strategy_store,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Telegram-independent Android guide API")
    parser.add_argument("--env-file", default=".env.mobile")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    data_dir = args.data_dir or ("data/mobile-demo" if args.demo else "data/mobile")
    try:
        load_environment_file(args.env_file)
        api = build_mobile_api(os.environ, data_dir, demo=args.demo)
        server = ThreadingHTTPServer((args.host, args.port), make_handler(api))
    except (EnvFileError, OSError, ValueError):
        raise SystemExit("모바일 API 설정을 확인해주세요.") from None
    mode = "demo" if args.demo else "server"
    print(f"Mobile API listening at http://{args.host}:{server.server_port} mode={mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _read_payload(
    body: bytes, content_type: str | None
) -> tuple[dict[str, Any], tuple[int, dict[str, Any]] | None]:
    if content_type is None or content_type.split(";", 1)[0].strip().lower() != "application/json":
        return {}, _error(415, "unsupported_media_type", "JSON 형식으로 보내주세요.")
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return {}, _error(400, "invalid_json", "요청 내용을 확인해주세요.")
    if not isinstance(payload, dict):
        return {}, _error(400, "invalid_json", "요청 내용은 JSON 객체여야 합니다.")
    return payload, None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _has_only(payload: dict[str, Any], fields: set[str]) -> bool:
    return bool(payload) and set(payload).issubset(fields)


def _finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_data_mode(directory: Path, mode: str) -> None:
    marker = directory / ".mobile-api-mode"
    expected = f"{mode}-v1\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if marker.is_symlink():
            raise ValueError
        if marker.exists():
            stored = marker.read_text(encoding="ascii")
            if stored != expected:
                raise ValueError
            return
        if any(
            item.is_file() and item.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
            for item in directory.iterdir()
        ):
            raise ValueError
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, expected.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        try:
            if marker.is_symlink() or marker.read_text(encoding="ascii") != expected:
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            raise ValueError("모바일 데이터 폴더 모드를 확인해주세요.") from None
    except (OSError, UnicodeError, ValueError):
        raise ValueError("모바일 데이터 폴더 모드를 확인해주세요.") from None


def _action_label(action: str) -> str:
    return {
        "buy": "분할 매수",
        "hold": "유지 / 대기",
        "partial_sell": "일부 매도",
        "full_sell": "전량 매도",
        "review_alternative": "다른 종목 검토",
        "stop_trading": "거래 중단",
    }.get(action, "유지 / 대기")


if __name__ == "__main__":
    main()
