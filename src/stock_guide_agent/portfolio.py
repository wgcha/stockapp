from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .intent import InvestmentIntent
from .market import MarketSnapshot
from .strategies import StrategyRecord


Bucket = Literal["long_term", "swing", "intraday", "cash"]


@dataclass(frozen=True)
class StrategyAllocation:
    bucket: Bucket
    strategy_id: str
    version: str
    weight_pct: float


@dataclass(frozen=True)
class RiskOverlaySignal:
    strategy_id: str
    version: str
    exposure_multiplier: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.exposure_multiplier <= 1.0:
            raise ValueError("risk overlay exposure must be between zero and one")


@dataclass(frozen=True)
class StrategyAttribution:
    strategy_id: str
    version: str
    display_name: str
    source_title: str
    source_url: str
    published_year: int
    applicable_markets: tuple[str, ...]
    limitation: str
    last_researched_on: date | None


@dataclass
class PersonalizedStrategyPlan:
    allocations: list[StrategyAllocation] = field(default_factory=list)
    attributions: list[StrategyAttribution] = field(default_factory=list)
    cash_weight_pct: float = 0.0
    warnings: list[str] = field(default_factory=list)
    requires_confirmation: bool = True
    risk_overlay: RiskOverlaySignal | None = None

    @property
    def invested_weight_pct(self) -> float:
        return round(sum(item.weight_pct for item in self.allocations), 6)


_BUCKET_MAP: dict[str, Bucket] = {
    "long_term_stable": "long_term",
    "long_term": "long_term",
    "swing": "swing",
    "day_trading": "intraday",
    "cash": "cash",
}

_STRATEGY_NAMES = {
    "time_series_momentum": "시계열 모멘텀",
    "long_term_absolute_momentum": "장기 절대 모멘텀",
    "intraday_opening_range_breakout": "장중 오프닝 레인지 돌파",
    "volatility_managed_exposure": "변동성 위험조정",
}

_LIMITATIONS_KO = {
    "time_series_momentum": "횡보장에서 잦은 반전 손실이 나며 한국 개별종목 근거가 제한적",
    "long_term_absolute_momentum": "급반등 초기에 현금 상태가 이어질 수 있고 개별종목 근거가 약함",
    "intraday_opening_range_breakout": "미국시장 중심 연구이며 슬리피지·세금·체결순서에 매우 민감",
    "volatility_managed_exposure": "급반등 직전에 비중을 줄일 수 있고 추정기간과 거래비용에 민감",
}

_MARKET_NAMES = {
    "global_futures": "글로벌 선물",
    "equity_indices": "주가지수",
    "us_equities": "미국 주식",
    "korean_equities_research_candidate": "한국 주식 재검증 대상",
    "factor_portfolios": "팩터 포트폴리오",
}


def _attribution(record: StrategyRecord) -> StrategyAttribution:
    definition = record.definition
    return StrategyAttribution(
        strategy_id=definition.strategy_id,
        version=definition.version,
        display_name=_STRATEGY_NAMES.get(
            definition.strategy_id, definition.display_name
        ),
        source_title=definition.source.title,
        source_url=definition.source.url,
        published_year=definition.source.published_year,
        applicable_markets=definition.applicable_markets,
        limitation=_LIMITATIONS_KO.get(
            definition.strategy_id,
            definition.limitations[0] if definition.limitations else "별도 한계 확인 필요",
        ),
        last_researched_on=record.last_researched_on,
    )


def compose_strategy_plan(
    intent: InvestmentIntent,
    records: list[StrategyRecord],
    market: MarketSnapshot,
    risk_overlay_signals: tuple[RiskOverlaySignal, ...] = (),
) -> PersonalizedStrategyPlan:
    """Allocate only to approved strategies and preserve unsupported risk as cash."""
    plan = PersonalizedStrategyPlan()
    requested_total = sum(item.weight_pct for item in intent.allocations)
    if requested_total > 100.000001:
        plan.warnings.append("requested_allocation_exceeds_100pct")
        plan.cash_weight_pct = 100.0
        return plan

    if market.regime == "insufficient_data" or market.confidence < 0.4:
        plan.cash_weight_pct = 100.0
        plan.warnings.append("insufficient_market_confidence_no_new_positions")
        return plan

    approved = [
        record
        for record in records
        if record.status == "approved"
        and record.definition.executable
        and record.definition.signal_engine is not None
    ]
    by_horizon: dict[str, list[StrategyRecord]] = {}
    for record in approved:
        by_horizon.setdefault(record.definition.horizon, []).append(record)

    unassigned = max(0.0, 100.0 - requested_total)
    for requested in intent.allocations:
        bucket = _BUCKET_MAP.get(requested.strategy)
        if bucket is None:
            unassigned += requested.weight_pct
            plan.warnings.append(f"unsupported_bucket_{requested.strategy}")
            continue
        if bucket == "cash":
            unassigned += requested.weight_pct
            continue
        if market.regime == "risk_off" and bucket == "intraday":
            unassigned += requested.weight_pct
            plan.warnings.append("intraday_disabled_in_risk_off_regime")
            continue

        candidates = sorted(
            by_horizon.get(bucket, []),
            key=lambda item: (item.definition.strategy_id, item.definition.version),
        )
        if not candidates:
            unassigned += requested.weight_pct
            plan.warnings.append(f"no_approved_strategy_for_{bucket}")
            continue

        share = requested.weight_pct / len(candidates)
        for candidate in candidates:
            plan.allocations.append(
                StrategyAllocation(
                    bucket=bucket,
                    strategy_id=candidate.definition.strategy_id,
                    version=candidate.definition.version,
                    weight_pct=round(share, 6),
                )
            )
            attribution = _attribution(candidate)
            if attribution not in plan.attributions:
                plan.attributions.append(attribution)

    plan.cash_weight_pct = round(unassigned, 6)

    approved_risk_models = {
        (record.definition.strategy_id, record.definition.version)
        for record in approved
        if record.definition.horizon == "risk_management"
    }
    eligible_overlays = sorted(
        (
            signal
            for signal in risk_overlay_signals
            if (signal.strategy_id, signal.version) in approved_risk_models
        ),
        key=lambda signal: (
            signal.exposure_multiplier,
            signal.strategy_id,
            signal.version,
        ),
    )
    if eligible_overlays and plan.allocations:
        overlay = eligible_overlays[0]
        overlay_record = next(
            record
            for record in approved
            if (
                record.definition.strategy_id,
                record.definition.version,
            )
            == (overlay.strategy_id, overlay.version)
        )
        invested_before = plan.invested_weight_pct
        plan.allocations = [
            StrategyAllocation(
                bucket=item.bucket,
                strategy_id=item.strategy_id,
                version=item.version,
                weight_pct=round(item.weight_pct * overlay.exposure_multiplier, 6),
            )
            for item in plan.allocations
        ]
        released = invested_before - plan.invested_weight_pct
        plan.cash_weight_pct = round(plan.cash_weight_pct + released, 6)
        plan.risk_overlay = overlay
        attribution = _attribution(overlay_record)
        if attribution not in plan.attributions:
            plan.attributions.append(attribution)
        plan.warnings.append("approved_volatility_risk_overlay_applied")

    total = plan.invested_weight_pct + plan.cash_weight_pct
    if abs(total - 100.0) > 0.0001:
        plan.warnings.append("plan_weight_invariant_failed")
        plan.allocations.clear()
        plan.attributions.clear()
        plan.cash_weight_pct = 100.0
    return plan


def format_strategy_plan(plan: PersonalizedStrategyPlan) -> str:
    """Render the effective, validated allocation in compact messenger text."""
    lines = ["검증 전략 조합"]
    bucket_names = {
        "long_term": "장기",
        "swing": "스윙",
        "intraday": "단타",
        "cash": "현금",
    }
    for item in plan.allocations:
        lines.append(
            f"• {bucket_names[item.bucket]} {item.weight_pct:g}%: "
            f"{item.strategy_id} v{item.version}"
        )
    if plan.risk_overlay is not None:
        lines.append(
            f"• 위험조정 {plan.risk_overlay.exposure_multiplier * 100:g}%: "
            f"{plan.risk_overlay.strategy_id} v{plan.risk_overlay.version}"
        )
    for item in plan.attributions:
        markets = "·".join(
            _MARKET_NAMES.get(value, value) for value in item.applicable_markets[:2]
        )
        checked = (
            item.last_researched_on.isoformat()
            if item.last_researched_on is not None
            else "원문 확인 대기"
        )
        lines.extend(
            (
                f"  ↳ {item.display_name}: {item.source_title} ({item.published_year})",
                f"    연구시장 {markets} / 최근확인 {checked}",
                f"    한국 적용 한계: {item.limitation}",
                f"    원문: {item.source_url}",
            )
        )
    lines.append(f"• 현금·미배정: {plan.cash_weight_pct:g}%")
    warning_names = {
        "requested_allocation_exceeds_100pct": "요청 배분이 100%를 초과해 전액 현금 대기",
        "insufficient_market_confidence_no_new_positions": "시황 신뢰도 부족으로 신규진입 중단",
        "intraday_disabled_in_risk_off_regime": "위험회피 시황이라 단타 배분 중단",
        "approved_volatility_risk_overlay_applied": "승인된 변동성 위험조정 신호를 적용",
    }
    for warning in plan.warnings[:3]:
        if warning.startswith("no_approved_strategy_for_"):
            bucket = warning.removeprefix("no_approved_strategy_for_")
            rendered = f"{bucket_names.get(bucket, bucket)}용 승인 전략이 없어 현금 대기"
        else:
            rendered = warning_names.get(warning, warning)
        lines.append(f"⚠ {rendered}")
    return "\n".join(lines)
