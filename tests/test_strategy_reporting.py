import unittest
from dataclasses import replace
from datetime import date

from stock_guide_agent.strategies import PUBLIC_STRATEGY_CATALOG, StrategyRecord, apply_report
from stock_guide_agent.strategy_reporting import (
    format_strategy_overview_requirement,
    format_strategy_report,
)


class StrategyReportingTests(unittest.TestCase):
    def setUp(self):
        self.definition = next(
            item for item in PUBLIC_STRATEGY_CATALOG if item.strategy_id == "time_series_momentum"
        )

    def test_unvalidated_version_has_clear_missing_evidence_and_activation_state(self):
        record = StrategyRecord(self.definition)

        rendered = format_strategy_report(
            record, checked_on=None, paper_progress="0/30일"
        )

        self.assertIn("time_series_momentum v1.0.0", rendered)
        self.assertIn("최근 검증: 없음", rendered)
        self.assertIn("정상 모의검증 진행: 0/30일", rendered)
        self.assertIn("아직 검증 보고서가 없습니다", rendered)
        self.assertIn("별도 소유자 승인", rendered)
        self.assertIn("검증자료 없음", format_strategy_overview_requirement(record))

    def test_partial_version_report_shows_cost_evidence_and_unmet_requirements(self):
        from test_strategies import passing_report

        report = replace(
            passing_report(),
            strategy_id=self.definition.strategy_id,
            version=self.definition.version,
            out_of_sample=False,
            walk_forward_windows=1,
            includes_fees_taxes_slippage=False,
            cost_source_urls=("https://example.test/fees",),
            cost_assumptions=("매도세금만 반영",),
        )
        record = StrategyRecord(self.definition)
        apply_report(record, report)

        rendered = format_strategy_report(
            record,
            checked_on=date(2026, 7, 17),
            paper_progress="4/30일",
        )

        self.assertIn("v1.0.0", rendered)
        self.assertIn("최근 검증: 2026-07-17", rendered)
        self.assertIn("표본 외 미통과", rendered)
        self.assertIn("워크포워드 1회", rendered)
        self.assertIn("https://example.test/fees", rendered)
        self.assertIn("매도세금만 반영", rendered)
        self.assertIn("표본 외 검증이 없습니다", rendered)
        self.assertIn("수수료·세금·슬리피지 반영이 확인되지 않았습니다", rendered)
        self.assertIn("모의검증 진행: 4/30일", rendered)

    def test_invalid_metric_values_are_unavailable_and_explain_rejection(self):
        from test_strategies import passing_report

        report = replace(
            passing_report(),
            strategy_id=self.definition.strategy_id,
            version=self.definition.version,
            sample_years=10**400,
            cost_assumptions=(
                "slippage_5_bps_each_side",
                "apply_2026_costs_outside_disclosed_effective_period",
            ),
        )
        record = StrategyRecord(self.definition)
        apply_report(record, report)

        rendered = format_strategy_report(
            record, checked_on=date(2026, 7, 17), paper_progress="확인 불가"
        )

        self.assertIn("표본 확인 불가", rendered)
        self.assertIn("수치나 자료 형식이 유효하지 않아 검증이 거부됐습니다", rendered)
        self.assertIn("편도 5bp 슬리피지 가정", rendered)
        self.assertIn("공개 적용기간 밖의 2026 비용표를 사용한 추정 가정", rendered)
        self.assertNotIn("1e+", rendered)


if __name__ == "__main__":
    unittest.main()
