from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Messenger = Literal["telegram"]
BrokerProvider = Literal["toss_invest", "mirae_asset"]


@dataclass(frozen=True)
class ArchitectureChoice:
    messenger: Messenger = "telegram"
    broker_provider: BrokerProvider = "toss_invest"
    price_and_account_path: str = "toss_invest_rest"
    investor_flow_path: str = "toss_invest_investor_trading_with_krx_eod_crosscheck"
    disclosure_path: str = "open_dart"
    macro_path: str = "bok_ecos"
    research_path: str = "official_web_and_primary_research"
    llm_policy: str = "explanation_and_unstructured_research_only"


DEFAULT_ARCHITECTURE = ArchitectureChoice()
