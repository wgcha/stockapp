from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable

from .toss import TossInvestClient, TossInvestToken


class TossTokenManager:
    """Cache the single valid Toss token and refresh shortly before expiration."""

    def __init__(
        self,
        client: TossInvestClient,
        *,
        clock: Callable[[], datetime] | None = None,
        refresh_skew_seconds: int = 60,
    ) -> None:
        if refresh_skew_seconds < 0:
            raise ValueError("refresh skew cannot be negative")
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self._token: TossInvestToken | None = None
        self._expires_at: datetime | None = None
        self._lock = Lock()

    def get_token(self) -> TossInvestToken:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("token manager clock must return an aware datetime")
        if self._is_valid(now):
            assert self._token is not None
            return self._token
        with self._lock:
            now = self.clock()
            if self._is_valid(now):
                assert self._token is not None
                return self._token
            token = self.client.issue_token()
            lifetime = token.expires_in_seconds or 300
            self._token = token
            self._expires_at = now + timedelta(seconds=lifetime)
            return token

    def __call__(self) -> TossInvestToken:
        return self.get_token()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    def _is_valid(self, now: datetime) -> bool:
        return (
            self._token is not None
            and self._expires_at is not None
            and now + self.refresh_skew < self._expires_at
        )
