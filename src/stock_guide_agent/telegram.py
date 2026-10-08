from __future__ import annotations

from dataclasses import dataclass
import os
from datetime import datetime, timezone
from typing import Any, Literal

from .http import HttpRequest, JsonTransport, urllib_json_transport
from .intent import InvestmentIntent
from .intent import parse_investment_intent
from .execution import ApprovalChallenge, ApprovalGate, AuthorizedOrder, OrderRejected
from .messaging import SQLiteMessageStore


class TelegramConfigurationError(RuntimeError):
    pass


class TelegramApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    user_id: int
    text: str


@dataclass(frozen=True)
class OwnerCommandResult:
    status: Literal["approved", "rejected", "kill_switch_on", "kill_switch_off"]
    proposal_id: str | None = None
    authorized_order: AuthorizedOrder | None = None


def parse_telegram_update(payload: dict[str, Any]) -> TelegramMessage | None:
    """Extract a private text message and ignore unsupported Telegram updates."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if chat.get("type") != "private":
        return None
    try:
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (payload["update_id"], chat["id"], sender["id"])):
            return None
        update_id = int(payload["update_id"])
        chat_id = int(chat["id"])
        user_id = int(sender["id"])
        if update_id < 0 or user_id <= 0 or chat_id != user_id or sender.get("is_bot") is True:
            return None
        return TelegramMessage(
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            text=text.strip(),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_send_message(chat_id: int, text: str) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        owner_user_id: int,
        *,
        allowed_user_ids: set[int] | frozenset[int] | None = None,
        owner_chat_id: int | None = None,
        transport: JsonTransport = urllib_json_transport,
        message_store: SQLiteMessageStore | None = None,
    ) -> None:
        if not token:
            raise TelegramConfigurationError("Telegram bot token is required")
        if owner_user_id <= 0:
            raise TelegramConfigurationError("A positive Telegram owner user ID is required")
        members = set(allowed_user_ids or ())
        members.add(owner_user_id)
        if any(user_id <= 0 for user_id in members):
            raise TelegramConfigurationError("All Telegram user IDs must be positive")
        if owner_chat_id is not None and owner_chat_id <= 0:
            raise TelegramConfigurationError("Telegram owner chat ID must be positive")
        if owner_chat_id is not None and owner_chat_id != owner_user_id:
            raise TelegramConfigurationError("Owner notifications must use the owner's private chat")
        self.token = token
        self.owner_user_id = owner_user_id
        self.allowed_user_ids = frozenset(members)
        self.owner_chat_id = owner_chat_id or owner_user_id
        self.transport = transport
        self.message_store = message_store
        self.base_url = f"https://api.telegram.org/bot{token}"

    @classmethod
    def from_env(
        cls, *, transport: JsonTransport = urllib_json_transport
    ) -> "TelegramBotClient":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        raw_owner_id = os.environ.get("TELEGRAM_OWNER_USER_ID", "")
        raw_member_ids = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
        raw_owner_chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
        try:
            owner_user_id = int(raw_owner_id)
            member_ids = {
                int(value.strip())
                for value in raw_member_ids.split(",")
                if value.strip()
            }
            owner_chat_id = int(raw_owner_chat_id) if raw_owner_chat_id else None
        except ValueError as exc:
            raise TelegramConfigurationError(
                "Telegram user IDs must be comma-separated integers"
            ) from exc
        return cls(
            token,
            owner_user_id,
            allowed_user_ids=member_ids,
            owner_chat_id=owner_chat_id,
            transport=transport,
        )

    def is_allowed_user(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_user_id

    def get_updates(self, *, offset: int | None = None, timeout_seconds: int = 30) -> list[dict[str, Any]]:
        if not 0 <= timeout_seconds <= 50:
            raise ValueError("timeout_seconds must be between 0 and 50")
        query = {
            "timeout": str(timeout_seconds),
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            query["offset"] = str(offset)
        response = self.transport(
            HttpRequest(
                method="GET",
                url=f"{self.base_url}/getUpdates",
                query=query,
                timeout_seconds=float(timeout_seconds + 5),
                sensitive_values=(self.token,),
            )
        )
        payload = response.json_body
        if response.status >= 400 or payload.get("ok") is not True:
            raise TelegramApiError("Telegram getUpdates failed")
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    def send_text(self, chat_id: int, text: str) -> None:
        if not self.is_allowed_user(chat_id):
            raise TelegramConfigurationError("Message recipient is not an allowed private user")
        if self.message_store is not None:
            self.message_store.enqueue_message(chat_id, text, now=datetime.now(timezone.utc))
            return
        self._send_text_now(chat_id, text)

    def _send_text_now(self, chat_id: int, text: str) -> None:
        response = self.transport(
            HttpRequest(
                method="POST",
                url=f"{self.base_url}/sendMessage",
                json_body=build_send_message(chat_id, text),
                sensitive_values=(self.token,),
            )
        )
        payload = response.json_body
        if response.status >= 400 or payload.get("ok") is not True:
            raise TelegramApiError("Telegram sendMessage failed")

    def flush_outbox(self, *, now: datetime, limit: int = 20) -> int:
        """Retry replies without replaying the user command that produced them."""
        if self.message_store is None:
            return 0
        failures = 0
        for message in self.message_store.due_messages(now=now, limit=limit):
            if not self.is_allowed_user(message.chat_id):
                # Membership may have changed while the reply was queued.
                self.message_store.mark_sent(message.id)
                continue
            try:
                self._send_text_now(message.chat_id, message.text)
            except Exception:
                failures += 1
                self.message_store.mark_send_failed(message.id, now=now)
            else:
                self.message_store.mark_sent(message.id)
        return failures

    def handle_intent_update(self, payload: dict[str, Any]) -> TelegramMessage | None:
        message = parse_telegram_update(payload)
        if message is None or not self.is_allowed_user(message.user_id):
            return None
        intent = parse_investment_intent(message.text)
        self.send_text(message.chat_id, format_intent_confirmation(intent))
        return message

    def send_owner_approval_request(self, challenge: ApprovalChallenge) -> None:
        if challenge.approver_user_id != self.owner_user_id:
            raise TelegramConfigurationError("approval challenge is assigned to another owner")
        proposal = challenge.proposal
        cost_text = (
            f"{proposal.estimated_transaction_cost:,.0f}원"
            if proposal.estimated_transaction_cost is not None
            else "미확인"
        )
        cost_status = "완전" if proposal.transaction_costs_complete else "불완전"
        text = (
            f"예상 거래비용: {cost_text} ({cost_status})\n"
            f"비용 가정: {proposal.transaction_cost_assumption}\n"
            "모의검증용 주문 승인 요청 (실제 주문 전송 없음)\n"
            f"• 요청 사용자: {proposal.requester_user_id}\n"
            f"• 증권사: {proposal.broker_provider}\n"
            f"• 종목/방향: {proposal.stock_code} / {proposal.side}\n"
            f"• 수량/지정가: {proposal.quantity}주 / {proposal.limit_price:g}원\n"
            f"• 주문금액: {proposal.notional:,.0f}원\n"
            f"• 가이드 비중/손실한도: {proposal.suggested_fraction * 100:g}% / "
            f"{proposal.loss_limit_pct:g}%\n"
            f"• 가격 무효화선: "
            f"{f'{proposal.invalidation_price:,.0f}원' if proposal.invalidation_price is not None else '없음'}\n"
            f"• 환경: {proposal.environment}\n"
            f"• 모의 승인: 승인 {proposal.proposal_id} {challenge.approval_code}\n"
            f"• 모의 거절: 거절 {proposal.proposal_id}"
        )
        self.send_text(self.owner_chat_id, text)

    def handle_owner_command(
        self,
        payload: dict[str, Any],
        gate: ApprovalGate,
        *,
        now: datetime,
    ) -> OwnerCommandResult | None:
        message = parse_telegram_update(payload)
        if message is None or not self.is_owner(message.user_id):
            return None
        parts = message.text.split()
        if len(parts) == 3 and parts[0] == "승인":
            order = gate.authorize(
                parts[1], parts[2], now=now, approver_user_id=message.user_id
            )
            self.send_text(message.chat_id, f"승인 완료: {parts[1]}")
            return OwnerCommandResult("approved", parts[1], order)
        if len(parts) == 2 and parts[0] == "거절":
            gate.reject(parts[1], now=now, approver_user_id=message.user_id)
            self.send_text(message.chat_id, f"거절 완료: {parts[1]}")
            return OwnerCommandResult("rejected", parts[1])
        if message.text.startswith("거래중단"):
            reason = message.text.removeprefix("거래중단").strip() or "owner request"
            gate.activate_kill_switch(actor_user_id=message.user_id, reason=reason)
            self.send_text(message.chat_id, "전체 거래를 중단했습니다.")
            return OwnerCommandResult("kill_switch_on")
        if message.text == "RESET KILL SWITCH":
            gate.reset_kill_switch(
                actor_user_id=message.user_id, confirmation=message.text
            )
            self.send_text(message.chat_id, "킬스위치를 해제했습니다.")
            return OwnerCommandResult("kill_switch_off")
        raise OrderRejected("unsupported owner command")


_STRATEGY_NAMES = {
    "long_term_stable": "\uc7a5\uae30\uc548\uc815",
    "long_term": "\uc7a5\uae30",
    "day_trading": "\ub2e8\ud0c0",
    "swing": "\uc2a4\uc719",
    "cash": "\ud604\uae08",
}

_PERIOD_NAMES = {
    "daily": "\uc77c\uc77c",
    "weekly": "\uc8fc\uac04",
    "monthly": "\uc6d4\uac04",
    "annual": "\uc5f0\uac04",
    "unspecified": "\uae30\uac04 \ubbf8\uc9c0\uc815",
}


def format_intent_confirmation(intent: InvestmentIntent) -> str:
    """Render a compact confirmation instead of exposing internal JSON."""
    lines = ["\uc774\ub807\uac8c \uc774\ud574\ud588\uc5b4\uc694."]
    if intent.allocations:
        allocation = " / ".join(
            f"{_STRATEGY_NAMES.get(item.strategy, item.strategy)} {item.weight_pct:g}%"
            for item in intent.allocations
        )
        lines.append(f"\u2022 \ubc30\ubd84: {allocation}")
    if intent.target_return_pct is not None:
        period = _PERIOD_NAMES[intent.target_return_period]
        lines.append(f"\u2022 \ud76c\ub9dd \ubaa9\ud45c: {period} {intent.target_return_pct:g}%")
    if intent.max_trade_loss_pct is not None:
        lines.append(f"\u2022 \uac70\ub798\ub2f9 \uc190\uc2e4\ud55c\ub3c4: {intent.max_trade_loss_pct:g}%")
    if intent.max_daily_loss_pct is not None:
        lines.append(f"\u2022 \uc77c\uc77c \uc911\ub2e8\uc120: {intent.max_daily_loss_pct:g}%")
    if intent.max_trades_per_day is not None:
        lines.append(f"• 거래빈도: 하루 최대 {intent.max_trades_per_day}회")
    elif intent.trading_frequency != "unspecified":
        names = {"low": "낮음", "medium": "보통", "high": "높음"}
        lines.append(f"• 거래빈도: {names[intent.trading_frequency]}")
    lines.append("• 이용 방식: 이 봇은 투자 가이드만 제공합니다.")
    lines.append("• 실제 매매: 가이드를 참고해 증권사 앱에서 직접 진행하세요.")
    lines.append("• 보유정보: 실제 매매 후 종목·수량·평단을 갱신해 주세요.")
    if intent.valid_for_days is not None:
        lines.append(f"• 설정 유효기간: 저장 시점부터 {intent.valid_for_days}일")
    for explanation in intent.risk_explanations[:2]:
        lines.append(f"• 위험 설명: {explanation}")
    for warning in intent.warnings[:3]:
        lines.append(f"\u26a0 {warning}")
    for guardrail in intent.suggested_guardrails[:8]:
        lines.append(f"• 안전 대안: {guardrail}")
    if intent.missing_fields:
        field_names = {
            "target_return": "목표수익률",
            "strategy_allocation": "전략별 배분",
            "max_daily_loss": "하루 손실 중단선",
            "remaining_allocation": "남은 배분",
        }
        lines.append(
            "\u2022 \ucd94\uac00 \ud655\uc778: "
            + ", ".join(field_names.get(value, value) for value in intent.missing_fields)
        )
    lines.append("설정했어요. 추가하거나 바꿀 조건만 이어서 말해도 됩니다.")
    return "\n".join(lines)
