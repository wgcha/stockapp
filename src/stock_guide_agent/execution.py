from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import hmac
import math
import secrets
from threading import RLock
from typing import Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .guidance import TradeGuide


Environment = Literal["mock", "live"]
OrderSide = Literal["buy", "sell"]
KOREA_TIMEZONE = ZoneInfo("Asia/Seoul")


class OrderRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionPolicy:
    environment: Environment = "mock"
    live_trading_enabled: bool = False
    max_order_notional: float = 1_000_000.0
    max_orders_per_day: int = 5
    guide_max_age_seconds: int = 120
    approval_ttl_seconds: int = 180


@dataclass
class ExecutionState:
    daily_pnl_pct: float = 0.0
    max_daily_loss_pct: float = 1.5
    orders_today: int = 0
    orders_trading_date: date | None = None
    api_healthy: bool = True
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    account_equity: float | None = None
    _kill_switch_persist: Callable[[bool, str | None], None] | None = field(
        default=None, repr=False, compare=False
    )
    _kill_switch_lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.account_equity is not None and self.account_equity <= 0:
            raise ValueError("account equity must be positive")

    def activate_kill_switch(self, reason: str) -> None:
        with self._kill_switch_lock:
            self.kill_switch_active = True
            self.kill_switch_reason = reason or "manual"
            self._persist_kill_switch()

    def reset_kill_switch(self, confirmation: str) -> None:
        if confirmation != "RESET KILL SWITCH":
            raise OrderRejected("explicit kill-switch reset confirmation required")
        with self._kill_switch_lock:
            if self._kill_switch_persist is not None:
                try:
                    self._kill_switch_persist(False, None)
                except Exception:
                    raise RuntimeError("kill switch could not be reset safely") from None
            self.kill_switch_active = False
            self.kill_switch_reason = None

    def _persist_kill_switch(self) -> None:
        if self._kill_switch_persist is None:
            return
        try:
            self._kill_switch_persist(
                self.kill_switch_active, self.kill_switch_reason
            )
        except Exception:
            raise RuntimeError("kill switch could not be persisted safely") from None

    def roll_trading_day(self, now: datetime) -> None:
        _require_aware(now)
        trading_date = now.astimezone(KOREA_TIMEZONE).date()
        if self.orders_trading_date != trading_date:
            self.orders_today = 0
            self.orders_trading_date = trading_date


@dataclass(frozen=True)
class OrderProposal:
    proposal_id: str
    stock_code: str
    side: OrderSide
    quantity: int
    limit_price: float
    notional: float
    strategy_id: str
    guide_action: str
    created_at: datetime
    expires_at: datetime
    environment: Environment
    requester_user_id: int
    broker_provider: str
    max_daily_loss_pct: float = 1.5
    strategy_version: str = "1.0.0"
    suggested_fraction: float = 0.0
    loss_limit_pct: float = 0.5
    invalidation_price: float | None = None
    owned_quantity_at_proposal: int | None = None
    account_equity_at_proposal: float | None = None
    estimated_transaction_cost: float | None = None
    transaction_costs_complete: bool = False
    transaction_cost_assumption: str = ""


@dataclass(frozen=True)
class ApprovalChallenge:
    proposal: OrderProposal
    approval_code: str
    approver_user_id: int


@dataclass(frozen=True)
class AuthorizedOrder:
    proposal: OrderProposal
    authorized_at: datetime


@dataclass
class _ApprovalRecord:
    proposal: OrderProposal
    code_digest: str
    used: bool = False


class ApprovalGate:
    def __init__(
        self,
        policy: ExecutionPolicy,
        state: ExecutionState,
        *,
        owner_user_id: int,
        account_equity_source: Callable[[int], float | None] | None = None,
    ) -> None:
        if policy.environment == "live" and not policy.live_trading_enabled:
            raise OrderRejected("live environment requires an explicit enable flag")
        if owner_user_id <= 0:
            raise ValueError("owner_user_id must be positive")
        self.policy = policy
        self.state = state
        self.owner_user_id = owner_user_id
        self.account_equity_source = account_equity_source
        self._pending: dict[str, _ApprovalRecord] = {}

    def set_account_equity_source(
        self, source: Callable[[int], float | None]
    ) -> None:
        self.account_equity_source = source

    def propose(
        self,
        guide: TradeGuide,
        *,
        quantity: int,
        limit_price: float,
        now: datetime,
        requester_user_id: int,
        broker_provider: str = "toss_invest",
        approval_code: str | None = None,
        owned_quantity: int | None = None,
        estimated_transaction_cost: float | None = None,
        transaction_costs_complete: bool = False,
        transaction_cost_assumption: str = "",
    ) -> ApprovalChallenge:
        _require_aware(now)
        if requester_user_id <= 0:
            raise OrderRejected("requester user ID must be positive")
        if broker_provider not in {"toss_invest", "mirae_asset"}:
            raise OrderRejected("unsupported broker provider")
        self.state.roll_trading_day(now)
        self._check_runtime_state()
        if self.state.daily_pnl_pct <= -guide.max_daily_loss_pct:
            raise OrderRejected("user daily loss limit reached")
        age = (now - guide.generated_at).total_seconds()
        if age < 0 or age > self.policy.guide_max_age_seconds:
            raise OrderRejected("trade guide is stale")
        if guide.action not in {"buy", "partial_sell", "full_sell"}:
            raise OrderRejected("guide does not authorize an order action")
        if not guide.requires_confirmation:
            raise OrderRejected("guide must require user confirmation")
        if quantity <= 0 or limit_price <= 0:
            raise OrderRejected("quantity and limit price must be positive")

        side: OrderSide = "buy" if guide.action == "buy" else "sell"
        account_equity = self._account_equity(requester_user_id)
        notional = quantity * limit_price
        if estimated_transaction_cost is not None and estimated_transaction_cost < 0:
            raise OrderRejected("estimated transaction cost cannot be negative")
        if notional > self.policy.max_order_notional:
            raise OrderRejected("order exceeds maximum notional")
        if self.state.orders_today >= self.policy.max_orders_per_day:
            raise OrderRejected("daily order count limit reached")
        self._check_position_risk(
            guide,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            owned_quantity=owned_quantity,
            account_equity=account_equity,
        )
        if self.policy.environment == "live" and not transaction_costs_complete:
            raise OrderRejected("live order requires a complete transaction cost estimate")

        proposal = OrderProposal(
            proposal_id=uuid4().hex,
            stock_code=guide.stock_code,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            notional=notional,
            strategy_id=guide.strategy_id,
            guide_action=guide.action,
            created_at=now,
            expires_at=now + timedelta(seconds=self.policy.approval_ttl_seconds),
            environment=self.policy.environment,
            requester_user_id=requester_user_id,
            broker_provider=broker_provider,
            max_daily_loss_pct=guide.max_daily_loss_pct,
            strategy_version=guide.strategy_version,
            suggested_fraction=guide.suggested_fraction,
            loss_limit_pct=guide.loss_limit_pct,
            invalidation_price=guide.invalidation_price,
            owned_quantity_at_proposal=owned_quantity,
            account_equity_at_proposal=account_equity,
            estimated_transaction_cost=estimated_transaction_cost,
            transaction_costs_complete=transaction_costs_complete,
            transaction_cost_assumption=transaction_cost_assumption,
        )
        code = approval_code or f"{secrets.randbelow(1_000_000):06d}"
        if len(code) < 4:
            raise ValueError("approval code must have at least four characters")
        self._pending[proposal.proposal_id] = _ApprovalRecord(
            proposal=proposal,
            code_digest=_digest(code),
        )
        return ApprovalChallenge(proposal, code, self.owner_user_id)

    def authorize(
        self,
        proposal_id: str,
        approval_code: str,
        *,
        now: datetime,
        approver_user_id: int,
    ) -> AuthorizedOrder:
        _require_aware(now)
        if approver_user_id != self.owner_user_id:
            raise OrderRejected("only the configured owner can approve orders")
        self.state.roll_trading_day(now)
        self._check_runtime_state()
        record = self._pending.get(proposal_id)
        if record is None:
            raise OrderRejected("unknown order proposal")
        if record.used:
            raise OrderRejected("approval code has already been used")
        if now > record.proposal.expires_at:
            raise OrderRejected("approval code expired")
        if self.state.daily_pnl_pct <= -record.proposal.max_daily_loss_pct:
            raise OrderRejected("user daily loss limit reached after proposal")
        current_equity = self._account_equity(
            record.proposal.requester_user_id,
            fallback=record.proposal.account_equity_at_proposal,
        )
        self._recheck_buy_risk(record.proposal, current_equity)
        if not hmac.compare_digest(record.code_digest, _digest(approval_code)):
            raise OrderRejected("approval code mismatch")
        record.used = True
        self.state.orders_today += 1
        return AuthorizedOrder(record.proposal, now)

    def reject(
        self, proposal_id: str, *, now: datetime, approver_user_id: int
    ) -> OrderProposal:
        _require_aware(now)
        if approver_user_id != self.owner_user_id:
            raise OrderRejected("only the configured owner can reject orders")
        record = self._pending.get(proposal_id)
        if record is None:
            raise OrderRejected("unknown order proposal")
        if record.used:
            raise OrderRejected("order proposal has already been decided")
        if now > record.proposal.expires_at:
            raise OrderRejected("approval code expired")
        record.used = True
        return record.proposal

    def activate_kill_switch(self, *, actor_user_id: int, reason: str) -> None:
        if actor_user_id != self.owner_user_id:
            raise OrderRejected("only the configured owner can activate the kill switch")
        self.state.activate_kill_switch(reason)

    def reset_kill_switch(self, *, actor_user_id: int, confirmation: str) -> None:
        if actor_user_id != self.owner_user_id:
            raise OrderRejected("only the configured owner can reset the kill switch")
        self.state.reset_kill_switch(confirmation)

    def _check_runtime_state(self) -> None:
        if self.state.kill_switch_active:
            raise OrderRejected("kill switch is active")
        if not self.state.api_healthy:
            raise OrderRejected("broker API is unhealthy")
        if self.state.daily_pnl_pct <= -self.state.max_daily_loss_pct:
            raise OrderRejected("daily loss limit reached")

    def _check_position_risk(
        self,
        guide: TradeGuide,
        *,
        side: OrderSide,
        quantity: int,
        limit_price: float,
        owned_quantity: int | None,
        account_equity: float | None,
    ) -> None:
        if side == "sell":
            if owned_quantity is None or owned_quantity <= 0:
                raise OrderRejected("sell order requires a recorded holding")
            if quantity > owned_quantity:
                raise OrderRejected("sell quantity exceeds recorded holding")
            if guide.action == "partial_sell":
                maximum = max(1, math.ceil(owned_quantity * guide.suggested_fraction))
                if quantity > maximum:
                    raise OrderRejected("sell quantity exceeds guided partial reduction")
            return

        equity = account_equity
        if self.policy.environment == "live" and equity is None:
            raise OrderRejected("live buy requires configured account equity")
        if self.policy.environment == "live" and guide.invalidation_price is None:
            raise OrderRejected("live buy requires a numeric invalidation price")
        if equity is None:
            return
        if guide.suggested_fraction <= 0:
            raise OrderRejected("buy guide has no positive allocation")
        if quantity * limit_price > equity * guide.suggested_fraction:
            raise OrderRejected("buy order exceeds guided allocation")
        if guide.invalidation_price is not None:
            if limit_price <= guide.invalidation_price:
                raise OrderRejected("buy limit must be above invalidation price")
            loss_budget = equity * guide.loss_limit_pct / 100.0
            estimated_loss = quantity * (limit_price - guide.invalidation_price)
            if estimated_loss > loss_budget:
                raise OrderRejected("buy order exceeds per-trade loss budget")

    def _recheck_buy_risk(
        self, proposal: OrderProposal, account_equity: float | None
    ) -> None:
        if proposal.side != "buy" or account_equity is None:
            return
        equity = account_equity
        if proposal.notional > equity * proposal.suggested_fraction:
            raise OrderRejected("account equity fell below guided allocation")
        if proposal.invalidation_price is not None:
            loss_budget = equity * proposal.loss_limit_pct / 100.0
            estimated_loss = proposal.quantity * (
                proposal.limit_price - proposal.invalidation_price
            )
            if estimated_loss > loss_budget:
                raise OrderRejected("account equity fell below loss budget")

    def _account_equity(
        self, user_id: int, *, fallback: float | None = None
    ) -> float | None:
        value = (
            self.account_equity_source(user_id)
            if self.account_equity_source is not None
            else None
        )
        if value is None:
            value = self.state.account_equity
        if value is None:
            value = fallback
        if value is not None and value <= 0:
            raise OrderRejected("account equity must remain positive")
        return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution time must be timezone-aware")
