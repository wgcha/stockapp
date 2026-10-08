import unittest

from stock_guide_agent.order_request import parse_order_request


class NaturalOrderRequestTests(unittest.TestCase):
    def test_parses_command_and_natural_buy_sell_requests(self) -> None:
        cases = (
            ("주문 005930 1주 70000원", None, 70_000),
            ("005930 1주를 7만원에 사줘", "buy", 70_000),
            ("005930 70,000원에 2주 매도해줘", "sell", 70_000),
            ("005930 3주 7.5만원에 주문해줘요", None, 75_000),
        )
        for text, side, price in cases:
            with self.subTest(text=text):
                parsed = parse_order_request(text)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.stock_code, "005930")
                self.assertEqual(parsed.requested_side, side)
                self.assertEqual(parsed.limit_price, price)

    def test_rejects_zero_or_unrelated_requests(self) -> None:
        self.assertIsNone(parse_order_request("005930 0주를 7만원에 사줘"))
        self.assertIsNone(parse_order_request("005930 지금 사도 돼?"))


if __name__ == "__main__":
    unittest.main()
