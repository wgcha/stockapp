import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from stock_guide_agent.academic_discovery import CrossrefAcademicDiscoveryClient
from stock_guide_agent.http import HttpResponse
from stock_guide_agent.research_queue import SQLiteResearchQueue


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self) -> None:
        self.request = None

    def __call__(self, request):
        self.request = request
        relevant = {
            "DOI": "10.1000/MOMENTUM.1",
            "title": ["A Momentum Strategy for Equity Portfolio Trading"],
            "published-online": {"date-parts": [[2026, 6, 1]]},
            "author": [{"given": "Ada", "family": "Kim"}],
            "URL": "https://doi.org/10.1000/MOMENTUM.1",
        }
        return HttpResponse(
            200,
            {},
            {
                "message": {
                    "items": [
                        relevant,
                        relevant,
                        {
                            "DOI": "10.1000/IRRELEVANT.1",
                            "title": ["Momentum in Fluid Dynamics"],
                            "published": {"date-parts": [[2026]]},
                        },
                    ]
                }
            },
        )


class AcademicDiscoveryTests(unittest.TestCase):
    def test_crossref_metadata_is_filtered_and_deduplicated(self) -> None:
        transport = FakeTransport()
        client = CrossrefAcademicDiscoveryClient(
            mailto="owner@example.com", transport=transport
        )

        candidates = client.discover(since=date(2026, 1, 1), rows=20)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].doi, "10.1000/MOMENTUM.1")
        self.assertIn("momentum", candidates[0].matched_terms)
        self.assertIn("portfolio", candidates[0].matched_terms)
        self.assertEqual(transport.request.query["mailto"], "owner@example.com")
        self.assertIn("from-pub-date:2026-01-01", transport.request.query["filter"])

    def test_candidates_enter_review_queue_but_not_strategy_registry(self) -> None:
        transport = FakeTransport()
        candidate = CrossrefAcademicDiscoveryClient(transport=transport).discover(
            since=date(2026, 1, 1)
        )[0]
        with tempfile.TemporaryDirectory() as directory:
            queue = SQLiteResearchQueue(Path(directory) / "research.sqlite3")

            self.assertEqual(
                queue.enqueue_academic_candidates((candidate,), now=NOW), 1
            )
            self.assertEqual(
                queue.enqueue_academic_candidates((candidate,), now=NOW), 0
            )
            pending = queue.list_pending_academic_candidates()

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].status, "pending")
            self.assertEqual(pending[0].doi, candidate.doi.lower())


if __name__ == "__main__":
    unittest.main()
