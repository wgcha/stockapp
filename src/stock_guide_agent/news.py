from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import os
import re
from urllib.parse import urlparse

from .http import HttpRequest, JsonTransport, urllib_json_transport


@dataclass(frozen=True)
class NewsArticle:
    title: str
    url: str
    published_at: datetime
    description: str = ""


@dataclass(frozen=True)
class MaterialNewsSignal:
    normalized_score: float
    observed_at: datetime
    summary: str
    reference_urls: tuple[str, ...]


class NaverNewsHubClient:
    BASE_URL = "https://naverapihub.apigw.ntruss.com"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: JsonTransport = urllib_json_transport,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("NAVER API HUB credentials are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.transport = transport

    @classmethod
    def from_env(cls) -> "NaverNewsHubClient":
        return cls(
            os.environ.get("NAVER_API_HUB_CLIENT_ID", ""),
            os.environ.get("NAVER_API_HUB_CLIENT_SECRET", ""),
        )

    def search(self, query: str, *, display: int = 10) -> list[NewsArticle]:
        if not query.strip():
            raise ValueError("news query is required")
        if not 1 <= display <= 100:
            raise ValueError("display must be between 1 and 100")
        response = self.transport(
            HttpRequest(
                "GET",
                self.BASE_URL + "/search/v1/news",
                headers={
                    "X-NCP-APIGW-API-KEY-ID": self.client_id,
                    "X-NCP-APIGW-API-KEY": self.client_secret,
                },
                query={
                    "query": query,
                    "display": str(display),
                    "start": "1",
                    "sort": "date",
                    "format": "json",
                },
                sensitive_values=(self.client_id, self.client_secret),
            )
        )
        if response.status >= 400:
            raise RuntimeError("NAVER API HUB news request failed")
        items = response.json_body.get("items")
        if not isinstance(items, list):
            raise ValueError("NAVER news response items must be an array")
        result: list[NewsArticle] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("originallink") or item.get("link") or "")
            try:
                published = parsedate_to_datetime(str(item["pubDate"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not url or published.tzinfo is None or published.utcoffset() is None:
                continue
            result.append(
                NewsArticle(
                    _clean_html(str(item.get("title", ""))),
                    url,
                    published,
                    _clean_html(str(item.get("description", ""))),
                )
            )
        return result


_NEGATIVE_TERMS = (
    "상장폐지",
    "횡령",
    "배임",
    "회생절차",
    "부도",
    "거래정지",
    "감사의견 거절",
)
_POSITIVE_TERMS = (
    "자사주 소각",
    "대규모 공급계약",
    "신약 승인",
)
_NEGATION_TERMS = ("아니다", "해소", "철회", "무혐의", "사실무근")


def score_material_news(
    articles: list[NewsArticle], *, minimum_publishers: int = 2
) -> MaterialNewsSignal | None:
    """Emit only low-weight, independently corroborated factual-event signals."""
    if minimum_publishers < 2:
        raise ValueError("minimum_publishers cannot be lower than two")
    classified: dict[int, list[NewsArticle]] = {-1: [], 1: []}
    seen: set[tuple[int, str]] = set()
    for article in articles:
        text = f"{article.title} {article.description}"
        if any(term in text for term in _NEGATION_TERMS):
            continue
        direction = 0
        if any(term in text for term in _NEGATIVE_TERMS):
            direction = -1
        elif any(term in text for term in _POSITIVE_TERMS):
            direction = 1
        host = (urlparse(article.url).hostname or "").lower()
        if direction and host and (direction, host) not in seen:
            classified[direction].append(article)
            seen.add((direction, host))

    negative = classified[-1]
    positive = classified[1]
    if len(negative) >= minimum_publishers and len(positive) >= minimum_publishers:
        return None
    selected = negative if len(negative) >= minimum_publishers else positive
    if len(selected) < minimum_publishers:
        return None
    direction = -1.0 if selected is negative else 1.0
    selected = sorted(selected, key=lambda item: item.published_at, reverse=True)
    return MaterialNewsSignal(
        normalized_score=0.35 * direction,
        observed_at=selected[0].published_at,
        summary=" / ".join(item.title for item in selected[:3]),
        reference_urls=tuple(item.url for item in selected[:3]),
    )


def _clean_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(value)).strip()
