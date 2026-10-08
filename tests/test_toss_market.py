import unittest
from datetime import datetime, timedelta, timezone

from stock_guide_agent.execution import ExecutionState
from stock_guide_agent.intent import parse_investment_intent
from stock_guide_agent.state import Holding
from stock_guide_agent.toss import TossInvestClient, TossInvestResponse, TossInvestToken
from stock_guide_agent.toss_market import (
    TossGuideInputFactory,
    TossPayloadError,
    analyze_investor_flow,
    analyze_investor_flow_breakdown,
    analyze_technical_levels,
    analyze_volatility_target,
    daily_bars_from_toss_candles,
    parse_price_quote,
    parse_holdings_daily_pnl_pct,
)
from stock_guide_agent.http import HttpRequest, HttpResponse


NOW = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)


def price_payload():
    return {
        "result": [
            {
                "symbol": "005930",
                "timestamp": NOW.isoformat(),
                "lastPrice": "80000",
                "currency": "KRW",
            }
        ]
    }


def candle_payload(count: int = 60):
    return {
        "result": {
            "candles": [
                {
                    "timestamp": NOW.isoformat(),
                    "openPrice": str(79500 - index * 100),
                    "highPrice": str(80500 - index * 100),
                    "lowPrice": str(79000 - index * 100),
                    "closePrice": str(80000 - index * 100),
                    "volume": "1000",
                }
                for index in range(count)
            ]
        }
    }


def flow_payload():
    return {
        "result": {
            "records": [
                {
                    "date": "2026-07-17",
                    "updatedAt": NOW.isoformat(),
                    "individual": {"buyAmount": "1000", "sellAmount": "1300"},
                    "foreigner": {"buyAmount": "1000", "sellAmount": "800"},
                    "institution": {"buyAmount": "1000", "sellAmount": "900"},
                    "otherCorporation": {"buyAmount": "1000", "sellAmount": "1000"},
                }
            ]
        }
    }


class FakeTransport:
    def __init__(self):
        self.responses = [price_payload(), candle_payload(), flow_payload()]
        self.requests: list[HttpRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        return HttpResponse(200, {}, self.responses.pop(0))


class TossMarketAdapterTests(unittest.TestCase):
    def test_dominant_long_term_allocation_selects_200_day_approved_engine(self) -> None:
        transport = FakeTransport()
        transport.responses[1] = candle_payload(200)
        factory = TossGuideInputFactory(
            TossInvestClient("CLIENT", "SECRET", transport=transport),
            TossInvestToken("TOKEN", "Bearer", 3600),
            ExecutionState(),
            strategy_id="time_series_momentum",
            strategy_approved=True,
            long_term_strategy_id="long_term_absolute_momentum",
            long_term_strategy_approved=True,
        )
        intent = parse_investment_intent(
            "장기안정 60% 단타 40%, 연간 8%, 하루 손실 1%"
        )

        value = factory(30, "005930", intent, None, NOW)

        self.assertEqual(value.strategy_id, "long_term_absolute_momentum")
        self.assertTrue(value.strategy_approved)
        candle_request = next(
            request for request in transport.requests if request.url.endswith("/candles")
        )
        self.assertEqual(candle_request.query["count"], "200")
        self.assertEqual(value.signal_confidence, 1)

    def test_long_history_candles_convert_to_unique_chronological_daily_bars(self) -> None:
        bars = daily_bars_from_toss_candles(
            [
                {
                    "timestamp": "2026-07-17T09:00:00+09:00",
                    "closePrice": "72000",
                },
                {
                    "timestamp": "2026-07-16T09:00:00+09:00",
                    "closePrice": "71000",
                },
                {
                    "timestamp": "2026-07-17T15:30:00+09:00",
                    "closePrice": "72000",
                },
            ]
        )

        self.assertEqual(
            [bar.trading_date.isoformat() for bar in bars],
            ["2026-07-16", "2026-07-17"],
        )
        self.assertEqual([bar.adjusted_close for bar in bars], [71000, 72000])

        with self.assertRaisesRegex(ValueError, "conflicting"):
            daily_bars_from_toss_candles(
                [
                    {
                        "timestamp": "2026-07-17T09:00:00+09:00",
                        "closePrice": "72000",
                    },
                    {
                        "timestamp": "2026-07-17T15:30:00+09:00",
                        "closePrice": "73000",
                    },
                ]
            )

    def test_volatility_target_uses_dated_closes_and_never_leverages(self):
        candles = []
        price = 100.0
        for index in range(30):
            price *= 1 + (0.03 if index % 2 == 0 else -0.025)
            candles.append(
                {
                    "timestamp": (NOW - timedelta(days=29 - index)).isoformat(),
                    "closePrice": str(price),
                }
            )
        response = TossInvestResponse({"result": {"candles": candles[::-1]}})

        signal = analyze_volatility_target(
            response, lookback=20, annual_target_volatility=0.10
        )

        self.assertEqual(signal.observed_at, NOW)
        self.assertGreater(signal.annualized_volatility, 0.10)
        self.assertGreaterEqual(signal.target_exposure, 0)
        self.assertLess(signal.target_exposure, 1)
    def test_holdings_daily_decimal_rate_becomes_percentage_points(self):
        value = parse_holdings_daily_pnl_pct(
            TossInvestResponse(
                {"result": {"dailyProfitLoss": {"rate": "-0.0141"}}}
            )
        )

        self.assertEqual(value, -1.41)

    def test_factory_compares_user_watchlist_and_exposes_best_alternative(self):
        class AlternativeClient:
            stale_alternative = False

            def get_prices(self, symbols, token):
                values = {"005930": 80000, "000660": 100}
                return TossInvestResponse(
                    {
                        "result": [
                            {
                                "symbol": symbol,
                                "timestamp": (
                                    NOW - timedelta(minutes=10)
                                    if symbol == "000660" and self.stale_alternative
                                    else NOW
                                ).isoformat(),
                                "lastPrice": str(values[symbol]),
                                "currency": "KRW",
                            }
                            for symbol in symbols
                        ]
                    }
                )

            def get_candles(self, symbol, token, **kwargs):
                if symbol == "005930":
                    closes = [80000.0] * 60
                else:
                    closes = [100.0 - index * 0.8 for index in range(60)]
                return TossInvestResponse(
                    {
                        "result": {
                            "candles": [
                                {
                                    "closePrice": str(value),
                                    "lowPrice": str(value * 0.99),
                                    "highPrice": str(value * 1.01),
                                }
                                for value in closes
                            ]
                        }
                    }
                )

            def get_investor_trading(self, symbol, token, **kwargs):
                return TossInvestResponse(flow_payload())

        client = AlternativeClient()
        factory = TossGuideInputFactory(
            client,  # type: ignore[arg-type]
            TossInvestToken("TOKEN", "Bearer", 3600),
            ExecutionState(),
            strategy_id="approved_strategy",
            strategy_approved=True,
            alternative_symbols=lambda user_id, current: ["000660"],
        )
        intent = parse_investment_intent("장기 100%, 연간 8% 목표")

        value = factory(
            30, "005930", intent, Holding(30, "005930", 10, 70000, NOW), NOW
        )

        self.assertEqual(value.alternative_stock_code, "000660")
        self.assertGreater(value.alternative_score, value.signal_score + 0.35)
        self.assertGreaterEqual(value.alternative_confidence, 0.5)

        client.stale_alternative = True
        stale = factory(
            30, "005930", intent, Holding(30, "005930", 10, 70000, NOW), NOW
        )
        self.assertIsNone(stale.alternative_stock_code)

    def test_parses_official_price_and_rejects_missing_symbol(self) -> None:
        quote = parse_price_quote(TossInvestResponse(price_payload()), "005930")
        self.assertEqual(quote.price, 80000)
        with self.assertRaises(TossPayloadError):
            parse_price_quote(TossInvestResponse(price_payload()), "000660")

    def test_twenty_session_reference_levels_are_descriptive_and_ordered(self):
        levels = analyze_technical_levels(TossInvestResponse(candle_payload()))

        self.assertLessEqual(levels.support_price, levels.entry_reference_price)
        self.assertLess(levels.entry_reference_price, levels.take_profit_reference_price)
        self.assertLessEqual(levels.take_profit_reference_price, levels.resistance_price)

    def test_normalizes_foreign_and_institution_net_flow(self) -> None:
        score, observed_at = analyze_investor_flow(TossInvestResponse(flow_payload()))
        self.assertGreater(score, 0)
        self.assertEqual(observed_at, NOW)

        breakdown = analyze_investor_flow_breakdown(
            TossInvestResponse(flow_payload())
        )
        self.assertEqual(breakdown.foreign_net_amount, 200)
        self.assertEqual(breakdown.institution_net_amount, 100)
        self.assertEqual(breakdown.individual_net_amount, -300)
        self.assertIn("외국인 +200원", breakdown.summary())
        self.assertIn("기관 +100원", breakdown.summary())
        self.assertIn("개인 -300원", breakdown.summary())

    def test_factory_builds_position_and_market_snapshot_from_toss(self) -> None:
        transport = FakeTransport()
        state = ExecutionState()
        factory = TossGuideInputFactory(
            TossInvestClient("CLIENT", "SECRET", transport=transport),
            TossInvestToken("TOKEN", "Bearer", 3600),
            state,
            strategy_id="approved_strategy",
            strategy_approved=True,
        )
        intent = parse_investment_intent("장기 100%, 연간 8% 목표")
        holding = Holding(30, "005930", 10, 70000, NOW)

        value = factory(30, "005930", intent, holding, NOW)

        self.assertEqual(value.position.current_price, 80000)
        self.assertEqual(value.position.quantity, 10)
        self.assertGreater(value.signal_score, 0)
        self.assertEqual(value.signal_confidence, 1)
        self.assertEqual(value.market.regime, "risk_on")
        self.assertGreaterEqual(value.market.confidence, 0.4)
        flow_evidence = next(
            item for item in value.market.evidence if item.category == "investor_flow"
        )
        self.assertIn("외국인", flow_evidence.summary)
        self.assertIn("기관", flow_evidence.summary)
        self.assertIn("개인", flow_evidence.summary)
        self.assertEqual(transport.requests[0].url.split("/")[-1], "prices")


if __name__ == "__main__":
    unittest.main()
