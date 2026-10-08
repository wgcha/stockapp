import unittest
from datetime import datetime, timezone

from stock_guide_agent.http import HttpResponse
from stock_guide_agent.news import NaverNewsHubClient, NewsArticle, score_material_news


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class NewsTests(unittest.TestCase):
    def test_new_api_hub_schema_and_secret_redaction(self):
        requests = []

        def transport(request):
            requests.append(request)
            return HttpResponse(
                200,
                {},
                {
                    "items": [
                        {
                            "title": "<b>기업</b> 뉴스",
                            "originallink": "https://publisher.example/story",
                            "description": "설명",
                            "pubDate": "Fri, 17 Jul 2026 09:00:00 +0000",
                        }
                    ]
                },
            )

        client = NaverNewsHubClient("CLIENT", "SECRET", transport=transport)
        articles = client.search("기업 주식")

        self.assertEqual(articles[0].title, "기업 뉴스")
        self.assertTrue(requests[0].url.endswith("/search/v1/news"))
        redacted = requests[0].redacted()
        self.assertEqual(redacted["headers"]["X-NCP-APIGW-API-KEY"], "***")
        self.assertNotIn("SECRET", str(redacted))

    def test_two_independent_publishers_are_required_for_low_weight_signal(self):
        first = NewsArticle("A사 횡령 발생", "https://one.example/a", NOW)
        duplicate_domain = NewsArticle("A사 횡령", "https://one.example/b", NOW)
        second = NewsArticle("A사 횡령 공시", "https://two.example/c", NOW)

        self.assertIsNone(score_material_news([first, duplicate_domain]))
        signal = score_material_news([first, second])

        self.assertIsNotNone(signal)
        self.assertEqual(signal.normalized_score, -0.35)
        self.assertEqual(len(signal.reference_urls), 2)

    def test_negated_or_conflicting_material_news_is_not_scored(self):
        cleared = NewsArticle("A사 횡령 의혹 무혐의", "https://one.example/a", NOW)
        positive1 = NewsArticle("A사 자사주 소각", "https://two.example/a", NOW)
        positive2 = NewsArticle("A사 자사주 소각", "https://three.example/a", NOW)
        negative1 = NewsArticle("A사 거래정지", "https://four.example/a", NOW)
        negative2 = NewsArticle("A사 거래정지", "https://five.example/a", NOW)

        self.assertIsNone(score_material_news([cleared]))
        self.assertIsNone(
            score_material_news([positive1, positive2, negative1, negative2])
        )


if __name__ == "__main__":
    unittest.main()
