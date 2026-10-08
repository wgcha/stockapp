from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import re
from typing import Literal


Horizon = Literal["intraday", "week", "month", "long_term", "unspecified"]
ReturnPeriod = Literal["daily", "weekly", "monthly", "annual", "unspecified"]
TradingFrequency = Literal["low", "medium", "high", "unspecified"]
ApprovalLevel = Literal["guide_only", "owner_each_order"]


@dataclass(frozen=True)
class Allocation:
    strategy: str
    weight_pct: float


@dataclass
class InvestmentIntent:
    raw_text: str
    horizon: Horizon = "unspecified"
    target_return_pct: float | None = None
    target_return_period: ReturnPeriod = "unspecified"
    allocations: list[Allocation] = field(default_factory=list)
    max_daily_loss_pct: float | None = None
    max_trade_loss_pct: float | None = None
    trading_frequency: TradingFrequency = "unspecified"
    max_trades_per_day: int | None = None
    approval_level: ApprovalLevel = "owner_each_order"
    valid_for_days: int | None = None
    requires_confirmation: bool = True
    warnings: list[str] = field(default_factory=list)
    risk_explanations: list[str] = field(default_factory=list)
    suggested_guardrails: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_STRATEGY_ALIASES = {
    "장기안정": "long_term_stable",
    "장기 안정": "long_term_stable",
    "장기": "long_term",
    "단타": "day_trading",
    "스윙": "swing",
    "현금": "cash",
}
_PCT = r"(?:%|퍼센트|퍼)"


def _parse_allocations(text: str) -> list[Allocation]:
    found: dict[str, float] = {}
    for label, strategy in sorted(
        _STRATEGY_ALIASES.items(), key=lambda item: -len(item[0])
    ):
        pattern = rf"{re.escape(label)}\s*(?:형)?\s*(\d+(?:\.\d+)?)\s*{_PCT}"
        match = re.search(pattern, text)
        if match and strategy not in found:
            found[strategy] = float(match.group(1))
    return [Allocation(strategy=k, weight_pct=v) for k, v in found.items()]


def _parse_target(text: str) -> tuple[float | None, ReturnPeriod]:
    patterns: list[tuple[str, ReturnPeriod]] = [
        (rf"(?:매일|일(?:일|간)?)(?:씩)?\s*(\d+(?:\.\d+)?)\s*{_PCT}", "daily"),
        (rf"(?:매주|주간|주당)\s*(\d+(?:\.\d+)?)\s*{_PCT}", "weekly"),
        (rf"(?:매월|월간|월)\s*(\d+(?:\.\d+)?)\s*{_PCT}", "monthly"),
        (rf"(?:연간|연|년)\s*(\d+(?:\.\d+)?)\s*{_PCT}", "annual"),
    ]
    for pattern, period in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)), period
    generic = re.search(
        rf"(?:목표|수익(?:률)?)\s*(\d+(?:\.\d+)?)\s*{_PCT}", text
    )
    return (float(generic.group(1)), "unspecified") if generic else (None, "unspecified")


def _infer_horizon(text: str) -> Horizon:
    if any(word in text for word in ("이번 주", "이번주", "이번 한주", "일주일", "주간")):
        return "week"
    if any(word in text for word in ("이번 달", "이번달", "월간")):
        return "month"
    if any(word in text for word in ("오늘", "당일", "장중", "단타")):
        return "intraday"
    if any(word in text for word in ("장기", "년", "연간")):
        return "long_term"
    return "unspecified"


def _parse_valid_for_days(text: str) -> int | None:
    if any(word in text for word in ("이번 주", "이번주", "이번 한주", "일주일")):
        return 7
    if any(word in text for word in ("이번 달", "이번달")):
        return 31
    if any(word in text for word in ("오늘만", "오늘 하루", "당일만")):
        return 1
    return None


def _first_number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _parse_risk_limits(text: str) -> tuple[float | None, float | None]:
    trade = _first_number(
        text,
        (
            rf"(?:거래당|한\s*번(?:의\s*거래)?당)\s*(?:허용)?\s*손실(?:한도)?(?:은|는)?\s*(\d+(?:\.\d+)?)\s*{_PCT}",
            rf"(?:손절|거래당\s*리스크)\s*(\d+(?:\.\d+)?)\s*{_PCT}",
        ),
    )
    daily = _first_number(
        text,
        (
            rf"(?:일일|하루)\s*(?:최대\s*)?(?:허용)?\s*손실(?:한도)?(?:은|는)?\s*(\d+(?:\.\d+)?)\s*{_PCT}",
            rf"(?:일일|하루)\s*(?:중단선|손실제한)\s*(\d+(?:\.\d+)?)\s*{_PCT}",
        ),
    )
    return trade, daily


def _parse_frequency(text: str) -> tuple[TradingFrequency, int | None]:
    explicit = re.search(r"(?:하루|일일)\s*(\d+)\s*(?:회|번)", text)
    if explicit:
        limit = int(explicit.group(1))
        if limit <= 1:
            return "low", limit
        if limit <= 3:
            return "medium", limit
        return "high", limit
    if any(term in text for term in ("자주", "고빈도", "적극 거래")):
        return "high", None
    if any(term in text for term in ("가끔", "저빈도", "거래 적게")):
        return "low", None
    return "unspecified", None


def _parse_approval(text: str) -> tuple[ApprovalLevel, bool]:
    if any(term in text for term in ("가이드만", "알림만", "주문하지 마")):
        return "guide_only", False
    automatic_requested = any(
        term in text for term in ("자동주문", "자동 매매", "알아서 거래")
    )
    return "owner_each_order", automatic_requested


def _compounded_annual_pct(target: float, period: ReturnPeriod) -> float | None:
    periods = {"daily": 252, "weekly": 52, "monthly": 12, "annual": 1}.get(period)
    if periods is None or target <= -100:
        return None
    try:
        return (math.pow(1.0 + target / 100.0, periods) - 1.0) * 100.0
    except OverflowError:
        return math.inf


def _apply_risk_policy(intent: InvestmentIntent) -> None:
    total_weight = sum(item.weight_pct for item in intent.allocations)
    if intent.allocations and abs(total_weight - 100.0) > 0.01:
        intent.warnings.append(
            f"전략 배분 합계가 {total_weight:g}%로 100%가 아닙니다."
        )
        intent.missing_fields.append("remaining_allocation")
        if total_weight < 100:
            intent.suggested_guardrails.append(
                f"미지정 {100 - total_weight:g}%는 승인 전략이 정해질 때까지 현금으로 유지"
            )
        else:
            intent.suggested_guardrails.append(
                "배분 합계를 100% 이하로 고치기 전에는 신규진입 없이 전액 현금 대기"
            )

    target = intent.target_return_pct
    period = intent.target_return_period
    aggressive = target is not None and (
        (period == "daily" and target >= 1.0)
        or (period == "weekly" and target >= 3.0)
    )
    if aggressive:
        annualized = _compounded_annual_pct(target, period)
        if annualized is not None:
            display = (
                "계산 범위 초과"
                if not math.isfinite(annualized)
                else f"약 {annualized:,.0f}%"
            )
            intent.risk_explanations.append(
                f"{period} {target:g}%를 매 기간 복리로 달성한다고 가정하면 "
                f"연 환산은 {display}입니다. 이는 예측치가 아니라 목표의 "
                "공격성을 보여주는 산술 환산입니다."
            )
        intent.warnings.append(
            "요청한 목표수익률은 매우 공격적이며 달성을 보장할 수 없습니다. "
            "목표는 거래 강제가 아닌 기회 평가 기준으로만 사용합니다."
        )
        if intent.max_trade_loss_pct is None:
            intent.max_trade_loss_pct = 0.5
        if intent.max_daily_loss_pct is None:
            intent.max_daily_loss_pct = 1.5
        intent.suggested_guardrails.extend(
            [
                "대안 예시: 연간 6~12% 목표와 월간 최대손실 3~5% 범위부터 검토",
                "거래당 총자산의 0.5% 이상 손실 위험을 허용하지 않기",
                "일일 누적손실 1.5% 도달 시 신규 거래 중단",
                "조건을 충족하는 기회가 없으면 거래하지 않기",
                "일일 수익 강제 대신 연간 목표와 월별 손실한도로 재설정 검토",
            ]
        )

    long_term_weight = sum(
        item.weight_pct
        for item in intent.allocations
        if item.strategy in {"long_term", "long_term_stable"}
    )
    short_target = target is not None and (
        (period == "daily" and target >= 0.5)
        or (period == "weekly" and target >= 2.0)
    )
    if long_term_weight >= 50 and short_target:
        period_name = {"daily": "일일", "weekly": "주간"}.get(period, period)
        intent.warnings.append(
            f"장기 배분 {long_term_weight:g}%와 {period_name} 수익목표의 평가기간이 서로 다릅니다."
        )
        intent.risk_explanations.append(
            "장기 전략은 수개월~수년 단위로 평가하므로 일별 목표 미달을 이유로 "
            "매매하면 전략 규칙과 거래비용 가정을 훼손합니다."
        )
        intent.suggested_guardrails.append(
            "장기 버킷은 연간 목표로 평가하고 단기 목표는 승인된 단타·스윙 비중에만 적용"
        )

    day_trading_weight = sum(
        item.weight_pct
        for item in intent.allocations
        if item.strategy == "day_trading"
    )
    if day_trading_weight > 0 and intent.max_trades_per_day is None:
        intent.suggested_guardrails.append(
            "단타는 검증 초기 하루 최대 2회와 거래당 손실 0.5% 이하부터 검토"
        )
    if short_target and intent.trading_frequency == "low":
        intent.warnings.append(
            "낮은 거래빈도와 매일·매주 반복 수익목표는 동시에 강제할 수 없습니다."
        )
        intent.suggested_guardrails.append(
            "거래가 없을 수 있음을 허용하고 수익목표를 월간·연간 평가기준으로 변경"
        )

    if intent.max_trade_loss_pct is not None and not 0 < intent.max_trade_loss_pct <= 5:
        intent.warnings.append("거래당 손실한도는 0% 초과 5% 이하로 다시 설정해야 합니다.")
        intent.max_trade_loss_pct = None
    if intent.max_daily_loss_pct is not None and not 0 < intent.max_daily_loss_pct <= 10:
        intent.warnings.append("일일 손실한도는 0% 초과 10% 이하로 다시 설정해야 합니다.")
        intent.max_daily_loss_pct = None
    if intent.max_trades_per_day is not None and intent.max_trades_per_day > 20:
        intent.warnings.append("하루 거래 횟수는 최대 20회까지만 설정할 수 있습니다.")
        intent.max_trades_per_day = 20

    if intent.target_return_pct is None:
        intent.missing_fields.append("target_return")
    if not intent.allocations:
        intent.missing_fields.append("strategy_allocation")
    if intent.max_daily_loss_pct is None:
        intent.missing_fields.append("max_daily_loss")


def parse_investment_intent(text: str) -> InvestmentIntent:
    """Convert a short Korean investment request into a safe structured intent."""
    normalized = re.sub(r"\s+", " ", text.strip())
    target, period = _parse_target(normalized)
    max_trade_loss, max_daily_loss = _parse_risk_limits(normalized)
    frequency, max_trades = _parse_frequency(normalized)
    approval_level, automatic_requested = _parse_approval(normalized)
    intent = InvestmentIntent(
        raw_text=text,
        horizon=_infer_horizon(normalized),
        target_return_pct=target,
        target_return_period=period,
        allocations=_parse_allocations(normalized),
        max_trade_loss_pct=max_trade_loss,
        max_daily_loss_pct=max_daily_loss,
        trading_frequency=frequency,
        max_trades_per_day=max_trades,
        approval_level=approval_level,
        valid_for_days=_parse_valid_for_days(normalized),
    )
    if automatic_requested:
        intent.warnings.append(
            "이 프로그램은 투자 가이드만 제공하며 자동주문을 실행하지 않습니다. "
            "실제 매매는 증권사 앱에서 직접 진행하세요."
        )
    _apply_risk_policy(intent)
    return intent


def merge_investment_intents(
    previous: InvestmentIntent, update: InvestmentIntent
) -> InvestmentIntent:
    """Merge a conversational partial update without discarding prior settings."""
    explicit_trade, explicit_daily = _parse_risk_limits(update.raw_text)
    frequency, max_trades = _parse_frequency(update.raw_text)
    approval, automatic_requested = _parse_approval(update.raw_text)
    approval_explicit = automatic_requested or any(
        term in update.raw_text
        for term in (
            "가이드만",
            "알림만",
            "주문하지 마",
            "자동주문",
            "자동 매매",
            "알아서 거래",
            "주문마다 승인",
        )
    )

    def conservative_default(
        old: float | None, new: float | None, explicit: float | None
    ) -> float | None:
        if explicit is not None:
            return new if new is not None else old
        if old is None:
            return new
        if new is None:
            return old
        return min(old, new)

    merged = InvestmentIntent(
        raw_text=f"{previous.raw_text} | {update.raw_text}",
        horizon=(update.horizon if update.horizon != "unspecified" else previous.horizon),
        target_return_pct=(
            update.target_return_pct
            if update.target_return_pct is not None
            else previous.target_return_pct
        ),
        target_return_period=(
            update.target_return_period
            if update.target_return_pct is not None
            else previous.target_return_period
        ),
        allocations=(update.allocations or previous.allocations),
        max_daily_loss_pct=conservative_default(
            previous.max_daily_loss_pct,
            update.max_daily_loss_pct,
            explicit_daily,
        ),
        max_trade_loss_pct=conservative_default(
            previous.max_trade_loss_pct,
            update.max_trade_loss_pct,
            explicit_trade,
        ),
        trading_frequency=(
            frequency if frequency != "unspecified" else previous.trading_frequency
        ),
        max_trades_per_day=(
            max_trades if frequency != "unspecified" else previous.max_trades_per_day
        ),
        approval_level=(approval if approval_explicit else previous.approval_level),
        valid_for_days=(
            update.valid_for_days
            if update.valid_for_days is not None
            else previous.valid_for_days
        ),
    )
    _apply_risk_policy(merged)
    for warning in update.warnings:
        if warning not in merged.warnings:
            merged.warnings.append(warning)
    return merged
