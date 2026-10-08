import unittest

from stock_guide_agent.intent import merge_investment_intents, parse_investment_intent


class InvestmentIntentTests(unittest.TestCase):
    def test_simple_mixed_strategy_and_aggressive_daily_target(self) -> None:
        intent = parse_investment_intent(
            "\uc774\ubc88 \ud55c\uc8fc\ub294 \ub9e4\uc77c 3% \uc774\uc775 \ubcf4\uace0 \uc2f6\uc5b4. "
            "\uc7a5\uae30\uc548\uc815 60% \ub2e8\ud0c0 40%\ub85c \ud574\uc918"
        )

        self.assertEqual(intent.horizon, "week")
        self.assertEqual(intent.valid_for_days, 7)
        self.assertEqual(intent.target_return_pct, 3.0)
        self.assertEqual(intent.target_return_period, "daily")
        self.assertEqual(
            {item.strategy: item.weight_pct for item in intent.allocations},
            {"long_term_stable": 60.0, "day_trading": 40.0},
        )
        self.assertEqual(intent.max_trade_loss_pct, 0.5)
        self.assertEqual(intent.max_daily_loss_pct, 1.5)
        self.assertTrue(intent.warnings)
        self.assertTrue(any("연 환산" in item for item in intent.risk_explanations))
        self.assertTrue(any("장기 배분 60%" in item for item in intent.warnings))
        self.assertTrue(any("연간 6~12%" in item for item in intent.suggested_guardrails))

    def test_plain_korean_percent_risk_frequency_and_approval_are_structured(self) -> None:
        intent = parse_investment_intent(
            "이번 한주는 매일 3퍼센트, 장기안정 60퍼 단타 40퍼, "
            "거래당 손실 0.4퍼 하루 손실 1.2퍼, 하루 2회, 자동주문"
        )

        self.assertEqual(intent.target_return_pct, 3)
        self.assertEqual(intent.max_trade_loss_pct, 0.4)
        self.assertEqual(intent.max_daily_loss_pct, 1.2)
        self.assertEqual(intent.max_trades_per_day, 2)
        self.assertEqual(intent.trading_frequency, "medium")
        self.assertEqual(intent.approval_level, "owner_each_order")
        self.assertTrue(any("가이드만 제공" in warning for warning in intent.warnings))
        self.assertTrue(any("자동주문을 실행하지 않습니다" in warning for warning in intent.warnings))
        self.assertTrue(any("증권사 앱에서 직접 진행" in warning for warning in intent.warnings))

    def test_guide_only_mode_is_understood(self) -> None:
        intent = parse_investment_intent(
            "장기 100%, 연간 8%, 일일 손실한도 1%, 가이드만"
        )

        self.assertEqual(intent.approval_level, "guide_only")
        self.assertEqual(intent.max_daily_loss_pct, 1)

    def test_allocation_total_must_be_one_hundred(self) -> None:
        intent = parse_investment_intent("\uc7a5\uae30 50% \uc2a4\uc719 30%")

        self.assertIn("remaining_allocation", intent.missing_fields)
        self.assertTrue(any("80%" in warning for warning in intent.warnings))
        self.assertTrue(any("20%" in item and "현금" in item for item in intent.suggested_guardrails))

    def test_low_frequency_conflicts_with_repeated_short_term_target(self) -> None:
        intent = parse_investment_intent(
            "이번주는 매일 1% 목표, 스윙 100%, 거래 적게"
        )

        self.assertEqual(intent.trading_frequency, "low")
        self.assertTrue(any("거래빈도" in item for item in intent.warnings))
        self.assertTrue(any("월간·연간" in item for item in intent.suggested_guardrails))

    def test_missing_fields_are_explicit(self) -> None:
        intent = parse_investment_intent("\uc0bc\uc131\uc804\uc790\ub97c \uacc4\uc18d \ub4e4\uace0 \uac08\uae4c?")

        self.assertIn("target_return", intent.missing_fields)
        self.assertIn("strategy_allocation", intent.missing_fields)
        self.assertIn("max_daily_loss", intent.missing_fields)

    def test_partial_conversation_preserves_prior_settings_and_stricter_risk(self) -> None:
        allocation = parse_investment_intent(
            "장기안정 60% 단타 40%, 거래당 손실 0.3%, 하루 손실 1%"
        )
        target = parse_investment_intent("이번 주는 매일 3% 목표")
        merged = merge_investment_intents(allocation, target)

        self.assertEqual(
            {item.strategy: item.weight_pct for item in merged.allocations},
            {"long_term_stable": 60, "day_trading": 40},
        )
        self.assertEqual(merged.target_return_pct, 3)
        self.assertEqual(merged.valid_for_days, 7)
        self.assertEqual(merged.max_trade_loss_pct, 0.3)
        self.assertEqual(merged.max_daily_loss_pct, 1)

        revised = merge_investment_intents(
            merged, parse_investment_intent("하루 손실은 0.8%로 해줘")
        )
        self.assertEqual(revised.max_daily_loss_pct, 0.8)
        self.assertEqual(revised.target_return_pct, 3)
        self.assertEqual(len(revised.allocations), 2)


if __name__ == "__main__":
    unittest.main()
