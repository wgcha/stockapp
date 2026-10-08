"""Bounded Korean rendering for owner-facing strategy evidence."""

from __future__ import annotations

from datetime import date
import math
from numbers import Real
import re

from .strategies import (
    PromotionDecision,
    StrategyRecord,
    evaluate_for_promotion,
)


_STATUS_NAMES = {
    "research": "연구중",
    "rejected": "거부",
    "backtested": "백테스트",
    "paper_trading": "모의검증중",
    "validated": "승인대기",
    "approved": "소유자 활성화",
    "suspended": "중지",
}

_FAILURE_NAMES = {
    "report_definition_mismatch": "보고서의 전략 ID 또는 버전이 다릅니다",
    "report_signal_engine_mismatch": "보고서의 신호 엔진이 전략 정의와 다릅니다",
    "public_principle_requires_testable_rules": "검증 가능한 실행 규칙이 아직 없습니다",
    "strategy_signal_engine_not_implemented": "전략 신호 엔진이 아직 구현되지 않았습니다",
    "insufficient_sample_years": "표본 기간이 기준보다 짧습니다",
    "missing_out_of_sample_test": "표본 외 검증이 없습니다",
    "insufficient_walk_forward_windows": "워크포워드 구간이 3개 미만입니다",
    "insufficient_trade_count": "거래 표본 수가 기준보다 적습니다",
    "weak_cost_adjusted_sharpe": "비용 반영 샤프 비율이 0.5 미만입니다",
    "excessive_drawdown": "최대 낙폭이 25%를 넘습니다",
    "parameter_instability": "파라미터 안정성이 0.6 미만입니다",
    "insufficient_paper_trading": "인정된 모의검증일이 30일 미만입니다",
    "missing_realistic_costs": "수수료·세금·슬리피지 반영이 확인되지 않았습니다",
    "data_leakage_risk": "데이터 누수 검사를 통과하지 못했습니다",
    "invalid_validation_metrics": "수치나 자료 형식이 유효하지 않아 검증이 거부됐습니다",
}


def _finite_real(value: object) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _number(value: object, *, digits: int = 2, suffix: str = "") -> str:
    if _finite_real(value):
        return f"{value:.{digits}f}{suffix}"
    return "확인 불가"


def _percentage(value: object) -> str:
    if _finite_real(value):
        return _number(value * 100, suffix="%")
    return "확인 불가"


def unmet_requirement_names(decision: PromotionDecision) -> tuple[str, ...]:
    return tuple(_FAILURE_NAMES.get(item, "추가 검증이 필요합니다") for item in decision.failures)


def format_strategy_overview_requirement(record: StrategyRecord) -> str:
    """Return one short evidence hint for the strategy overview."""
    if not record.reports:
        return "검증자료 없음 · 전략보고 명령으로 확인"
    decision = evaluate_for_promotion(record.definition, record.reports[-1])
    if decision.eligible:
        if record.status == "approved":
            return "기술 검증 통과 · 현재 소유자 활성화"
        if record.status == "suspended":
            return "기술 검증 통과 · 소유자 재활성화 필요"
        return "기술 검증 통과 · 소유자 활성화 대기"
    names = unmet_requirement_names(decision)
    return "검증 필요: " + "; ".join(names[:2])


def format_strategy_report(
    record: StrategyRecord,
    *,
    checked_on: date | None,
    paper_progress: str,
) -> str:
    definition = record.definition
    lines = [
        f"전략 보고서 · {definition.strategy_id} v{definition.version}",
        f"상태: {_STATUS_NAMES.get(record.status, '확인 필요')}",
    ]
    report = record.reports[-1] if record.reports else None
    if report is None:
        lines.extend(("최근 검증: 없음", f"정상 모의검증 진행: {paper_progress}"))
        missing = ["아직 검증 보고서가 없습니다"]
        if not definition.executable:
            missing.append("검증 가능한 실행 규칙이 없습니다")
        if definition.signal_engine is None:
            missing.append("전략 신호 엔진이 구현되지 않았습니다")
        lines.append("미충족 요건: " + "; ".join(missing))
    else:
        lines.extend(
            (
                f"최근 검증: {checked_on.isoformat() if checked_on else '날짜 확인 불가'}",
                f"정상 모의검증 진행: {paper_progress}",
                "최근 검증 성과: "
                f"표본 {_number(report.sample_years, suffix='년')} / "
                f"거래 {report.trades if isinstance(report.trades, int) and not isinstance(report.trades, bool) and report.trades >= 0 else '확인 불가'}건 / "
                f"표본 외 {'통과' if report.out_of_sample is True else '미통과' if report.out_of_sample is False else '확인 불가'} / "
                f"워크포워드 {report.walk_forward_windows if isinstance(report.walk_forward_windows, int) and not isinstance(report.walk_forward_windows, bool) and report.walk_forward_windows >= 0 else '확인 불가'}회",
                f"비용 반영 샤프 {_number(report.cost_adjusted_sharpe)} / "
                f"최대 낙폭 {_percentage(report.max_drawdown)} / "
                f"파라미터 안정성 {_number(report.parameter_stability)} / "
                f"보고서 모의검증 {report.paper_trading_days if isinstance(report.paper_trading_days, int) and not isinstance(report.paper_trading_days, bool) and report.paper_trading_days >= 0 else '확인 불가'}일",
            )
        )
        lines.append(
            "비용: 수수료·세금·슬리피지 "
            + ("포함" if report.includes_fees_taxes_slippage is True else "미포함" if report.includes_fees_taxes_slippage is False else "확인 불가")
        )
        urls = report.cost_source_urls if isinstance(report.cost_source_urls, (tuple, list)) else ()
        assumptions = report.cost_assumptions if isinstance(report.cost_assumptions, (tuple, list)) else ()
        lines.append("비용 출처: " + _render_evidence_items(urls, max_item_chars=512))
        lines.append("비용 가정: " + _render_assumptions(assumptions))
        decision = evaluate_for_promotion(definition, report)
        names = unmet_requirement_names(decision)
        lines.append(
            "미충족 요건: " + ("없음" if not names else "; ".join(names))
        )
    lines.append("기술 검증 통과는 전략 활성화와 다르며, 별도 소유자 승인이 필요합니다.")
    return "\n".join(lines)


def _render_evidence_items(items: tuple | list, *, max_item_chars: int) -> str:
    if not items:
        return "자료 없음"
    rendered: list[str] = []
    omitted = 0
    for item in items:
        value = str(item)
        if len(rendered) >= 2 or len(value) > max_item_chars:
            omitted += 1
            continue
        rendered.append(value)
    if omitted:
        rendered.append(f"[{omitted}건 생략]")
    return ", ".join(rendered) if rendered else f"[{omitted}건 생략]"


def _render_assumptions(items: tuple | list) -> str:
    if not items:
        return "자료 없음"
    rendered: list[str] = []
    omitted = 0
    for item in items:
        value = str(item)
        if value.startswith("apply_2026_costs_outside"):
            value = "공개 적용기간 밖의 2026 비용표를 사용한 추정 가정"
        else:
            match = re.fullmatch(r"slippage_(\d+(?:\.\d+)?)_bps_each_side", value)
            if match:
                value = f"편도 {match.group(1)}bp 슬리피지 가정"
        if len(rendered) >= 2 or len(value) > 240:
            omitted += 1
            continue
        rendered.append(value)
    if omitted:
        rendered.append(f"[{omitted}건 생략]")
    return "; ".join(rendered) if rendered else f"[{omitted}건 생략]"
