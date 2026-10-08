from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .http import HttpRequest, JsonTransport, urllib_json_transport


MARKET_TERMS = ("trading", "portfolio", "stock", "equity", "asset", "market")
METHOD_TERMS = (
    "momentum",
    "factor",
    "volatility",
    "algorithm",
    "machine learning",
    "strategy",
    "signal",
    "mean reversion",
    "trend",
)


@dataclass(frozen=True)
class AcademicStrategyCandidate:
    doi: str
    title: str
    published_on: date
    landing_url: str
    authors: tuple[str, ...]
    matched_terms: tuple[str, ...]
    source: str = "crossref"

    def __post_init__(self) -> None:
        if not self.doi or not self.title or not self.landing_url:
            raise ValueError("academic candidate identity is required")
        if not self.matched_terms:
            raise ValueError("academic candidate must have relevance evidence")


class CrossrefAcademicDiscoveryClient:
    """Low-cost metadata discovery; candidates never become executable here."""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        transport: JsonTransport = urllib_json_transport,
        base_url: str = "https://api.crossref.org/v1",
    ) -> None:
        self.mailto = mailto.strip() if mailto else None
        self.transport = transport
        self.base_url = base_url.rstrip("/")

    def discover(
        self, *, since: date, rows: int = 20
    ) -> tuple[AcademicStrategyCandidate, ...]:
        if not 1 <= rows <= 100:
            raise ValueError("Crossref rows must be between 1 and 100")
        query = {
            "query.title": (
                "algorithmic trading portfolio momentum factor volatility "
                "mean reversion trend strategy"
            ),
            "filter": f"from-pub-date:{since.isoformat()},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": str(rows),
            "select": "DOI,title,published,published-online,published-print,author,URL",
        }
        if self.mailto:
            query["mailto"] = self.mailto
        response = self.transport(
            HttpRequest(
                "GET",
                f"{self.base_url}/works",
                headers={
                    "User-Agent": (
                        "stock-guide-agent/0.1"
                        + (f" (mailto:{self.mailto})" if self.mailto else "")
                    )
                },
                query=query,
            )
        )
        if response.status >= 400:
            raise RuntimeError(f"Crossref discovery failed with HTTP {response.status}")
        message = response.json_body.get("message")
        items = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items, list):
            raise ValueError("Crossref works response has no items array")
        candidates: dict[str, AcademicStrategyCandidate] = {}
        for item in items:
            parsed = _parse_candidate(item)
            if parsed is not None:
                candidates.setdefault(parsed.doi.lower(), parsed)
        return tuple(candidates.values())


def _parse_candidate(item: Any) -> AcademicStrategyCandidate | None:
    if not isinstance(item, dict):
        return None
    doi = str(item.get("DOI") or "").strip()
    titles = item.get("title")
    title = str(titles[0]).strip() if isinstance(titles, list) and titles else ""
    lowered = title.casefold()
    market = tuple(term for term in MARKET_TERMS if term in lowered)
    methods = tuple(term for term in METHOD_TERMS if term in lowered)
    if not doi or not title or not market or not methods:
        return None
    published_on = _published_date(item)
    if published_on is None:
        return None
    authors = []
    for author in item.get("author", []):
        if not isinstance(author, dict):
            continue
        name = " ".join(
            value.strip()
            for value in (str(author.get("given") or ""), str(author.get("family") or ""))
            if value.strip()
        )
        if name:
            authors.append(name)
    return AcademicStrategyCandidate(
        doi=doi,
        title=title,
        published_on=published_on,
        landing_url=str(item.get("URL") or f"https://doi.org/{doi}"),
        authors=tuple(authors[:20]),
        matched_terms=tuple(dict.fromkeys((*market, *methods))),
    )


def _published_date(item: dict[str, Any]) -> date | None:
    for field in ("published-print", "published-online", "published"):
        value = item.get(field)
        parts = value.get("date-parts") if isinstance(value, dict) else None
        first = parts[0] if isinstance(parts, list) and parts else None
        if not isinstance(first, list) or not first:
            continue
        try:
            year = int(first[0])
            month = int(first[1]) if len(first) > 1 else 1
            day = int(first[2]) if len(first) > 2 else 1
            return date(year, month, day)
        except (TypeError, ValueError):
            continue
    return None
