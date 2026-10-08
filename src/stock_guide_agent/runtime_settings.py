"""Common bounded numeric parsing for preflight and runtime construction."""
from __future__ import annotations

import math
from typing import Mapping


# name: (default, minimum, maximum, integer)
NUMERIC_SETTINGS = {
    "AGENT_ACCOUNT_EQUITY_KRW": (None, 0.01, 1e15, False),
    "AGENT_MAX_ORDER_NOTIONAL": ("1000000", 0.01, 1e15, False),
    "AGENT_MAX_ORDERS_PER_DAY": ("5", 1, 10000, True),
    "AGENT_MAX_DAILY_LOSS_PCT": ("1.5", 0.001, 100, False),
    "DAILY_GUIDE_HOUR_KST": ("9", 0, 23, True),
    "DAILY_GUIDE_MINUTE_KST": ("10", 0, 59, True),
    "PAPER_INITIAL_CAPITAL_KRW": ("10000000", 0.01, 1e15, False),
    "PAPER_EVALUATION_MAX_SYMBOLS": ("20", 1, 200, True),
    "INTRADAY_MINIMUM_BARS": ("300", 60, 500, True),
    "INTRADAY_ARCHIVE_MAX_SYMBOLS": ("5", 0, 20, True),
    "MIRAE_PAPER_COMMISSION_RATE": (None, 0, 0.099999999, False),
    "NEWS_MAX_SYMBOLS": ("20", 1, 200, True),
    "CROSSREF_DISCOVERY_ROWS": ("20", 1, 100, True),
}


def validate_runtime_settings(environment: Mapping[str, str]) -> dict[str, str]:
    values = {}
    for name, (default, minimum, maximum, integer) in NUMERIC_SETTINGS.items():
        raw = environment.get(name, default)
        if default is None and (raw is None or not raw.strip()):
            continue
        try:
            parsed = int(raw) if integer else float(raw)
            if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"{name} must be finite and within the supported range") from None
        values[name] = str(parsed)
    for name, default in (("CROSSREF_DISCOVERY_ENABLED", "true"),
                          ("AGENT_LIVE_TRADING_ENABLED", "false"),
                          ("TOSSINVEST_LIVE_ORDERS", "false")):
        raw = environment.get(name, default).strip().lower()
        if raw not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        values[name] = raw
    return values
