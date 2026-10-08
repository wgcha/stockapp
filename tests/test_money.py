import unittest

from stock_guide_agent.money import (
    parse_account_equity_message,
    parse_korean_money,
)


class KoreanMoneyTests(unittest.TestCase):
    def test_parses_common_korean_account_equity_phrases(self) -> None:
        self.assertEqual(parse_korean_money("1억 5000만"), 150_000_000)
        for text, expected in (
            ("내 투자금은 1,000만원", 10_000_000),
            ("계좌 평가금 1억 5000만원", 150_000_000),
            ("총자산 12,500,000원입니다", 12_500_000),
        ):
            with self.subTest(text=text):
                parsed = parse_account_equity_message(text)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.account_equity, expected)

    def test_unrelated_or_nonpositive_money_is_not_account_equity(self) -> None:
        self.assertIsNone(parse_account_equity_message("삼성전자 1만원"))
        self.assertIsNone(parse_account_equity_message("투자금 0원"))


if __name__ == "__main__":
    unittest.main()
