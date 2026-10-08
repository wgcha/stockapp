from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from .money import parse_korean_money


RequestedSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class ParsedOrderRequest:
    stock_code: str
    quantity: int
    limit_price: float
    requested_side: RequestedSide | None = None


_STOCK = r"[A-Za-z0-9.]{1,20}"
_QUANTITY = r"[\d,]+"
_PRICE = r"[\d,.]+(?:\s*만)?"
_VERB = r"사줘|사\s*줘|매수(?:해\s*줘)?|팔아줘|팔아\s*줘|매도(?:해\s*줘)?|주문(?:해\s*줘)?"
_PATTERNS = (
    re.compile(
        rf"^주문\s+(?P<stock>{_STOCK})\s+(?P<quantity>{_QUANTITY})\s*주\s+"
        rf"(?P<price>{_PRICE})\s*원?$"
    ),
    re.compile(
        rf"^(?P<stock>{_STOCK})\s+(?P<quantity>{_QUANTITY})\s*주(?:를)?\s+"
        rf"(?P<price>{_PRICE})\s*원?(?:에)?\s*(?P<verb>{_VERB})(?:요)?$"
    ),
    re.compile(
        rf"^(?P<stock>{_STOCK})\s+(?P<price>{_PRICE})\s*원?에\s+"
        rf"(?P<quantity>{_QUANTITY})\s*주(?:를)?\s*(?P<verb>{_VERB})(?:요)?$"
    ),
)


def parse_order_request(text: str) -> ParsedOrderRequest | None:
    normalized = re.sub(r"\s+", " ", text.strip())
    match = next(
        (
            candidate
            for pattern in _PATTERNS
            if (candidate := pattern.fullmatch(normalized)) is not None
        ),
        None,
    )
    if match is None:
        return None
    quantity = int(match.group("quantity").replace(",", ""))
    limit_price = parse_korean_money(match.group("price"))
    if quantity <= 0 or limit_price <= 0:
        return None
    verb = match.groupdict().get("verb") or ""
    requested_side: RequestedSide | None
    if verb.startswith(("사", "매수")):
        requested_side = "buy"
    elif verb.startswith(("팔", "매도")):
        requested_side = "sell"
    else:
        requested_side = None
    return ParsedOrderRequest(
        match.group("stock").upper(),
        quantity,
        limit_price,
        requested_side,
    )
