"""Core components for the stock guide agent."""

from .intent import (
    Allocation,
    InvestmentIntent,
    merge_investment_intents,
    parse_investment_intent,
)
from .intraday import IntradayBar, IntradayPaperDay, SQLiteIntradayBarStore
from .academic_discovery import (
    AcademicStrategyCandidate,
    CrossrefAcademicDiscoveryClient,
)
from .market import (
    MarketObservation,
    MarketSnapshot,
    build_market_snapshot,
    format_market_snapshot,
)
from .money import ParsedAccountEquity, parse_account_equity_message, parse_korean_money
from .order_request import ParsedOrderRequest, parse_order_request
from .telegram import (
    OwnerCommandResult,
    TelegramBotClient,
    format_intent_confirmation,
    parse_telegram_update,
)
from .strategies import (
    PUBLIC_STRATEGY_CATALOG,
    StrategyRecord,
    ValidationReport,
    apply_report,
)
from .portfolio import (
    PersonalizedStrategyPlan,
    RiskOverlaySignal,
    StrategyAttribution,
    compose_strategy_plan,
)
from .collectors import EcosClient, OpenDartClient
from .toss import TossInvestClient
from .brokers import (
    BrokerOrderReceipt,
    BrokerRouter,
    MiraeAssetBrokerGateway,
    TossBrokerGateway,
)
from .toss_market import (
    TossGuideInputFactory,
    InvestorFlowBreakdown,
    VolatilityTarget,
    analyze_volatility_target,
    analyze_investor_flow_breakdown,
    daily_bars_from_toss_candles,
)
from .backtest import (
    DailyBar,
    TradingCostProfile,
    WalkForwardResult,
    mirae_krx_2026_costs,
    toss_krx_2026_costs,
    walk_forward_momentum,
    walk_forward_volatility_managed,
    run_volatility_managed_exposure,
    run_opening_range_breakout,
    walk_forward_opening_range_breakout,
)
from .paper_trading import (
    PaperPortfolioKey,
    PaperPortfolioValuation,
    PaperModelDay,
    PaperModelObservation,
    PaperStrategyEvaluation,
    PaperTradeFill,
    PaperTradingDay,
    SQLitePaperTradingLedger,
    estimate_paper_transaction_cost,
)
from .runtime import AgentRuntime
from .scheduler import ScheduledJobRunner, SQLiteJobStateStore
from .cache import SQLiteMarketCache
from .research_queue import SQLiteResearchQueue
from .tokens import TossTokenManager
from .service import ServiceEvent, StockGuideService
from .state import Holding, SQLiteUserStateStore
from .strategy_store import StrategyActivationEvent, SQLiteStrategyStore
from .scheduler import DEFAULT_JOBS, JobState, due_jobs, initialize_jobs
from .guidance import GuideInput, Position, TradeGuide, format_trade_guide, generate_trade_guide
from .execution import (
    ApprovalGate,
    AuthorizedOrder,
    ExecutionPolicy,
    ExecutionState,
    OrderProposal,
)

__all__ = [
    "Allocation",
    "AcademicStrategyCandidate",
    "AgentRuntime",
    "ApprovalGate",
    "AuthorizedOrder",
    "BrokerOrderReceipt",
    "BrokerRouter",
    "CrossrefAcademicDiscoveryClient",
    "InvestmentIntent",
    "merge_investment_intents",
    "IntradayBar",
    "IntradayPaperDay",
    "MarketObservation",
    "MarketSnapshot",
    "MiraeAssetBrokerGateway",
    "DEFAULT_JOBS",
    "DailyBar",
    "EcosClient",
    "ExecutionPolicy",
    "ExecutionState",
    "JobState",
    "GuideInput",
    "Holding",
    "OpenDartClient",
    "OwnerCommandResult",
    "OrderProposal",
    "PaperTradingDay",
    "ParsedAccountEquity",
    "ParsedOrderRequest",
    "PaperTradeFill",
    "PaperStrategyEvaluation",
    "PaperPortfolioKey",
    "PaperPortfolioValuation",
    "PaperModelDay",
    "PaperModelObservation",
    "PersonalizedStrategyPlan",
    "RiskOverlaySignal",
    "Position",
    "PUBLIC_STRATEGY_CATALOG",
    "StrategyRecord",
    "StrategyActivationEvent",
    "StrategyAttribution",
    "ServiceEvent",
    "SQLiteUserStateStore",
    "SQLiteStrategyStore",
    "SQLitePaperTradingLedger",
    "SQLiteJobStateStore",
    "SQLiteMarketCache",
    "SQLiteIntradayBarStore",
    "SQLiteResearchQueue",
    "StockGuideService",
    "ScheduledJobRunner",
    "TelegramBotClient",
    "TossInvestClient",
    "TossBrokerGateway",
    "TossGuideInputFactory",
    "InvestorFlowBreakdown",
    "VolatilityTarget",
    "TossTokenManager",
    "TradeGuide",
    "TradingCostProfile",
    "ValidationReport",
    "WalkForwardResult",
    "apply_report",
    "analyze_volatility_target",
    "analyze_investor_flow_breakdown",
    "daily_bars_from_toss_candles",
    "build_market_snapshot",
    "compose_strategy_plan",
    "due_jobs",
    "format_intent_confirmation",
    "format_market_snapshot",
    "format_trade_guide",
    "generate_trade_guide",
    "estimate_paper_transaction_cost",
    "parse_investment_intent",
    "parse_account_equity_message",
    "parse_korean_money",
    "parse_order_request",
    "parse_telegram_update",
    "initialize_jobs",
    "mirae_krx_2026_costs",
    "toss_krx_2026_costs",
    "walk_forward_momentum",
    "walk_forward_volatility_managed",
    "run_volatility_managed_exposure",
    "run_opening_range_breakout",
    "walk_forward_opening_range_breakout",
]
