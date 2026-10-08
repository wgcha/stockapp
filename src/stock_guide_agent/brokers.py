from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from .execution import AuthorizedOrder, OrderRejected
from .toss import TossInvestClient, TossInvestToken


BrokerProvider = Literal["toss_invest", "mirae_asset"]


@dataclass(frozen=True)
class BrokerOrderReceipt:
    provider: BrokerProvider
    simulated: bool
    proposal_id: str
    payload: dict[str, Any]


class BrokerGateway(Protocol):
    provider: BrokerProvider

    def submit_authorized_order(self, order: AuthorizedOrder) -> BrokerOrderReceipt: ...


class TossBrokerGateway:
    provider: BrokerProvider = "toss_invest"

    def __init__(
        self,
        client: TossInvestClient,
        token: TossInvestToken | Callable[[], TossInvestToken],
    ) -> None:
        self.client = client
        self._token = token

    def _get_token(self) -> TossInvestToken:
        return self._token() if callable(self._token) else self._token

    def submit_authorized_order(self, order: AuthorizedOrder) -> BrokerOrderReceipt:
        receipt = self.client.submit_authorized_order(order, self._get_token())
        return BrokerOrderReceipt(
            provider=self.provider,
            simulated=receipt.simulated,
            proposal_id=receipt.proposal_id,
            payload=receipt.payload,
        )


class MiraeAssetLiveAdapter(Protocol):
    """Contract for an approved Mirae Asset AnyLink, FIX/API, or DMA integration."""

    def submit_authorized_order(self, order: AuthorizedOrder) -> dict[str, Any]: ...


class MiraeAssetBrokerGateway:
    provider: BrokerProvider = "mirae_asset"

    def __init__(self, *, live_adapter: MiraeAssetLiveAdapter | None = None) -> None:
        self.live_adapter = live_adapter

    def submit_authorized_order(self, order: AuthorizedOrder) -> BrokerOrderReceipt:
        proposal = order.proposal
        if proposal.environment == "mock":
            return BrokerOrderReceipt(
                provider=self.provider,
                simulated=True,
                proposal_id=proposal.proposal_id,
                payload={
                    "status": "SIMULATED",
                    "symbol": proposal.stock_code,
                    "side": proposal.side.upper(),
                    "quantity": proposal.quantity,
                    "price": proposal.limit_price,
                    "integration": "mirae_asset_fix_api_pending",
                },
            )
        raise OrderRejected(
            "Mirae Asset live orders are disabled; the program provides guidance only"
        )


class BrokerRouter:
    def __init__(self, gateways: list[BrokerGateway]) -> None:
        self._gateways = {gateway.provider: gateway for gateway in gateways}
        if len(self._gateways) != len(gateways):
            raise ValueError("broker providers must be unique")

    def available_providers(self) -> frozenset[str]:
        return frozenset(self._gateways)

    def submit_authorized_order(self, order: AuthorizedOrder) -> BrokerOrderReceipt:
        provider = order.proposal.broker_provider
        gateway = self._gateways.get(provider)
        if gateway is None:
            raise OrderRejected(f"broker provider is not configured: {provider}")
        return gateway.submit_authorized_order(order)
