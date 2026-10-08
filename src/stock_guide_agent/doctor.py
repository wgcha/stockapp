from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Literal
from .runtime_settings import validate_runtime_settings
from .trading_calendar import KrxTradingCalendar, TradingCalendarUnavailable


CheckStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True)
class ReadinessCheck:
    check_id: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    checks: tuple[ReadinessCheck, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def run_preflight(environment: Mapping[str, str]) -> ReadinessReport:
    checks: list[ReadinessCheck] = []
    try:
        normalized = validate_runtime_settings(environment)
    except ValueError as exc:
        checks.append(ReadinessCheck("runtime_settings", "error", str(exc)))
    else:
        environment = {**environment, **normalized}
        checks.append(ReadinessCheck("runtime_settings", "ok", "Runtime settings validated"))
    try:
        KrxTradingCalendar(overrides_path=environment.get("KRX_CALENDAR_OVERRIDES"))
    except TradingCalendarUnavailable:
        checks.append(ReadinessCheck("trading_calendar", "error", "KRX calendar or overrides unavailable"))
    else:
        checks.append(ReadinessCheck("trading_calendar", "ok", "KRX calendar loaded; verify new exchange notices before deployment"))

    def required(name: str, check_id: str) -> None:
        checks.append(
            ReadinessCheck(
                check_id,
                "ok" if environment.get(name, "").strip() else "error",
                f"{name} configured"
                if environment.get(name, "").strip()
                else f"{name} is required",
            )
        )

    required("TELEGRAM_BOT_TOKEN", "telegram_token")
    required("TELEGRAM_OWNER_USER_ID", "telegram_owner")
    required("TOSSINVEST_CLIENT_ID", "toss_client_id")
    required("TOSSINVEST_CLIENT_SECRET", "toss_client_secret")

    raw_members = environment.get("TELEGRAM_ALLOWED_USER_IDS", "")
    try:
        members = {
            int(value.strip()) for value in raw_members.split(",") if value.strip()
        }
        owner = int(environment.get("TELEGRAM_OWNER_USER_ID", "0"))
        owner_chat = int(environment.get("TELEGRAM_OWNER_CHAT_ID", "") or owner)
        if owner <= 0 or any(member <= 0 for member in members) or owner_chat != owner:
            raise ValueError
    except ValueError:
        checks.append(
            ReadinessCheck(
                "telegram_user_ids", "error",
                "Telegram user IDs must be positive integers and owner chat must match owner user"
            )
        )
    else:
        if not members:
            checks.append(
                ReadinessCheck(
                    "telegram_members",
                    "warning",
                    "No member IDs configured; only the owner can use the bot",
                )
            )
        elif owner in members:
            checks.append(
                ReadinessCheck(
                    "telegram_members", "ok", f"{len(members)} member IDs configured"
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "telegram_members",
                    "ok",
                    f"{len(members)} member IDs configured; owner is added automatically",
                )
            )

    equity_text = environment.get("AGENT_ACCOUNT_EQUITY_KRW", "").strip()
    equity_ready = False
    if equity_text:
        try:
            equity_ready = float(equity_text) > 0
        except ValueError:
            equity_ready = False
        checks.append(
            ReadinessCheck(
                "account_equity",
                "ok" if equity_ready else "error",
                "Positive account equity configured"
                if equity_ready
                else "AGENT_ACCOUNT_EQUITY_KRW must be a positive number",
            )
        )

    execution_mode = environment.get("AGENT_EXECUTION_ENV", "mock").lower()
    if execution_mode not in {"mock", "live"}:
        checks.append(
            ReadinessCheck(
                "execution_mode", "error", "AGENT_EXECUTION_ENV must be mock"
            )
        )
    elif execution_mode == "mock":
        checks.append(
            ReadinessCheck(
                "execution_mode", "ok", "Mock execution: no orders leave the process"
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                "live_trading_gate",
                "error",
                "Live trading is not supported; use AGENT_EXECUTION_ENV=mock for guide-only operation",
            )
        )

    optional_pairs = (
        ("NAVER_API_HUB_CLIENT_ID", "NAVER_API_HUB_CLIENT_SECRET", "news"),
    )
    for first, second, check_id in optional_pairs:
        first_set = bool(environment.get(first, "").strip())
        second_set = bool(environment.get(second, "").strip())
        if first_set != second_set:
            checks.append(
                ReadinessCheck(
                    check_id,
                    "error",
                    f"{first} and {second} must be configured together",
                )
            )
        elif first_set:
            checks.append(ReadinessCheck(check_id, "ok", f"{check_id} enabled"))
        else:
            checks.append(
                ReadinessCheck(
                    check_id,
                    "warning",
                    f"{check_id} disabled; core guide remains available",
                )
            )

    if bool(environment.get("ECOS_API_KEY", "")) != bool(
        environment.get("ECOS_QUERIES", "")
    ):
        checks.append(
            ReadinessCheck(
                "ecos",
                "error",
                "ECOS_API_KEY and ECOS_QUERIES must be configured together",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                "ecos",
                "ok" if environment.get("ECOS_API_KEY") else "warning",
                "ECOS enabled" if environment.get("ECOS_API_KEY") else "ECOS disabled",
            )
        )

    try:
        paper_symbol_limit = int(
            environment.get("PAPER_EVALUATION_MAX_SYMBOLS", "20")
        )
        if not 1 <= paper_symbol_limit <= 200:
            raise ValueError
    except ValueError:
        checks.append(
            ReadinessCheck(
                "paper_symbol_limit",
                "error",
                "PAPER_EVALUATION_MAX_SYMBOLS must be an integer from 1 to 200",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                "paper_symbol_limit",
                "ok",
                f"Paper daily evaluation limit: {paper_symbol_limit} symbols",
            )
        )

    for name, check_id, minimum, maximum, default in (
        ("INTRADAY_MINIMUM_BARS", "intraday_minimum_bars", 60, 500, "300"),
        (
            "INTRADAY_ARCHIVE_MAX_SYMBOLS",
            "intraday_archive_symbols",
            0,
            20,
            "5",
        ),
        ("DAILY_GUIDE_HOUR_KST", "daily_guide_hour", 0, 23, "9"),
        ("DAILY_GUIDE_MINUTE_KST", "daily_guide_minute", 0, 59, "10"),
    ):
        try:
            value = int(environment.get(name, default))
            if not minimum <= value <= maximum:
                raise ValueError
        except ValueError:
            checks.append(
                ReadinessCheck(
                    check_id,
                    "error",
                    f"{name} must be an integer from {minimum} to {maximum}",
                )
            )
        else:
            checks.append(
                ReadinessCheck(check_id, "ok", f"{name} configured: {value}")
            )

    try:
        paper_initial_cash = float(
            environment.get("PAPER_INITIAL_CAPITAL_KRW", "10000000")
        )
        if paper_initial_cash <= 0:
            raise ValueError
    except ValueError:
        checks.append(
            ReadinessCheck(
                "paper_initial_cash",
                "error",
                "PAPER_INITIAL_CAPITAL_KRW must be a positive number",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                "paper_initial_cash",
                "ok",
                f"Paper initial capital configured: {paper_initial_cash:g} KRW",
            )
        )

    raw_mirae_commission = environment.get("MIRAE_PAPER_COMMISSION_RATE", "").strip()
    if not raw_mirae_commission:
        checks.append(
            ReadinessCheck(
                "mirae_paper_cost",
                "warning",
                "Mirae paper valuations cannot qualify until the contracted commission rate is configured",
            )
        )
    else:
        try:
            mirae_commission = float(raw_mirae_commission)
            if not 0 <= mirae_commission < 0.1:
                raise ValueError
        except ValueError:
            checks.append(
                ReadinessCheck(
                    "mirae_paper_cost",
                    "error",
                    "MIRAE_PAPER_COMMISSION_RATE must be a decimal rate in [0, 0.1)",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "mirae_paper_cost",
                    "ok",
                    "Mirae paper commission rate configured",
                )
            )

    raw_crossref_enabled = environment.get(
        "CROSSREF_DISCOVERY_ENABLED", "true"
    ).lower()
    try:
        if raw_crossref_enabled not in {"true", "false"}:
            raise ValueError
        crossref_rows = int(environment.get("CROSSREF_DISCOVERY_ROWS", "20"))
        if not 1 <= crossref_rows <= 100:
            raise ValueError
    except ValueError:
        checks.append(
            ReadinessCheck(
                "crossref_discovery",
                "error",
                "Crossref enabled must be true/false and rows must be 1 to 100",
            )
        )
    else:
        if raw_crossref_enabled == "false":
            checks.append(
                ReadinessCheck(
                    "crossref_discovery", "warning", "Academic discovery disabled"
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "crossref_discovery",
                    "ok"
                    if environment.get("CROSSREF_MAILTO", "").strip()
                    else "warning",
                    "Crossref discovery enabled with polite-pool email"
                    if environment.get("CROSSREF_MAILTO", "").strip()
                    else "Crossref discovery enabled; CROSSREF_MAILTO is recommended",
                )
            )
    return ReadinessReport(
        ready=not any(item.status == "error" for item in checks),
        checks=tuple(checks),
    )
