from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

from .execution import AuthorizedOrder, OrderRejected
from .http import HttpRequest, JsonTransport, urllib_json_transport


class TossInvestConfigurationError(RuntimeError):
    pass


class TossInvestApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class TossInvestToken:
    access_token: str = field(repr=False)
    token_type: str
    expires_in_seconds: int | None


@dataclass(frozen=True)
class TossInvestResponse:
    payload: dict[str, Any]


@dataclass(frozen=True)
class TossOrderReceipt:
    simulated: bool
    proposal_id: str
    payload: dict[str, Any]


class TossInvestClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        account_sequence: str | None = None,
        allow_live_orders: bool = False,
        transport: JsonTransport = urllib_json_transport,
    ) -> None:
        if not client_id or not client_secret:
            raise TossInvestConfigurationError("Toss Invest client credentials are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_sequence = account_sequence
        self.allow_live_orders = allow_live_orders
        self.transport = transport
        self.base_url = "https://openapi.tossinvest.com"

    @classmethod
    def from_env(
        cls, *, transport: JsonTransport = urllib_json_transport
    ) -> "TossInvestClient":
        return cls(
            os.environ.get("TOSSINVEST_CLIENT_ID", ""),
            os.environ.get("TOSSINVEST_CLIENT_SECRET", ""),
            account_sequence=os.environ.get("TOSSINVEST_ACCOUNT_SEQ") or None,
            allow_live_orders=os.environ.get("TOSSINVEST_LIVE_ORDERS", "").lower() == "true",
            transport=transport,
        )

    def issue_token(self) -> TossInvestToken:
        response = self.transport(
            HttpRequest(
                method="POST",
                url=f"{self.base_url}/oauth2/token",
                form_body={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
        )
        payload = response.json_body
        token = payload.get("access_token")
        if response.status >= 400 or not token:
            raise TossInvestApiError(_error_message(payload, "token issuance failed"))
        return TossInvestToken(
            access_token=str(token),
            token_type=str(payload.get("token_type", "Bearer")),
            expires_in_seconds=(
                int(payload["expires_in"]) if payload.get("expires_in") is not None else None
            ),
        )

    def get_stocks(self, symbols: list[str], token: TossInvestToken) -> TossInvestResponse:
        if not symbols or any(not symbol for symbol in symbols):
            raise ValueError("at least one non-empty symbol is required")
        return self._get(
            "/api/v1/stocks",
            token,
            query={"symbols": ",".join(symbols)},
        )

    def get_prices(self, symbols: list[str], token: TossInvestToken) -> TossInvestResponse:
        if not symbols or len(symbols) > 200 or any(not symbol for symbol in symbols):
            raise ValueError("symbols must contain between 1 and 200 non-empty values")
        return self._get(
            "/api/v1/prices",
            token,
            query={"symbols": ",".join(symbols)},
        )

    def get_candles(
        self,
        symbol: str,
        token: TossInvestToken,
        *,
        interval: str = "1d",
        count: int = 100,
        adjusted: bool = True,
        before: str | None = None,
    ) -> TossInvestResponse:
        if not symbol:
            raise ValueError("symbol is required")
        if interval not in {"1m", "1d"}:
            raise ValueError("interval must be 1m or 1d")
        if not 1 <= count <= 200:
            raise ValueError("count must be between 1 and 200")
        query = {
            "symbol": symbol,
            "interval": interval,
            "count": str(count),
            "adjusted": str(adjusted).lower(),
        }
        if before:
            query["before"] = before
        return self._get(
            "/api/v1/candles",
            token,
            query=query,
        )

    def get_candle_history(
        self,
        symbol: str,
        token: TossInvestToken,
        *,
        interval: str = "1d",
        max_count: int = 1500,
        adjusted: bool = True,
        max_pages: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        """Follow the official nextBefore cursor and return unique oldest-first bars."""
        if max_count < 1:
            raise ValueError("max_count must be positive")
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")
        unique: dict[str, dict[str, Any]] = {}
        before: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            response = self.get_candles(
                symbol,
                token,
                interval=interval,
                count=min(200, max_count),
                adjusted=adjusted,
                before=before,
            )
            result = response.payload.get("result")
            if not isinstance(result, dict):
                raise TossInvestApiError("candle history result is missing")
            candles = result.get("candles")
            if not isinstance(candles, list):
                raise TossInvestApiError("candle history list is missing")
            for candle in candles:
                if not isinstance(candle, dict) or not candle.get("timestamp"):
                    raise TossInvestApiError("candle history contains an invalid item")
                unique[str(candle["timestamp"])] = candle
            if len(unique) >= max_count:
                break
            cursor = result.get("nextBefore")
            if not cursor:
                break
            cursor_text = str(cursor)
            if cursor_text in seen_cursors or cursor_text == before:
                raise TossInvestApiError("candle history cursor did not advance")
            seen_cursors.add(cursor_text)
            before = cursor_text
        ordered = sorted(unique.values(), key=lambda item: str(item["timestamp"]))
        return tuple(ordered[-max_count:])

    def get_investor_trading(
        self,
        symbol: str,
        token: TossInvestToken,
        *,
        interval: str = "1d",
        count: int = 10,
    ) -> TossInvestResponse:
        if symbol not in {"KOSPI", "KOSDAQ"}:
            raise ValueError("investor trading supports KOSPI or KOSDAQ only")
        if interval not in {"1d", "1w", "1mo", "1y"}:
            raise ValueError("unsupported investor trading interval")
        if not 1 <= count <= 100:
            raise ValueError("count must be between 1 and 100")
        return self._get(
            f"/api/v1/market-indicators/{symbol}/investor-trading",
            token,
            query={"interval": interval, "count": str(count)},
        )

    def get_holdings(
        self, token: TossInvestToken, *, symbol: str | None = None
    ) -> TossInvestResponse:
        return self._get(
            "/api/v1/holdings",
            token,
            query={"symbol": symbol} if symbol else None,
            account_required=True,
        )

    def submit_authorized_order(
        self, order: AuthorizedOrder, token: TossInvestToken
    ) -> TossOrderReceipt:
        proposal = order.proposal
        if proposal.environment == "mock":
            return TossOrderReceipt(
                simulated=True,
                proposal_id=proposal.proposal_id,
                payload={
                    "status": "SIMULATED",
                    "symbol": proposal.stock_code,
                    "side": proposal.side.upper(),
                    "quantity": proposal.quantity,
                    "price": proposal.limit_price,
                },
            )
        raise OrderRejected(
            "Toss Invest live orders are disabled; the program provides guidance only"
        )

    def _get(
        self,
        path: str,
        token: TossInvestToken,
        *,
        query: dict[str, str] | None = None,
        account_required: bool = False,
    ) -> TossInvestResponse:
        response = self.transport(
            HttpRequest(
                method="GET",
                url=f"{self.base_url}{path}",
                headers=self._auth_headers(token, account_required=account_required),
                query=query or {},
            )
        )
        if response.status >= 400:
            raise TossInvestApiError(_error_message(response.json_body, "request failed"))
        return TossInvestResponse(response.json_body)

    def _auth_headers(
        self, token: TossInvestToken, *, account_required: bool
    ) -> dict[str, str]:
        headers = {"Authorization": f"{token.token_type} {token.access_token}"}
        if account_required:
            if not self.account_sequence:
                raise TossInvestConfigurationError(
                    "Toss Invest account sequence is required for account and order APIs"
                )
            headers["X-Tossinvest-Account"] = self.account_sequence
        return headers


def _error_message(payload: dict[str, Any], fallback: str) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code", error.get("errorCode", "unknown"))
        message = error.get("message", error.get("reason", fallback))
        return f"{code}: {message}"
    if isinstance(error, str):
        return f"{error}: {payload.get('error_description', fallback)}"
    return str(payload.get("message", payload.get("error_description", fallback)))


def _decimal_text(value: float) -> str:
    return f"{value:.10f}".rstrip("0").rstrip(".")
