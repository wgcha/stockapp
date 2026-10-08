from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ParsedAccountEquity:
    account_equity: float


_AMOUNT = r"[\d,.]+(?:\s*억(?:\s*[\d,.]+\s*만?)?|\s*만)?"
_ACCOUNT_EQUITY_PATTERN = re.compile(
    rf"^(?:내\s*)?(?:투자금|투자자금|계좌\s*평가금|계좌자산|총자산)"
    rf"(?:은|는|이|가)?\s*(?P<amount>{_AMOUNT})\s*원?"
    rf"(?:이야|이에요|입니다|으로\s*해줘|로\s*해줘)?$"
)


def parse_korean_money(value: str) -> float:
    compact = value.replace(",", "").replace(" ", "").removesuffix("원")
    total = 0.0
    if "억" in compact:
        major, compact = compact.split("억", 1)
        total += float(major) * 100_000_000
    if "만" in compact:
        major, remainder = compact.split("만", 1)
        total += float(major) * 10_000
        compact = remainder
    if compact:
        total += float(compact)
    return total


def parse_account_equity_message(text: str) -> ParsedAccountEquity | None:
    normalized = re.sub(r"\s+", " ", text.strip())
    match = _ACCOUNT_EQUITY_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    try:
        value = parse_korean_money(match.group("amount"))
    except ValueError:
        return None
    if value <= 0:
        return None
    return ParsedAccountEquity(value)
