from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataSource:
    source_id: str
    categories: tuple[str, ...]
    tier: int
    official: bool
    purpose: str
    homepage: str
    credentials_required: bool


DEFAULT_SOURCES = (
    DataSource(
        source_id="krx",
        categories=("price",),
        tier=1,
        official=True,
        purpose="Official Korean exchange daily trading data for cross-checking",
        homepage="https://data.krx.co.kr/",
        credentials_required=False,
    ),
    DataSource(
        source_id="open_dart",
        categories=("disclosure",),
        tier=1,
        official=True,
        purpose="Corporate disclosures and financial statements",
        homepage="https://opendart.fss.or.kr/",
        credentials_required=True,
    ),
    DataSource(
        source_id="bok_ecos",
        categories=("macro",),
        tier=1,
        official=True,
        purpose="Korean macroeconomic statistics",
        homepage="https://ecos.bok.or.kr/",
        credentials_required=True,
    ),
    DataSource(
        source_id="toss_invest",
        categories=("price", "investor_flow", "overseas"),
        tier=1,
        official=True,
        purpose="Tradable quotes, account data, orders, and investor trading statistics",
        homepage="https://developers.tossinvest.com/docs",
        credentials_required=True,
    ),
    DataSource(
        source_id="licensed_news",
        categories=("news", "overseas"),
        tier=2,
        official=False,
        purpose="Timestamped market and company news with usage rights",
        homepage="",
        credentials_required=True,
    ),
)
