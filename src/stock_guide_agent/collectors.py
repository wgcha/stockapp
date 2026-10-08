from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os

from .http import HttpRequest, JsonTransport, urllib_json_transport


class ConfigurationError(RuntimeError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, provider: str, code: str, message: str) -> None:
        super().__init__(f"{provider} error {code}: {message}")
        self.provider = provider
        self.code = code
        self.provider_message = message


@dataclass(frozen=True)
class DisclosureEvent:
    receipt_number: str
    corporation_name: str
    stock_code: str | None
    report_name: str
    filer_name: str
    receipt_date: date
    corrected: bool
    detail_url: str


class OpenDartClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: JsonTransport = urllib_json_transport,
    ) -> None:
        if not api_key:
            raise ConfigurationError("OpenDART API key is required")
        self.api_key = api_key
        self.transport = transport
        self.base_url = "https://opendart.fss.or.kr/api"

    @classmethod
    def from_env(cls, *, transport: JsonTransport = urllib_json_transport) -> "OpenDartClient":
        return cls(os.environ.get("OPENDART_API_KEY", ""), transport=transport)

    def search_disclosures(
        self,
        begin: date,
        end: date,
        *,
        corporation_code: str | None = None,
        final_only: bool = True,
        page_count: int = 100,
    ) -> list[DisclosureEvent]:
        if end < begin:
            raise ValueError("end cannot be earlier than begin")
        if not 1 <= page_count <= 100:
            raise ValueError("page_count must be between 1 and 100")
        query = {
            "crtfc_key": self.api_key,
            "bgn_de": begin.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "last_reprt_at": "Y" if final_only else "N",
            "page_no": "1",
            "page_count": str(page_count),
        }
        if corporation_code:
            query["corp_code"] = corporation_code
        response = self.transport(
            HttpRequest(method="GET", url=f"{self.base_url}/list.json", query=query)
        )
        payload = response.json_body
        status = str(payload.get("status", ""))
        if status == "013":
            return []
        if response.status >= 400 or status != "000":
            raise ProviderError(
                "open_dart",
                status or str(response.status),
                str(payload.get("message", "disclosure search failed")),
            )
        events: list[DisclosureEvent] = []
        for item in payload.get("list", []):
            receipt_number = str(item["rcept_no"])
            report_name = str(item.get("report_nm", ""))
            events.append(
                DisclosureEvent(
                    receipt_number=receipt_number,
                    corporation_name=str(item.get("corp_name", "")),
                    stock_code=str(item["stock_code"]).strip() or None,
                    report_name=report_name,
                    filer_name=str(item.get("flr_nm", "")),
                    receipt_date=datetime.strptime(str(item["rcept_dt"]), "%Y%m%d").date(),
                    corrected=report_name.startswith("[\uc815\uc815]"),
                    detail_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_number}",
                )
            )
        return events


@dataclass(frozen=True)
class MacroPoint:
    statistic_code: str
    cycle: str
    time: str
    value: float
    value_raw: str
    item_code1: str | None
    item_name1: str | None
    unit_name: str | None


class EcosClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: JsonTransport = urllib_json_transport,
    ) -> None:
        if not api_key:
            raise ConfigurationError("ECOS API key is required")
        self.api_key = api_key
        self.transport = transport
        self.base_url = "https://ecos.bok.or.kr/api"

    @classmethod
    def from_env(cls, *, transport: JsonTransport = urllib_json_transport) -> "EcosClient":
        return cls(os.environ.get("ECOS_API_KEY", ""), transport=transport)

    def search(
        self,
        statistic_code: str,
        cycle: str,
        start: str,
        end: str,
        *,
        item_code1: str | None = None,
        limit: int = 100,
    ) -> list[MacroPoint]:
        if not statistic_code or not cycle or not start or not end:
            raise ValueError("statistic_code, cycle, start, and end are required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        segments = [
            "StatisticSearch",
            self.api_key,
            "json",
            "kr",
            "1",
            str(limit),
            statistic_code,
            cycle,
            start,
            end,
        ]
        if item_code1:
            segments.append(item_code1)
        response = self.transport(
            HttpRequest(
                method="GET",
                url=f"{self.base_url}/{'/'.join(segments)}",
                sensitive_values=(self.api_key,),
            )
        )
        payload = response.json_body
        result = payload.get("RESULT")
        if isinstance(result, dict):
            code = str(result.get("CODE", ""))
            if code == "INFO-200":
                return []
            raise ProviderError("ecos", code, str(result.get("MESSAGE", "request failed")))
        container = payload.get("StatisticSearch")
        if response.status >= 400 or not isinstance(container, dict):
            raise ProviderError("ecos", str(response.status), "invalid StatisticSearch response")
        points: list[MacroPoint] = []
        for row in container.get("row", []):
            raw = str(row.get("DATA_VALUE", "")).replace(",", "").strip()
            if raw in {"", "-"}:
                continue
            points.append(
                MacroPoint(
                    statistic_code=statistic_code,
                    cycle=cycle,
                    time=str(row.get("TIME", "")),
                    value=float(raw),
                    value_raw=str(row.get("DATA_VALUE", "")),
                    item_code1=str(row["ITEM_CODE1"]) if row.get("ITEM_CODE1") else None,
                    item_name1=str(row["ITEM_NAME1"]) if row.get("ITEM_NAME1") else None,
                    unit_name=str(row["UNIT_NAME"]) if row.get("UNIT_NAME") else None,
                )
            )
        return points
