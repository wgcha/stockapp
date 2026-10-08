import unittest
from datetime import datetime, timedelta, timezone

from stock_guide_agent.http import HttpRequest, HttpResponse
from stock_guide_agent.tokens import TossTokenManager
from stock_guide_agent.toss import TossInvestClient


class Clock:
    def __init__(self):
        self.now = datetime(2026, 7, 17, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self):
        self.requests: list[HttpRequest] = []
        self.count = 0

    def __call__(self, request):
        self.requests.append(request)
        self.count += 1
        return HttpResponse(
            200,
            {},
            {
                "access_token": f"TOKEN-{self.count}",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )


class TokenManagerTests(unittest.TestCase):
    def test_reuses_token_and_refreshes_before_expiry(self) -> None:
        clock = Clock()
        transport = FakeTransport()
        manager = TossTokenManager(
            TossInvestClient("CLIENT", "SECRET", transport=transport),
            clock=clock,
            refresh_skew_seconds=60,
        )

        first = manager.get_token()
        second = manager.get_token()
        clock.now += timedelta(seconds=3541)
        third = manager.get_token()

        self.assertIs(first, second)
        self.assertEqual(first.access_token, "TOKEN-1")
        self.assertEqual(third.access_token, "TOKEN-2")
        self.assertEqual(len(transport.requests), 2)

    def test_invalidate_forces_refresh(self) -> None:
        transport = FakeTransport()
        manager = TossTokenManager(
            TossInvestClient("CLIENT", "SECRET", transport=transport)
        )
        manager.get_token()
        manager.invalidate()
        refreshed = manager.get_token()
        self.assertEqual(refreshed.access_token, "TOKEN-2")


if __name__ == "__main__":
    unittest.main()
