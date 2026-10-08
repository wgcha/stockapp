from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import math
from numbers import Real
from typing import Literal


Horizon = Literal["long_term", "swing", "intraday", "risk_management"]
ResearchKind = Literal["peer_reviewed", "working_paper", "official", "public_principle"]
SignalEngine = Literal[
    "time_series_momentum",
    "volatility_managed_exposure",
    "opening_range_breakout",
]
StrategyStatus = Literal[
    "research",
    "rejected",
    "backtested",
    "paper_trading",
    "validated",
    "approved",
    "suspended",
]


@dataclass(frozen=True)
class ResearchSource:
    title: str
    authors: tuple[str, ...]
    url: str
    kind: ResearchKind
    published_year: int
    notes: str


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    version: str
    display_name: str
    horizon: Horizon
    source: ResearchSource
    applicable_markets: tuple[str, ...]
    thesis: str
    limitations: tuple[str, ...]
    requires_intraday_data: bool = False
    executable: bool = True
    signal_engine: SignalEngine | None = None


@dataclass(frozen=True)
class ValidationReport:
    strategy_id: str
    version: str
    market: str
    sample_years: float
    out_of_sample: bool
    walk_forward_windows: int
    trades: int
    cost_adjusted_sharpe: float
    max_drawdown: float
    parameter_stability: float
    paper_trading_days: int
    includes_fees_taxes_slippage: bool
    data_leakage_check_passed: bool
    signal_engine: SignalEngine | None = None
    cost_source_urls: tuple[str, ...] = ()
    cost_assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionDecision:
    eligible: bool
    status: StrategyStatus
    failures: tuple[str, ...]


@dataclass
class StrategyRecord:
    definition: StrategyDefinition
    status: StrategyStatus = "research"
    reports: list[ValidationReport] = field(default_factory=list)
    last_researched_on: date | None = None
    next_research_due: date | None = None

    def schedule_next_research(self, checked_on: date, cadence_days: int = 90) -> None:
        if cadence_days < 7:
            raise ValueError("research cadence cannot be shorter than 7 days")
        self.last_researched_on = checked_on
        self.next_research_due = checked_on + timedelta(days=cadence_days)

    def research_due(self, today: date) -> bool:
        return self.next_research_due is None or today >= self.next_research_due


def evaluate_for_promotion(
    definition: StrategyDefinition, report: ValidationReport
) -> PromotionDecision:
    """Apply hard validation gates before a strategy can guide real money."""
    # Persisted reports can outlive code versions and may contain malformed JSON
    # values or non-finite numbers. Reject the whole report before threshold
    # comparisons: NaN comparisons are false, which could otherwise make a
    # corrupted report appear to pass a gate.
    numeric_metrics = (
        report.sample_years,
        report.cost_adjusted_sharpe,
        report.max_drawdown,
        report.parameter_stability,
    )
    counts = (
        report.walk_forward_windows,
        report.trades,
        report.paper_trading_days,
    )
    strict_flags = (
        report.out_of_sample,
        report.includes_fees_taxes_slippage,
        report.data_leakage_check_passed,
    )
    def finite_real(value: object) -> bool:
        if not isinstance(value, Real) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except (OverflowError, TypeError, ValueError):
            return False

    metrics_valid = all(finite_real(value) for value in numeric_metrics)
    metrics_valid = metrics_valid and numeric_metrics[0] >= 0
    metrics_valid = metrics_valid and 0 <= numeric_metrics[2] <= 1
    metrics_valid = metrics_valid and 0 <= numeric_metrics[3] <= 1
    metrics_valid = metrics_valid and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in counts
    )
    metrics_valid = metrics_valid and all(
        isinstance(value, bool) for value in strict_flags
    )
    if not metrics_valid:
        return PromotionDecision(False, "rejected", ("invalid_validation_metrics",))

    failures: list[str] = []
    if report.strategy_id != definition.strategy_id or report.version != definition.version:
        failures.append("report_definition_mismatch")
    if report.signal_engine != definition.signal_engine:
        failures.append("report_signal_engine_mismatch")
    if not definition.executable:
        failures.append("public_principle_requires_testable_rules")
    if definition.signal_engine is None:
        failures.append("strategy_signal_engine_not_implemented")
    minimum_years = 2.0 if definition.horizon == "intraday" else 5.0
    minimum_trades = (
        200
        if definition.horizon == "intraday"
        else 10
        if definition.horizon == "long_term"
        else 50
    )
    if report.sample_years < minimum_years:
        failures.append("insufficient_sample_years")
    if not report.out_of_sample:
        failures.append("missing_out_of_sample_test")
    if report.walk_forward_windows < 3:
        failures.append("insufficient_walk_forward_windows")
    if report.trades < minimum_trades:
        failures.append("insufficient_trade_count")
    if report.cost_adjusted_sharpe < 0.5:
        failures.append("weak_cost_adjusted_sharpe")
    if report.max_drawdown > 0.25:
        failures.append("excessive_drawdown")
    if report.parameter_stability < 0.6:
        failures.append("parameter_instability")
    if report.paper_trading_days < 30:
        failures.append("insufficient_paper_trading")
    if not report.includes_fees_taxes_slippage:
        failures.append("missing_realistic_costs")
    if not report.data_leakage_check_passed:
        failures.append("data_leakage_risk")

    if failures:
        status: StrategyStatus = (
            "rejected"
            if (
                "data_leakage_risk" in failures
                or "report_definition_mismatch" in failures
                or "report_signal_engine_mismatch" in failures
            )
            else "paper_trading"
        )
        return PromotionDecision(False, status, tuple(failures))
    # Passing automated gates proves technical eligibility only. A configured
    # owner must explicitly activate this exact strategy version.
    return PromotionDecision(True, "validated", ())


def apply_report(record: StrategyRecord, report: ValidationReport) -> PromotionDecision:
    decision = evaluate_for_promotion(record.definition, report)
    record.reports.append(report)
    record.status = decision.status
    return decision


PUBLIC_STRATEGY_CATALOG = (
    StrategyDefinition(
        strategy_id="buffett_long_horizon_business_quality_principle",
        version="1.0.0",
        display_name="Buffett long-horizon business quality principle",
        horizon="long_term",
        source=ResearchSource(
            title="Berkshire Hathaway 2024 Annual Letter",
            authors=("Warren E. Buffett",),
            url="https://www.berkshirehathaway.com/letters/2024ltr.pdf",
            kind="public_principle",
            published_year=2025,
            notes=(
                "Primary-source principle: emphasize operating earnings, cash generation, "
                "business quality, and a horizon measured in years or decades."
            ),
        ),
        applicable_markets=("global_equities", "korean_equities_research_candidate"),
        thesis=(
            "Prefer understandable businesses with durable earning power and cash generation, "
            "then evaluate them over a long horizon rather than trading short-term price noise."
        ),
        limitations=(
            "Business quality and intrinsic value are not specified as reproducible numeric rules",
            "Berkshire's scale, access, tax position, and concentration tolerance differ from individuals",
            "Accounting and governance comparability requires Korea-specific point-in-time data",
        ),
        executable=False,
    ),
    StrategyDefinition(
        strategy_id="howard_marks_risk_posture_principle",
        version="1.0.0",
        display_name="Howard Marks risk-posture principle",
        horizon="risk_management",
        source=ResearchSource(
            title="What Really Matters?",
            authors=("Howard Marks",),
            url="https://www.oaktreecapital.com/insights/memo/what-really-matters",
            kind="public_principle",
            published_year=2022,
            notes=(
                "Primary-author memo: align aggressiveness and defensiveness with financial "
                "capacity, needs, temperament, and the possibility of permanent loss."
            ),
        ),
        applicable_markets=("global_multi_asset", "korean_equities_risk_overlay"),
        thesis=(
            "Set a normal risk posture that fits the investor and vary aggressiveness only "
            "within explicit loss-bearing capacity."
        ),
        limitations=(
            "A risk philosophy is not a complete entry or exit algorithm",
            "Risk capacity and willingness require user-specific financial information",
            "Private-credit experience does not transfer directly to Korean retail equities",
        ),
        executable=False,
    ),
    StrategyDefinition(
        strategy_id="time_series_momentum",
        version="1.0.0",
        display_name="Time-series momentum",
        horizon="swing",
        source=ResearchSource(
            title="Time Series Momentum",
            authors=("Tobias J. Moskowitz", "Yao Hua Ooi", "Lasse H. Pedersen"),
            url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463",
            kind="working_paper",
            published_year=2012,
            notes="Test the trend signal separately from volatility scaling.",
        ),
        applicable_markets=("global_futures", "equity_indices"),
        thesis="Scale exposure to the direction and strength of an asset's own medium-term trend.",
        limitations=(
            "Whipsaw losses in range-bound markets",
            "Published evidence is not specific to Korean individual stocks",
            "Volatility scaling can explain part of the reported result",
        ),
        signal_engine="time_series_momentum",
    ),
    StrategyDefinition(
        strategy_id="long_term_absolute_momentum",
        version="1.0.0",
        display_name="Long-term absolute momentum",
        horizon="long_term",
        source=ResearchSource(
            title="Time Series Momentum",
            authors=("Tobias J. Moskowitz", "Yao Hua Ooi", "Lasse H. Pedersen"),
            url="https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf",
            kind="peer_reviewed",
            published_year=2012,
            notes="Long/cash adaptation of the published 12-month directional signal.",
        ),
        applicable_markets=("global_futures", "equity_indices"),
        thesis=(
            "Hold long exposure when the asset's own trailing 6-to-12-month return is "
            "positive and otherwise hold cash."
        ),
        limitations=(
            "The original study uses diversified futures and long-short positions",
            "A long/cash Korean equity adaptation can lag sharp rebounds",
            "Individual-stock evidence is weaker than diversified index evidence",
            "Long lookbacks can retain exposure through the early stage of a drawdown",
        ),
        signal_engine="time_series_momentum",
    ),
    StrategyDefinition(
        strategy_id="factor_momentum",
        version="1.0.0",
        display_name="Factor momentum",
        horizon="long_term",
        source=ResearchSource(
            title="Factor Momentum and the Momentum Factor",
            authors=("Sina Ehsani", "Juhani T. Linnainmaa"),
            url="https://www.nber.org/papers/w25551",
            kind="peer_reviewed",
            published_year=2022,
            notes="Factor autocorrelation can reverse abruptly and create momentum crashes.",
        ),
        applicable_markets=("us_equities", "korean_equities_research_candidate"),
        thesis="Tilt toward factors with positive trailing returns while controlling crash risk.",
        limitations=(
            "Factor definitions and investability vary by market",
            "Autocorrelation reversals can produce abrupt losses",
            "Requires point-in-time constituent and fundamental data",
        ),
    ),
    StrategyDefinition(
        strategy_id="intraday_opening_range_breakout",
        version="1.0.0",
        display_name="Intraday opening-range breakout",
        horizon="intraday",
        source=ResearchSource(
            title="A Profitable Day Trading Strategy For The U.S. Equity Market",
            authors=("Carlo Zarattini", "Andrea Barbon", "Andrew Aziz"),
            url="https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284",
            kind="working_paper",
            published_year=2024,
            notes=(
                "Research candidate only until Korea-specific minute data, costs, "
                "walk-forward tests, and paper evidence pass."
            ),
        ),
        applicable_markets=("us_equities", "korean_equities_research_candidate"),
        thesis=(
            "Enter on the next minute after a confirmed close above the opening range, "
            "use the opening low as a stop, and exit before the session ends."
        ),
        limitations=(
            "Published evidence is primarily from U.S. equities rather than Korea",
            "One-minute OHLC data cannot identify within-bar execution ordering",
            "Opening gaps, volatility interruptions, and price limits can worsen fills",
            "The edge is highly sensitive to spread, slippage, taxes, and stock selection",
        ),
        requires_intraday_data=True,
        signal_engine="opening_range_breakout",
    ),
    StrategyDefinition(
        strategy_id="volatility_managed_exposure",
        version="1.0.0",
        display_name="Volatility-managed exposure",
        horizon="risk_management",
        source=ResearchSource(
            title="Volatility-Managed Portfolios",
            authors=("Alan Moreira", "Tyler Muir"),
            url="https://doi.org/10.1111/jofi.12513",
            kind="peer_reviewed",
            published_year=2017,
            notes="Use as risk sizing, not as a return guarantee.",
        ),
        applicable_markets=("equity_indices", "factor_portfolios"),
        thesis="Reduce exposure when recent realized variance rises and restore it as risk normalizes.",
        limitations=(
            "Can reduce exposure immediately before a rebound",
            "Leverage is prohibited in the initial product scope",
            "Variance estimator and rebalance costs materially affect results",
        ),
        signal_engine="volatility_managed_exposure",
    ),
)

# Keep executable research candidates first for deterministic validation fixtures;
# public principles remain catalogued but cannot be promoted without testable rules.
PUBLIC_STRATEGY_CATALOG = (
    PUBLIC_STRATEGY_CATALOG[2:] + PUBLIC_STRATEGY_CATALOG[:2]
)
