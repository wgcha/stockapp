from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ParsedHolding:
    stock_code: str
    quantity: int
    average_price: float


_AMOUNT = r"[\d,]+(?:\.\d+)?(?:\s*만(?:\s*[\d,]+)?)?"
_HOLDING_PATTERNS = (
    re.compile(
        rf"^(?:보유|보유종목)\s+"
        rf"(?P<stock>[A-Za-z0-9.]{{1,20}})\s+"
        rf"(?P<quantity>[\d,]+)\s*주\s+"
        rf"(?:평단|평균단가|매입가)\s*(?P<average>{_AMOUNT})\s*원?$"
    ),
    re.compile(
        rf"^(?P<stock>[A-Za-z0-9.]{{1,20}})\s+"
        rf"(?P<quantity>[\d,]+)\s*주(?:를)?\s+"
        rf"(?P<average>{_AMOUNT})\s*원?에\s*"
        rf"(?:보유(?:\s*중)?|가지고\s*있어|갖고\s*있어|들고\s*있어|"
        rf"샀어|매수했어)(?:요)?$"
    ),
    re.compile(
        rf"^(?P<stock>[A-Za-z0-9.]{{1,20}})\s+"
        rf"(?P<quantity>[\d,]+)\s*주\s+"
        rf"(?:평단|평균단가|매입가)\s*(?P<average>{_AMOUNT})\s*원?"
        rf"(?:\s*보유(?:\s*중)?(?:이야|입니다)?)?$"
    ),
)


def _parse_korean_amount(value: str) -> float:
    compact = value.replace(",", "").replace(" ", "")
    if "만" not in compact:
        return float(compact)
    major, remainder = compact.split("만", 1)
    return float(major) * 10_000 + (float(remainder) if remainder else 0.0)


def parse_holding_message(text: str) -> ParsedHolding | None:
    """Parse a compact holding update such as '보유 005930 10주 평단 70000'."""
    normalized = re.sub(r"\s+", " ", text.strip())
    match = next(
        (
            candidate
            for pattern in _HOLDING_PATTERNS
            if (candidate := pattern.fullmatch(normalized)) is not None
        ),
        None,
    )
    if match is None:
        return None
    quantity = int(match.group("quantity").replace(",", ""))
    average_price = _parse_korean_amount(match.group("average"))
    if quantity < 0 or average_price < 0:
        return None
    return ParsedHolding(match.group("stock").upper(), quantity, average_price)
