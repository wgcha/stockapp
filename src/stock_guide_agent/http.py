from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen


@dataclass(frozen=True, repr=False)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    form_body: dict[str, str] | None = None
    timeout_seconds: float = 10.0
    sensitive_values: tuple[str, ...] = ()

    def redacted(self) -> dict[str, Any]:
        hidden = {
            "authorization",
            "appkey",
            "secretkey",
            "client_secret",
            "crtfc_key",
            "x-ncp-apigw-api-key-id",
            "x-ncp-apigw-api-key",
            "x-tossinvest-account",
            "x-naver-client-id",
            "x-naver-client-secret",
            "access_token", "refresh_token", "token", "api_key", "client_id",
            "account", "account_number", "account_sequence", "password",
            "text", "chat_id", "user_id",
        }
        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: "***" if key.lower() in hidden else scrub(item)
                        for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                for secret in self.sensitive_values:
                    if secret:
                        value = value.replace(secret, "***")
            return value

        parsed_url = urlsplit(self.url)
        safe_query = urlencode([
            (key, "***" if key.lower() in hidden else scrub(value))
            for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        ])
        redacted_url = scrub(urlunsplit((
            parsed_url.scheme, parsed_url.netloc.rsplit("@", 1)[-1],
            parsed_url.path, safe_query, "",
        )))
        return {
            "method": self.method,
            "url": redacted_url,
            "headers": scrub(self.headers),
            "query": scrub(self.query),
            "json_body": scrub(self.json_body or {}),
            "form_body": scrub(self.form_body or {}),
        }

    def __repr__(self) -> str:
        return f"HttpRequest({self.redacted()!r})"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(repr=False)
    json_body: dict[str, Any] = field(repr=False)


class JsonTransport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


class TransportError(RuntimeError):
    """A deliberately context-free error safe for operational diagnostics."""


def urllib_json_transport(request: HttpRequest) -> HttpResponse:
    try:
        return _urllib_json_transport(request)
    except Exception:
        # urllib/JSON errors may echo credential-bearing URLs and response bodies.
        raise TransportError("HTTP request failed") from None


def _urllib_json_transport(request: HttpRequest) -> HttpResponse:
    query = f"?{urlencode(request.query)}" if request.query else ""
    body = None
    headers = dict(request.headers)
    if request.json_body is not None:
        body = json.dumps(request.json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")
    elif request.form_body is not None:
        body = urlencode(request.form_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    raw_request = Request(
        request.url + query,
        data=body,
        headers=headers,
        method=request.method,
    )
    with urlopen(raw_request, timeout=request.timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return HttpResponse(
            status=response.status,
            headers={key.lower(): value for key, value in response.headers.items()},
            json_body=payload,
        )
