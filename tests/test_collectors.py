import unittest
from datetime import date

from stock_guide_agent.collectors import (
    ConfigurationError,
    EcosClient,
    OpenDartClient,
)
from stock_guide_agent.http import HttpRequest, HttpResponse


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class OpenDartCollectorTests(unittest.TestCase):
    def test_searches_and_parses_disclosures(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    {
                        "status": "000",
                        "message": "normal",
                        "list": [
                            {
                                "rcept_no": "20260717000001",
                                "corp_name": "Example",
                                "stock_code": "005930",
                                "report_nm": "[\uc815\uc815]\uc8fc\uc694\uc0ac\ud56d\ubcf4\uace0\uc11c",
                                "flr_nm": "Example",
                                "rcept_dt": "20260717",
                            }
                        ],
                    },
                )
            ]
        )
        client = OpenDartClient("KEY", transport=transport)
        events = client.search_disclosures(date(2026, 7, 17), date(2026, 7, 17))

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].corrected)
        self.assertTrue(events[0].detail_url.endswith("20260717000001"))
        request = transport.requests[0]
        self.assertEqual(request.url, "https://opendart.fss.or.kr/api/list.json")
        self.assertEqual(request.query["page_count"], "100")
        self.assertEqual(request.redacted()["query"]["crtfc_key"], "***")

    def test_no_data_status_returns_empty_list(self) -> None:
        transport = FakeTransport([HttpResponse(200, {}, {"status": "013", "message": "none"})])
        client = OpenDartClient("KEY", transport=transport)

        self.assertEqual(client.search_disclosures(date(2026, 7, 17), date(2026, 7, 17)), [])


class EcosCollectorTests(unittest.TestCase):
    def test_searches_and_parses_macro_series(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    {
                        "StatisticSearch": {
                            "list_total_count": 1,
                            "row": [
                                {
                                    "TIME": "20260717",
                                    "DATA_VALUE": "1,389.50",
                                    "ITEM_CODE1": "0000001",
                                    "ITEM_NAME1": "USD/KRW",
                                    "UNIT_NAME": "KRW",
                                }
                            ],
                        }
                    },
                )
            ]
        )
        client = EcosClient("KEY", transport=transport)
        points = client.search(
            "731Y001", "D", "20260717", "20260717", item_code1="0000001"
        )

        self.assertEqual(points[0].value, 1389.5)
        request = transport.requests[0]
        self.assertIn("/StatisticSearch/KEY/json/kr/1/100/731Y001/D/", request.url)
        self.assertNotIn("KEY", request.redacted()["url"])
        self.assertIn("/StatisticSearch/***/json/", request.redacted()["url"])

    def test_no_data_result_returns_empty_list(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    {},
                    {"RESULT": {"CODE": "INFO-200", "MESSAGE": "no data"}},
                )
            ]
        )
        client = EcosClient("KEY", transport=transport)
        self.assertEqual(client.search("731Y001", "D", "20260717", "20260717"), [])


if __name__ == "__main__":
    unittest.main()
