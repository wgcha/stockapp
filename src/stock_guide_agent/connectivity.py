"""Bounded, guide-only checks for the configured external services.

This module deliberately does not construct the application runtime.  Its
network surface is limited to Telegram's identity/webhook metadata endpoints,
Toss OAuth, and the documented Toss price contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from .http import HttpRequest, HttpResponse, JsonTransport, urllib_json_transport


CheckStatus = Literal["ok", "warning", "error"]

_TELEGRAM_HOST = "api.telegram.org"
_TOSS_BASE_URL = "https://openapi.tossinvest.com"


@dataclass(frozen=True)
class ConnectivityCheck:
    check_id: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class ConnectivityReport:
    ready: bool
    checks: tuple[ConnectivityCheck, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def run_connectivity_diagnostic(
    environment: Mapping[str, str],
    *,
    transport: JsonTransport = urllib_json_transport,
) -> ConnectivityReport:
    """Check Telegram identity and webhook metadata without polling or sending."""
    checks: list[ConnectivityCheck] = []
    token = environment.get("TELEGRAM_BOT_TOKEN", "")
    token_valid = isinstance(token, str) and _is_safe_telegram_token(token)
    checks.append(ConnectivityCheck(
        "telegram_token", "ok" if token_valid else "error",
        "TELEGRAM_BOT_TOKEN configured" if token_valid
        else "TELEGRAM_BOT_TOKEN is required and must not contain whitespace or URL delimiters",
    ))
    identity_valid = _validate_telegram_identity_settings(environment, checks)
    if not token_valid or not identity_valid:
        checks.append(ConnectivityCheck(
            "network", "warning", "Network checks skipped until Telegram settings are valid"
        ))
        return _report(checks)

    _telegram_check(transport, token, "getMe", "telegram_identity", checks)
    _telegram_check(transport, token, "getWebhookInfo", "telegram_webhook", checks,
                    webhook=True)
    return _report(checks)


def run_market_connectivity(
    environment: Mapping[str, str], *, transport: JsonTransport = urllib_json_transport,
) -> ConnectivityReport:
    """Verify only Toss OAuth and its documented, read-only Samsung price route.

    This intentionally runs separately from Telegram checks.  It does not use
    account routes or order functions.  The caller should serialize this check
    with its runtime because OAuth token issuance can affect another instance.
    """
    checks: list[ConnectivityCheck] = []
    missing = False
    values: dict[str, str] = {}
    for name, check_id in (
        ("TOSSINVEST_CLIENT_ID", "toss_client_id"),
        ("TOSSINVEST_CLIENT_SECRET", "toss_client_secret"),
    ):
        raw_value = environment.get(name, "")
        value = raw_value.strip() if isinstance(raw_value, str) else ""
        values[name] = value
        configured = bool(value)
        missing = missing or not configured
        checks.append(ConnectivityCheck(
            check_id, "ok" if configured else "error",
            f"{name} configured" if configured else f"{name} is required",
        ))
    if missing:
        checks.append(ConnectivityCheck(
            "network", "warning", "Network checks skipped until Toss credentials are configured"
        ))
        return _report(checks)
    token = _toss_token_check(
        transport, values["TOSSINVEST_CLIENT_ID"], values["TOSSINVEST_CLIENT_SECRET"], checks
    )
    if token is not None:
        _toss_market_check(transport, token[0], token[1], checks)
    else:
        checks.append(ConnectivityCheck(
            "toss_market", "warning", "Market check skipped because Toss authentication was not verified"
        ))
    return _report(checks)


def _report(checks: list[ConnectivityCheck]) -> ConnectivityReport:
    return ConnectivityReport(
        ready=not any(check.status == "error" for check in checks),
        checks=tuple(checks),
    )


def _telegram_check(
    transport: JsonTransport,
    token: str,
    method: str,
    check_id: str,
    checks: list[ConnectivityCheck],
    *,
    webhook: bool = False,
) -> None:
    url = f"https://{_TELEGRAM_HOST}/bot{token}/{method}"
    if not _is_trusted_telegram_url(url):
        checks.append(ConnectivityCheck(check_id, "error", "Telegram endpoint is not trusted"))
        return
    response = _request(transport, HttpRequest(
        method="GET", url=url, timeout_seconds=5.0, sensitive_values=(token,)
    ))
    if response is None or response.json_body.get("ok") is not True:
        checks.append(ConnectivityCheck(check_id, "error", "Telegram connectivity check failed"))
        return
    if webhook:
        result = response.json_body.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            checks.append(ConnectivityCheck(check_id, "error", "Telegram webhook metadata is malformed"))
            return
        has_webhook = bool(result["url"])
        checks.append(ConnectivityCheck(
            check_id,
            "error" if has_webhook else "ok",
            "Webhook is configured; polling would conflict" if has_webhook
            else "No webhook configured; polling is not blocked by a webhook",
        ))
        return
    result = response.json_body.get("result")
    if (
        not isinstance(result, dict)
        or isinstance(result.get("id"), bool)
        or not isinstance(result.get("id"), int)
        or result["id"] <= 0
        or result.get("is_bot") is not True
    ):
        checks.append(ConnectivityCheck(check_id, "error", "Telegram bot identity response is malformed"))
        return
    checks.append(ConnectivityCheck(check_id, "ok", "Telegram bot identity verified"))


def _toss_token_check(
    transport: JsonTransport, client_id: str, client_secret: str,
    checks: list[ConnectivityCheck],
) -> tuple[str, str] | None:
    response = _request(transport, HttpRequest(
        method="POST", url=f"{_TOSS_BASE_URL}/oauth2/token",
        form_body={"grant_type": "client_credentials", "client_id": client_id,
                   "client_secret": client_secret},
        timeout_seconds=5.0, sensitive_values=(client_id, client_secret),
    ))
    access_token = response.json_body.get("access_token") if response else None
    token_type = response.json_body.get("token_type") if response else None
    if (
        response is None
        or not isinstance(access_token, str)
        or not _is_safe_access_token(access_token)
        or token_type != "Bearer"
    ):
        checks.append(ConnectivityCheck("toss_auth", "error", "Toss authentication check failed"))
        return None
    checks.append(ConnectivityCheck("toss_auth", "ok", "Toss authentication verified"))
    return (token_type, access_token)


def _toss_market_check(
    transport: JsonTransport, token_type: str, access_token: str,
    checks: list[ConnectivityCheck],
) -> None:
    response = _request(transport, HttpRequest(
        method="GET", url=f"{_TOSS_BASE_URL}/api/v1/prices",
        headers={"Authorization": f"{token_type} {access_token}"},
        query={"symbols": "005930"}, timeout_seconds=5.0,
        sensitive_values=(access_token,),
    ))
    if response is None or not _has_price_contract(response.json_body):
        checks.append(ConnectivityCheck("toss_market", "error", "Toss market check failed"))
        return
    checks.append(ConnectivityCheck("toss_market", "ok", "Toss price endpoint and response contract verified"))


def _has_price_contract(payload: dict[str, Any]) -> bool:
    """Validate only the documented fields; do not retain or display a quote."""
    result = payload.get("result")
    if not isinstance(result, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("symbol") == "005930"
        and item.get("currency") == "KRW"
        and _is_aware_iso_timestamp(item.get("timestamp"))
        and _is_positive_finite_number(item.get("lastPrice"))
        for item in result
    )


def _request(transport: JsonTransport, request: HttpRequest) -> HttpResponse | None:
    try:
        response = transport(request)
    except Exception:
        # Transports can include URLs, headers, or server responses in errors.
        return None
    if (
        not isinstance(response, HttpResponse)
        or not 200 <= response.status < 300
        or not isinstance(response.json_body, dict)
    ):
        return None
    return response


def _validate_telegram_identity_settings(
    environment: Mapping[str, str], checks: list[ConnectivityCheck],
) -> bool:
    owner = _positive_decimal(environment.get("TELEGRAM_OWNER_USER_ID", ""))
    chat_raw = environment.get("TELEGRAM_OWNER_CHAT_ID", "")
    chat = owner if not chat_raw else _positive_decimal(chat_raw)
    allowed_raw = environment.get("TELEGRAM_ALLOWED_USER_IDS", "")
    allowed_values = allowed_raw.split(",") if isinstance(allowed_raw, str) and allowed_raw else []
    allowed_valid = isinstance(allowed_raw, str) and all(
        _positive_decimal(value.strip()) is not None for value in allowed_values
    )
    owner_valid = owner is not None and chat == owner
    checks.append(ConnectivityCheck(
        "telegram_owner", "ok" if owner_valid else "error",
        "Telegram owner and private chat configured" if owner_valid
        else "Telegram owner must be positive and owner chat must match",
    ))
    checks.append(ConnectivityCheck(
        "telegram_allowed_users", "ok" if allowed_valid else "error",
        "Telegram allowed user IDs are valid" if allowed_valid
        else "Telegram allowed user IDs must be comma-separated positive integers",
    ))
    return owner_valid and allowed_valid


def _positive_decimal(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _is_safe_telegram_token(value: str) -> bool:
    return bool(value) and not any(
        character.isspace() or character in "/?#" for character in value
    )


def _is_safe_access_token(value: str) -> bool:
    return bool(value) and "\r" not in value and "\n" not in value


def _is_positive_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        parsed = float(value)  # API may return a JSON number or numeric string.
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0


def _is_aware_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_trusted_telegram_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == _TELEGRAM_HOST and parsed.port is None
