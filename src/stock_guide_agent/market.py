from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from statistics import pstdev
from typing import Literal


Category = Literal[
    "price",
    "investor_flow",
    "macro",
    "disclosure",
    "news",
    "overseas",
]
Regime = Literal["risk_on", "neutral", "risk_off", "insufficient_data"]
SourceTier = Literal[1, 2, 3, 4]


CATEGORY_WEIGHTS: dict[Category, float] = {
    "price": 0.25,
    "investor_flow": 0.25,
    "macro": 0.15,
    "disclosure": 0.10,
    "news": 0.10,
    "overseas": 0.15,
}

MAX_AGE_SECONDS: dict[Category, int] = {
    "price": 5 * 60,
    "investor_flow": 60 * 60,
    "macro": 72 * 60 * 60,
    "disclosure": 30 * 60,
    "news": 60 * 60,
    "overseas": 60 * 60,
}

TIER_QUALITY: dict[SourceTier, float] = {1: 1.0, 2: 0.85, 3: 0.65, 4: 0.40}


@dataclass(frozen=True)
class MarketObservation:
    source_id: str
    category: Category
    metric: str
    value: float
    observed_at: datetime
    fetched_at: datetime
    source_tier: SourceTier
    official: bool = False
    summary: str = ""
    reference_url: str | None = None

    def __post_init__(self) -> None:
        if not -1.0 <= self.value <= 1.0:
            raise ValueError("value must be normalized to the range [-1, 1]")
        for name, value in (
            ("observed_at", self.observed_at),
            ("fetched_at", self.fetched_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.fetched_at < self.observed_at:
            raise ValueError("fetched_at cannot be earlier than observed_at")


@dataclass(frozen=True)
class Evidence:
    source_id: str
    category: Category
    metric: str
    value: float
    effective_weight: float
    age_seconds: int
    summary: str
    reference_url: str | None


@dataclass
class MarketSnapshot:
    as_of: datetime
    regime: Regime
    score: float
    confidence: float
    category_scores: dict[Category, float]
    evidence: list[Evidence]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def format_market_snapshot(snapshot: MarketSnapshot, *, max_evidence: int = 2) -> str:
    """Render a short, deterministic market briefing without another LLM call."""
    if max_evidence < 0:
        raise ValueError("max_evidence cannot be negative")
    regime_names = {
        "risk_on": "위험선호",
        "neutral": "중립",
        "risk_off": "위험회피",
        "insufficient_data": "자료부족",
    }
    category_names = {
        "price": "가격",
        "investor_flow": "수급",
        "macro": "거시",
        "disclosure": "공시",
        "news": "뉴스",
        "overseas": "해외",
    }
    source_names = {
        "toss_invest_prices": "토스증권 시세",
        "toss_invest_investor_trading": "토스증권 수급",
        "open_dart": "금융감독원 DART",
        "bok_ecos": "한국은행 ECOS",
        "naver_news": "네이버 뉴스",
        "overseas_market": "해외시장",
    }
    lines = [
        f"시황: {regime_names[snapshot.regime]} "
        f"({snapshot.score:+.2f}) / 신뢰 {snapshot.confidence * 100:.0f}%"
    ]
    if snapshot.category_scores:
        rendered_scores = " · ".join(
            f"{category_names[category]} {score:+.2f}"
            for category, score in snapshot.category_scores.items()
        )
        lines.append(f"• {rendered_scores}")
    for item in snapshot.evidence[:max_evidence]:
        source = source_names.get(item.source_id, item.source_id)
        age = _format_age(item.age_seconds)
        summary = item.summary.strip() or f"{item.metric} {item.value:+.2f}"
        lines.append(
            f"• 근거[{source}·{category_names[item.category]}·{age}]: {summary}"
        )
        if item.reference_url:
            lines.append(f"  출처: {item.reference_url}")
    if snapshot.regime == "insufficient_data":
        lines.append("⚠ 데이터 범위가 부족해 신규진입 판단을 보수적으로 제한합니다.")
    if snapshot.warnings:
        rendered_warnings: list[str] = []
        for warning in snapshot.warnings:
            if warning == "missing_price":
                rendered = "최신 가격 신호 없음"
            elif warning == "missing_investor_flow":
                rendered = "최신 외국인·기관·개인 수급 신호 없음"
            elif warning.startswith("conflicting_"):
                category = warning.removeprefix("conflicting_").removesuffix("_signals")
                rendered = f"{category_names.get(category, category)} 신호 상충"
            elif warning.startswith("stale_"):
                payload = warning.removeprefix("stale_")
                category = next(
                    (
                        value
                        for value in category_names
                        if payload.startswith(f"{value}_")
                    ),
                    payload,
                )
                rendered = f"오래된 {category_names.get(category, category)} 자료 제외"
            elif warning.startswith("future_timestamp_"):
                rendered = "미래 시각으로 표시된 자료 제외"
            elif warning == "market_coverage_below_50pct":
                rendered = "사용 가능한 시황 범위 50% 미만"
            else:
                rendered = warning
            if rendered not in rendered_warnings:
                rendered_warnings.append(rendered)
        if rendered_warnings:
            lines.append("⚠ " + " · ".join(rendered_warnings[:3]))
    return "\n".join(lines)


def _format_age(age_seconds: int) -> str:
    if age_seconds < 60:
        return f"{age_seconds}초 전"
    if age_seconds < 3600:
        return f"{age_seconds // 60}분 전"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}시간 전"
    return f"{age_seconds // 86400}일 전"


def _freshness(observation: MarketObservation, as_of: datetime) -> float:
    age = max(0.0, (as_of - observation.observed_at).total_seconds())
    max_age = MAX_AGE_SECONDS[observation.category]
    return max(0.0, 1.0 - age / max_age)


def _quality(observation: MarketObservation) -> float:
    return min(1.0, TIER_QUALITY[observation.source_tier] + (0.05 if observation.official else 0.0))


def build_market_snapshot(
    observations: list[MarketObservation], as_of: datetime | None = None
) -> MarketSnapshot:
    """Combine heterogeneous market evidence without hiding staleness or disagreement."""
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    usable: list[tuple[MarketObservation, float]] = []
    evidence: list[Evidence] = []
    warnings: list[str] = []
    for observation in observations:
        if observation.observed_at > now or observation.fetched_at > now:
            warnings.append(
                f"future_timestamp_{observation.category}_{observation.source_id}"
            )
            continue
        freshness = _freshness(observation, now)
        if freshness <= 0:
            warnings.append(f"stale_{observation.category}_{observation.source_id}")
            continue
        effective = CATEGORY_WEIGHTS[observation.category] * freshness * _quality(observation)
        usable.append((observation, effective))
        evidence.append(
            Evidence(
                source_id=observation.source_id,
                category=observation.category,
                metric=observation.metric,
                value=observation.value,
                effective_weight=round(effective, 6),
                age_seconds=max(0, int((now - observation.observed_at).total_seconds())),
                summary=observation.summary,
                reference_url=observation.reference_url,
            )
        )

    category_scores: dict[Category, float] = {}
    category_reliability: dict[Category, float] = {}
    for category in CATEGORY_WEIGHTS:
        members = [(item, weight) for item, weight in usable if item.category == category]
        if not members:
            continue
        denominator = sum(weight for _, weight in members)
        category_scores[category] = sum(item.value * weight for item, weight in members) / denominator
        base_reliability = min(1.0, denominator / CATEGORY_WEIGHTS[category])

        values = [item.value for item, _ in members]
        within_category_dispersion = pstdev(values) if len(values) > 1 else 0.0
        category_agreement = max(0.25, 1.0 - within_category_dispersion)
        category_reliability[category] = base_reliability * category_agreement
        if len(values) >= 2 and min(values) <= -0.5 and max(values) >= 0.5:
            warnings.append(f"conflicting_{category}_signals")

    for critical in ("price", "investor_flow"):
        if critical not in category_scores:
            warnings.append(f"missing_{critical}")

    coverage = sum(CATEGORY_WEIGHTS[c] for c in category_scores)
    critical_missing = any(
        critical not in category_scores for critical in ("price", "investor_flow")
    )
    if coverage < 0.5 or critical_missing:
        return MarketSnapshot(
            as_of=now,
            regime="insufficient_data",
            score=0.0,
            confidence=round(coverage, 4),
            category_scores={k: round(v, 4) for k, v in category_scores.items()},
            evidence=evidence,
            warnings=(
                warnings + (["market_coverage_below_50pct"] if coverage < 0.5 else [])
            ),
        )

    score = sum(
        category_scores[c] * CATEGORY_WEIGHTS[c] for c in category_scores
    ) / coverage
    reliability = sum(
        category_reliability[c] * CATEGORY_WEIGHTS[c] for c in category_scores
    ) / coverage
    dispersion = pstdev(category_scores.values()) if len(category_scores) > 1 else 0.0
    agreement = max(0.25, 1.0 - dispersion / math.sqrt(2.0))
    confidence = min(1.0, coverage * reliability * agreement)

    if score >= 0.35:
        regime: Regime = "risk_on"
    elif score <= -0.35:
        regime = "risk_off"
    else:
        regime = "neutral"

    evidence.sort(key=lambda item: item.effective_weight, reverse=True)
    return MarketSnapshot(
        as_of=now,
        regime=regime,
        score=round(score, 4),
        confidence=round(confidence, 4),
        category_scores={k: round(v, 4) for k, v in category_scores.items()},
        evidence=evidence,
        warnings=warnings,
    )
