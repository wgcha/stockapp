import unittest
from datetime import datetime, timedelta, timezone

from stock_guide_agent.market import (
    MarketObservation,
    build_market_snapshot,
    format_market_snapshot,
)


NOW = datetime(2026, 7, 17, 1, 0, tzinfo=timezone.utc)


def observation(
    category: str,
    value: float,
    *,
    age_minutes: int = 1,
    source_id: str = "official",
    metric: str = "metric",
    summary: str = "",
    reference_url: str | None = None,
) -> MarketObservation:
    observed_at = NOW - timedelta(minutes=age_minutes)
    return MarketObservation(
        source_id=source_id,
        category=category,  # type: ignore[arg-type]
        metric=metric,
        value=value,
        observed_at=observed_at,
        fetched_at=observed_at + timedelta(seconds=10),
        source_tier=1,
        official=True,
        summary=summary,
        reference_url=reference_url,
    )


class MarketSnapshotTests(unittest.TestCase):
    def test_compact_formatter_exposes_regime_confidence_and_category_scores(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation("price", 0.7),
                observation("investor_flow", 0.6),
                observation("macro", 0.4, age_minutes=30),
                observation("overseas", 0.5),
            ],
            as_of=NOW,
        )

        rendered = format_market_snapshot(snapshot)

        self.assertIn("시황: 위험선호", rendered)
        self.assertIn("신뢰", rendered)
        self.assertIn("가격", rendered)
        self.assertIn("수급", rendered)
        self.assertIn("거시", rendered)
        self.assertIn("해외", rendered)

    def test_formatter_exposes_evidence_source_age_and_reference(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation(
                    "price",
                    0.7,
                    source_id="toss_invest_prices",
                    summary="005930 절대 모멘텀",
                    reference_url="https://example.com/price",
                ),
                observation("investor_flow", 0.6),
                observation("macro", 0.2, age_minutes=30),
                observation("overseas", 0.3),
            ],
            as_of=NOW,
        )

        rendered = format_market_snapshot(snapshot)

        self.assertIn("토스증권 시세·가격·1분 전", rendered)
        self.assertIn("005930 절대 모멘텀", rendered)
        self.assertIn("https://example.com/price", rendered)

    def test_formatter_calls_out_insufficient_market_data(self) -> None:
        snapshot = build_market_snapshot([], as_of=NOW)

        rendered = format_market_snapshot(snapshot)

        self.assertIn("시황: 자료부족", rendered)
        self.assertIn("신규진입 판단을 보수적으로 제한", rendered)

    def test_builds_risk_on_snapshot_from_broad_fresh_evidence(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation("price", 0.7),
                observation("investor_flow", 0.6),
                observation("macro", 0.4, age_minutes=30),
                observation("overseas", 0.5),
                observation("news", 0.3),
            ],
            as_of=NOW,
        )

        self.assertEqual(snapshot.regime, "risk_on")
        self.assertGreater(snapshot.confidence, 0.5)
        self.assertFalse(any(item.startswith("missing_") for item in snapshot.warnings))

    def test_stale_observations_are_excluded_and_coverage_is_explicit(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation("price", 0.9, age_minutes=10),
                observation("macro", 0.3, age_minutes=30),
            ],
            as_of=NOW,
        )

        self.assertEqual(snapshot.regime, "insufficient_data")
        self.assertIn("missing_price", snapshot.warnings)
        self.assertIn("missing_investor_flow", snapshot.warnings)
        self.assertTrue(any(item.startswith("stale_price_") for item in snapshot.warnings))
        self.assertIn("오래된 가격 자료 제외", format_market_snapshot(snapshot))

    def test_conflicting_signals_are_reported(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation("price", 0.8, source_id="source_a"),
                observation("price", -0.7, source_id="source_b"),
                observation("investor_flow", 0.1),
                observation("macro", 0.1, age_minutes=30),
                observation("overseas", 0.1),
            ],
            as_of=NOW,
        )

        self.assertIn("conflicting_price_signals", snapshot.warnings)
        self.assertLess(snapshot.confidence, 0.8)

    def test_future_timestamp_is_excluded_fail_closed(self) -> None:
        future = MarketObservation(
            source_id="clock_error",
            category="price",
            metric="future",
            value=1,
            observed_at=NOW + timedelta(minutes=1),
            fetched_at=NOW + timedelta(minutes=1),
            source_tier=1,
            official=True,
        )

        snapshot = build_market_snapshot([future], as_of=NOW)

        self.assertEqual(snapshot.regime, "insufficient_data")
        self.assertTrue(
            any(item.startswith("future_timestamp_") for item in snapshot.warnings)
        )
        self.assertIn("미래 시각으로 표시된 자료 제외", format_market_snapshot(snapshot))

    def test_broad_secondary_evidence_cannot_override_stale_critical_price(self) -> None:
        snapshot = build_market_snapshot(
            [
                observation("price", 0.9, age_minutes=10),
                observation("investor_flow", 0.8),
                observation("macro", 0.8, age_minutes=30),
                observation("disclosure", 0.8),
                observation("news", 0.8),
                observation("overseas", 0.8),
            ],
            as_of=NOW,
        )

        self.assertEqual(snapshot.regime, "insufficient_data")
        self.assertIn("missing_price", snapshot.warnings)

    def test_rejects_naive_datetime_and_out_of_range_value(self) -> None:
        with self.assertRaises(ValueError):
            MarketObservation(
                source_id="bad",
                category="price",
                metric="x",
                value=2.0,
                observed_at=NOW,
                fetched_at=NOW,
                source_tier=1,
            )
        with self.assertRaises(ValueError):
            build_market_snapshot([], as_of=datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            format_market_snapshot(build_market_snapshot([], as_of=NOW), max_evidence=-1)


if __name__ == "__main__":
    unittest.main()
