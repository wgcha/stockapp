from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .market import MarketSnapshot


Action = Literal[
    "buy",
    "hold",
    "partial_sell",
    "full_sell",
    "review_alternative",
    "stop_trading",
]


@dataclass(frozen=True)
class Position:
    stock_code: str
    quantity: int
    average_price: float
    current_price: float

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        if self.average_price < 0 or self.current_price <= 0:
            raise ValueError("prices must be valid positive values")


@dataclass(frozen=True)
class GuideInput:
    stock_code: str
    market: MarketSnapshot
    strategy_id: str
    strategy_approved: bool
    signal_score: float
    signal_confidence: float
    max_trade_loss_pct: float
    max_daily_loss_pct: float
    daily_pnl_pct: float
    position: Position | None = None
    invalidation_price: float | None = None
    alternative_score: float | None = None
    alternative_stock_code: str | None = None
    alternative_confidence: float | None = None
    current_price: float | None = None
    entry_reference_price: float | None = None
    take_profit_reference_price: float | None = None
    api_healthy: bool = True
    kill_switch_active: bool = False
    strategy_version: str = "1.0.0"
    strategy_allocation_pct: float | None = None

    def __post_init__(self) -> None:
        if not -1 <= self.signal_score <= 1:
            raise ValueError("signal_score must be in [-1, 1]")
        if not 0 <= self.signal_confidence <= 1:
            raise ValueError("signal_confidence must be in [0, 1]")
        if self.max_trade_loss_pct <= 0 or self.max_daily_loss_pct <= 0:
            raise ValueError("loss limits must be positive")
        if self.invalidation_price is not None and self.invalidation_price <= 0:
            raise ValueError("invalidation_price must be positive")
        if self.strategy_allocation_pct is not None and not (
            0 <= self.strategy_allocation_pct <= 100
        ):
            raise ValueError("strategy allocation must be between zero and 100")
        if self.alternative_confidence is not None and not (
            0 <= self.alternative_confidence <= 1
        ):
            raise ValueError("alternative confidence must be in [0, 1]")
        for name, price in (
            ("current_price", self.current_price),
            ("entry_reference_price", self.entry_reference_price),
            ("take_profit_reference_price", self.take_profit_reference_price),
        ):
            if price is not None and price <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class TradeGuide:
    stock_code: str
    action: Action
    suggested_fraction: float
    confidence: float
    reasons: tuple[str, ...]
    loss_limit_pct: float
    invalidation_condition: str
    strategy_id: str
    generated_at: datetime
    max_daily_loss_pct: float = 1.5
    strategy_version: str = "1.0.0"
    alternative_stock_code: str | None = None
    entry_reference_price: float | None = None
    take_profit_reference_price: float | None = None
    requires_confirmation: bool = True
    invalidation_price: float | None = None
    reference_price: float | None = None
    current_signal_score: float | None = None
    alternative_score: float | None = None
    alternative_confidence: float | None = None


def generate_trade_guide(value: GuideInput, *, now: datetime | None = None) -> TradeGuide:
    """Generate a deterministic guide; requested profit targets never trigger a trade."""
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    confidence = round(min(value.market.confidence, value.signal_confidence), 4)
    reasons = [
        f"market_regime={value.market.regime}",
        f"market_score={value.market.score:.4f}",
        f"strategy_signal={value.signal_score:.4f}",
        "target_return_not_used_as_trade_trigger",
    ]
    invalidation = (
        f"current_price_below_or_equal_to_{value.invalidation_price:g}"
        if value.invalidation_price is not None
        else "strategy_defined_invalidation_required_before_order"
    )

    def guide(action: Action, fraction: float, extra: str) -> TradeGuide:
        return TradeGuide(
            stock_code=value.stock_code,
            action=action,
            suggested_fraction=fraction,
            confidence=confidence,
            reasons=tuple(reasons + [extra]),
            loss_limit_pct=value.max_trade_loss_pct,
            invalidation_condition=invalidation,
            strategy_id=value.strategy_id,
            generated_at=generated_at,
            max_daily_loss_pct=value.max_daily_loss_pct,
            strategy_version=value.strategy_version,
            alternative_stock_code=(
                value.alternative_stock_code
                if action == "review_alternative"
                else None
            ),
            entry_reference_price=value.entry_reference_price,
            take_profit_reference_price=value.take_profit_reference_price,
            invalidation_price=value.invalidation_price,
            reference_price=(
                value.current_price
                if value.current_price is not None
                else (
                    value.position.current_price
                    if value.position is not None
                    else None
                )
            ),
            current_signal_score=value.signal_score,
            alternative_score=(
                value.alternative_score
                if action == "review_alternative"
                else None
            ),
            alternative_confidence=(
                value.alternative_confidence
                if action == "review_alternative"
                else None
            ),
        )

    if value.kill_switch_active:
        return guide("stop_trading", 0.0, "kill_switch_active")
    if not value.api_healthy:
        return guide("stop_trading", 0.0, "broker_or_data_api_unhealthy")
    if value.daily_pnl_pct <= -value.max_daily_loss_pct:
        return guide("stop_trading", 0.0, "daily_loss_limit_reached")
    if not value.strategy_approved:
        return guide("hold", 0.0, "strategy_not_approved")
    if value.market.regime == "insufficient_data" or confidence < 0.4:
        return guide("hold", 0.0, "insufficient_data_or_confidence")

    market_component = value.market.score * 0.35
    strategy_component = value.signal_score * 0.65
    composite = market_component + strategy_component
    reasons.append(f"composite_score={composite:.4f}")

    if value.position is not None and value.position.quantity > 0:
        if (
            value.invalidation_price is not None
            and value.position.current_price <= value.invalidation_price
        ):
            return guide("full_sell", 1.0, "explicit_invalidation_price_breached")
        if composite <= -0.55 and value.market.regime == "risk_off" and confidence >= 0.65:
            return guide("full_sell", 1.0, "strong_confirmed_risk_off_exit")
        if composite <= -0.20:
            return guide("partial_sell", 0.5, "negative_signal_reduce_risk")
        if (
            value.current_price is not None
            and value.take_profit_reference_price is not None
            and value.current_price >= value.take_profit_reference_price
            and value.signal_score < 0.35
        ):
            return guide(
                "partial_sell", 0.25, "resistance_reference_reached_with_weak_momentum"
            )
        if (
            value.alternative_score is not None
            and value.alternative_score - value.signal_score >= 0.35
            and composite < 0.20
        ):
            return guide("review_alternative", 0.0, "materially_stronger_alternative_exists")
        return guide("hold", 0.0, "no_exit_condition_met")

    if value.strategy_allocation_pct is not None:
        reasons.append(
            f"effective_strategy_allocation={value.strategy_allocation_pct:.4f}pct"
        )
        if value.strategy_allocation_pct <= 0:
            return guide("hold", 0.0, "no_effective_allocation_for_strategy")

    allocation_multiplier = (
        value.strategy_allocation_pct / 100.0
        if value.strategy_allocation_pct is not None
        else 1.0
    )

    if (
        value.current_price is not None
        and value.entry_reference_price is not None
        and value.current_price <= value.entry_reference_price
        and composite >= 0.20
        and value.market.regime != "risk_off"
    ):
        return guide(
            "buy",
            round(0.15 * allocation_multiplier, 6),
            "support_reference_with_positive_confirmed_signal",
        )
    if composite >= 0.50 and value.market.regime != "risk_off":
        return guide(
            "buy",
            round(0.25 * allocation_multiplier, 6),
            "approved_signal_supports_first_tranche",
        )
    return guide("hold", 0.0, "no_entry_condition_met")


_ACTION_NAMES = {
    "buy": "\ubd84\ud560 \ub9e4\uc218",
    "hold": "\uc720\uc9c0/\ub300\uae30",
    "partial_sell": "\uc77c\ubd80 \ub9e4\ub3c4",
    "full_sell": "\uc804\ub7c9 \ub9e4\ub3c4",
    "review_alternative": "\ub2e4\ub978 \uc885\ubaa9 \uac80\ud1a0",
    "stop_trading": "\uac70\ub798 \uc911\ub2e8",
}

_REASON_NAMES = {
    "target_return_not_used_as_trade_trigger": "목표수익률은 매매 강제조건으로 사용하지 않음",
    "kill_switch_active": "전체 거래중단 스위치가 작동 중",
    "broker_or_data_api_unhealthy": "증권사 또는 시세 API 상태 이상",
    "daily_loss_limit_reached": "하루 손실한도 도달",
    "strategy_not_approved": "소유자가 아직 승인하지 않은 전략",
    "insufficient_data_or_confidence": "자료 또는 판단 신뢰도 부족",
    "explicit_invalidation_price_breached": "설정한 가격 무효화선 이탈",
    "strong_confirmed_risk_off_exit": "강한 위험회피 시황과 매도신호 동시 확인",
    "negative_signal_reduce_risk": "부정적 신호로 위험 축소",
    "resistance_reference_reached_with_weak_momentum": "고가 참고선 도달 후 모멘텀 약화",
    "materially_stronger_alternative_exists": "현재 종목보다 신호가 뚜렷하게 강한 비교 후보 존재",
    "no_exit_condition_met": "매도 또는 교체 조건 미충족",
    "no_effective_allocation_for_strategy": "이 전략에 배정된 실제 투자비중이 없음",
    "support_reference_with_positive_confirmed_signal": "저가 참고선과 긍정 신호 동시 확인",
    "approved_signal_supports_first_tranche": "승인된 긍정 신호로 첫 분할진입 조건 충족",
    "no_entry_condition_met": "신규진입 조건 미충족",
}


def format_reason(reason: str) -> str:
    """Render an internal audit reason as concise user-facing Korean."""
    if reason in _REASON_NAMES:
        return _REASON_NAMES[reason]
    dynamic = (
        ("market_regime=", "시황 국면 "),
        ("market_score=", "시황점수 "),
        ("strategy_signal=", "전략 신호 "),
        ("composite_score=", "종합점수 "),
        ("effective_strategy_allocation=", "실제 전략배분 "),
    )
    for prefix, label in dynamic:
        if reason.startswith(prefix):
            value = reason.removeprefix(prefix)
            return f"{label}{value.removesuffix('pct')}{'%' if value.endswith('pct') else ''}"
    return reason.replace("_", " ")


def format_invalidation_condition(condition: str) -> str:
    """Render a machine-checkable invalidation condition for a user."""
    prefix = "current_price_below_or_equal_to_"
    if condition.startswith(prefix):
        price = float(condition.removeprefix(prefix))
        return f"현재가 {price:,.0f}원 이하"
    if condition == "strategy_defined_invalidation_required_before_order":
        return "가이드 참고 전 전략별 가격 무효화선 설정 필요"
    return condition.replace("_", " ")


def format_trade_guide(guide: TradeGuide) -> str:
    reasons = ", ".join(format_reason(value) for value in guide.reasons[-2:])
    fraction = (
        f" ({guide.suggested_fraction * 100:g}%)" if guide.suggested_fraction else ""
    )
    lines = [
            f"{guide.stock_code}: {_ACTION_NAMES[guide.action]}{fraction}",
            f"\u2022 \uc2e0\ub8b0\ub3c4: {guide.confidence * 100:.0f}%",
            f"\u2022 \uc190\uc2e4\ud55c\ub3c4: \uac70\ub798\ub2f9 {guide.loss_limit_pct:g}% / "
            f"\ud558\ub8e8 {guide.max_daily_loss_pct:g}%",
            f"\u2022 \ubb34\ud6a8\ud654: {format_invalidation_condition(guide.invalidation_condition)}",
            f"\u2022 \uadfc\uac70: {reasons}",
            "\uc8fc\ubb38\uc740 \ubcc4\ub3c4 \ud655\uc778 \uc804\uc5d0\ub294 \uc2e4\ud589\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4.",
        ]
    lines[-1] = (
        "• 이 내용은 투자 가이드입니다. 실제 매매는 증권사 앱에서 직접 진행하고, "
        "이후 보유정보를 갱신하세요."
    )
    if guide.alternative_stock_code:
        lines.insert(1, f"• 비교 후보: {guide.alternative_stock_code}")
        if (
            guide.current_signal_score is not None
            and guide.alternative_score is not None
        ):
            confidence = (
                f" / 후보 신뢰 {guide.alternative_confidence * 100:.0f}%"
                if guide.alternative_confidence is not None
                else ""
            )
            lines.insert(
                2,
                f"• 신호 비교: 현재 {guide.current_signal_score:+.2f} / "
                f"후보 {guide.alternative_score:+.2f}{confidence}",
            )
        lines.insert(
            3,
            "• 후보 종목은 별도 가이드에서 시황·손실한도·무효화선을 확인한 뒤 증권사 앱에서 직접 판단하세요.",
        )
    if guide.entry_reference_price is not None:
        lines.insert(
            -1, f"• 저가 진입 참고선: {guide.entry_reference_price:,.0f}"
        )
    if guide.take_profit_reference_price is not None:
        lines.insert(
            -1, f"• 고가 분할매도 참고선: {guide.take_profit_reference_price:,.0f}"
        )
    if (
        guide.entry_reference_price is not None
        or guide.take_profit_reference_price is not None
    ):
        lines.insert(-1, "• 참고선은 최근 가격범위 기반이며 미래 고점·저점을 보장하지 않음")
    return "\n".join(lines)
