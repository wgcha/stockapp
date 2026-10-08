from __future__ import annotations

from typing import Any

from .cache import CachedPayload, SQLiteMarketCache
from .market import MarketObservation


_NEGATIVE_DISCLOSURE_TERMS = (
    "상장폐지",
    "회생절차",
    "파산",
    "횡령",
    "배임",
    "영업정지",
    "불성실공시",
    "감사의견거절",
    "유상증자결정",
)
_POSITIVE_DISCLOSURE_TERMS = (
    "자기주식취득결정",
    "현금ㆍ현물배당결정",
    "단일판매ㆍ공급계약체결",
)


class CachedSupplementalObservationProvider:
    """Convert only auditable cached scores and material disclosures into evidence."""

    def __init__(self, cache: SQLiteMarketCache) -> None:
        self.cache = cache

    def __call__(self, stock_code: str) -> list[MarketObservation]:
        observations: list[MarketObservation] = []
        for category in ("macro", "news", "overseas"):
            for item in self.cache.list_category(category):
                observation = _scored_observation(item, stock_code)
                if observation is not None:
                    observations.append(observation)
        for item in self.cache.list_category("disclosure"):
            observation = _disclosure_observation(item, stock_code)
            if observation is not None:
                observations.append(observation)
        return observations


def _scored_observation(
    item: CachedPayload, stock_code: str
) -> MarketObservation | None:
    payload = item.payload
    stock_codes = payload.get("stock_codes")
    if isinstance(stock_codes, list) and stock_code not in {
        str(value) for value in stock_codes
    }:
        return None
    value = payload.get("normalized_score")
    if not isinstance(value, (int, float)) or not -1 <= float(value) <= 1:
        return None
    reference_url = payload.get("reference_url")
    if item.category in {"news", "macro"} and not reference_url:
        return None
    tier = int(payload.get("source_tier", 2))
    if tier not in {1, 2, 3, 4}:
        return None
    return MarketObservation(
        source_id=item.source_id,
        category=item.category,  # type: ignore[arg-type]
        metric=str(payload.get("metric", item.cache_key)),
        value=float(value),
        observed_at=item.observed_at,
        fetched_at=item.fetched_at,
        source_tier=tier,  # type: ignore[arg-type]
        official=bool(payload.get("official", False)),
        summary=str(payload.get("summary", "")),
        reference_url=str(reference_url) if reference_url else None,
    )


def _disclosure_observation(
    item: CachedPayload, stock_code: str
) -> MarketObservation | None:
    payload: dict[str, Any] = item.payload
    if str(payload.get("stock_code") or "") != stock_code:
        return None
    name = str(payload.get("report_name", ""))
    if any(term in name for term in _NEGATIVE_DISCLOSURE_TERMS):
        value = -0.8
        metric = "material_negative_disclosure"
    elif any(term in name for term in _POSITIVE_DISCLOSURE_TERMS):
        value = 0.45
        metric = "material_positive_disclosure"
    else:
        return None
    return MarketObservation(
        source_id="open_dart",
        category="disclosure",
        metric=metric,
        value=value,
        observed_at=item.observed_at,
        fetched_at=item.fetched_at,
        source_tier=1,
        official=True,
        summary=name,
        reference_url=str(payload.get("detail_url") or "") or None,
    )
