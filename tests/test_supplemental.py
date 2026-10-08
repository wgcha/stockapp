import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_guide_agent.cache import CachedPayload, SQLiteMarketCache
from stock_guide_agent.market import build_market_snapshot
from stock_guide_agent.supplemental import CachedSupplementalObservationProvider


NOW = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)


class SupplementalObservationTests(unittest.TestCase):
    def put(self, cache, category, key, payload, *, observed=NOW):
        cache.put(CachedPayload("source", category, key, payload, observed, NOW))

    def test_only_auditable_scored_items_are_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteMarketCache(Path(directory) / "cache.sqlite3")
            self.put(
                cache,
                "news",
                "valid",
                {
                    "normalized_score": -0.6,
                    "reference_url": "https://example.com/story",
                    "stock_codes": ["005930"],
                    "source_tier": 2,
                },
            )
            self.put(cache, "news", "no-url", {"normalized_score": 0.8})
            self.put(
                cache,
                "macro",
                "wrong-stock",
                {
                    "normalized_score": 0.5,
                    "reference_url": "https://ecos.bok.or.kr/api/",
                    "stock_codes": ["000660"],
                },
            )

            observations = CachedSupplementalObservationProvider(cache)("005930")

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].category, "news")
            self.assertEqual(observations[0].value, -0.6)

    def test_material_disclosures_are_filtered_by_stock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteMarketCache(Path(directory) / "cache.sqlite3")
            self.put(
                cache,
                "disclosure",
                "negative",
                {"stock_code": "005930", "report_name": "횡령 발생"},
            )
            self.put(
                cache,
                "disclosure",
                "unknown",
                {"stock_code": "005930", "report_name": "기업설명회 개최"},
            )
            self.put(
                cache,
                "disclosure",
                "other-stock",
                {"stock_code": "000660", "report_name": "상장폐지 결정"},
            )

            observations = CachedSupplementalObservationProvider(cache)("005930")

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].metric, "material_negative_disclosure")
            self.assertLess(observations[0].value, 0)

    def test_stale_supplemental_observation_is_excluded_by_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteMarketCache(Path(directory) / "cache.sqlite3")
            stale = NOW - timedelta(hours=2)
            self.put(
                cache,
                "overseas",
                "SPY",
                {"normalized_score": 0.9, "source_tier": 1},
                observed=stale,
            )

            observations = CachedSupplementalObservationProvider(cache)("005930")
            snapshot = build_market_snapshot(observations, as_of=NOW)

            self.assertEqual(snapshot.evidence, [])
            self.assertEqual(snapshot.regime, "insufficient_data")


if __name__ == "__main__":
    unittest.main()
