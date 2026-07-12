from __future__ import annotations

import os
from datetime import date, datetime, time
from typing import Any, Iterable

from tradinglab_agents.data.evidence_provider import PointInTimeRecord
from tradinglab_agents.data.http_client import CachedHttpJsonClient, DataApiError, JsonHttpClient


DEFAULT_US_GAAP_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetCashProvidedByUsedInOperatingActivities",
    "EarningsPerShareDiluted",
)


class SecEdgarClient:
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

    def __init__(
        self,
        user_agent: str | None = None,
        http: JsonHttpClient | None = None,
    ):
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "")
        self.http = http or CachedHttpJsonClient()
        if not self.user_agent or "@" not in self.user_agent:
            raise ValueError(
                "SEC_USER_AGENT is required and should identify the application and a contact email"
            )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }

    def ticker_to_cik(
        self,
        ticker: str,
        *,
        cache_ttl_seconds: int = 7 * 24 * 3600,
        force_refresh: bool = False,
    ) -> str:
        # The ticker map is hosted on www.sec.gov, so do not send a mismatched Host header.
        payload = self.http.get_json(
            self.TICKERS_URL,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        target = ticker.upper()
        for row in payload.values():
            if isinstance(row, dict) and str(row.get("ticker", "")).upper() == target:
                try:
                    return f"{int(row['cik_str']):010d}"
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataApiError(f"invalid SEC ticker mapping for {target}") from exc
        raise DataApiError(f"SEC ticker not found: {target}")

    def fetch_company_facts(
        self,
        ticker: str,
        *,
        cache_ttl_seconds: int = 24 * 3600,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        cik = self.ticker_to_cik(
            ticker,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        payload = self.http.get_json(
            self.COMPANY_FACTS_URL.format(cik=cik),
            headers=self.headers,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        if "facts" not in payload:
            raise DataApiError(f"SEC company facts missing for {ticker.upper()}")
        return payload

    def fetch_fundamental_records(
        self,
        ticker: str,
        *,
        concepts: Iterable[str] = DEFAULT_US_GAAP_CONCEPTS,
        forms: Iterable[str] = ("10-K", "10-Q", "10-K/A", "10-Q/A"),
        filed_start: str | date | None = None,
        filed_end: str | date | None = None,
        cache_ttl_seconds: int = 24 * 3600,
        force_refresh: bool = False,
    ) -> list[PointInTimeRecord]:
        payload = self.fetch_company_facts(
            ticker,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        us_gaap = payload.get("facts", {}).get("us-gaap", {})
        if not isinstance(us_gaap, dict):
            raise DataApiError(f"SEC us-gaap facts missing for {ticker.upper()}")
        allowed_forms = {item.upper() for item in forms}
        lower = date.fromisoformat(str(filed_start)) if filed_start else None
        upper = date.fromisoformat(str(filed_end)) if filed_end else None
        records: list[PointInTimeRecord] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for concept in concepts:
            concept_payload = us_gaap.get(concept)
            if not isinstance(concept_payload, dict):
                continue
            label = str(concept_payload.get("label") or concept)
            units = concept_payload.get("units", {})
            if not isinstance(units, dict):
                continue
            for unit, facts in units.items():
                if not isinstance(facts, list):
                    continue
                for fact in facts:
                    if not isinstance(fact, dict):
                        continue
                    form = str(fact.get("form", "")).upper()
                    if form not in allowed_forms:
                        continue
                    try:
                        filed = date.fromisoformat(str(fact["filed"]))
                        period_end = date.fromisoformat(str(fact["end"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if lower and filed < lower:
                        continue
                    if upper and filed > upper:
                        continue
                    accession = str(fact.get("accn") or "unknown")
                    dedupe_key = (concept, unit, form, filed.isoformat(), accession)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    value = fact.get("val")
                    if value is None:
                        continue
                    if isinstance(value, bool):
                        normalized: float | str = str(value)
                    elif isinstance(value, (int, float)):
                        normalized = float(value)
                    else:
                        normalized = str(value)
                    fiscal_year = fact.get("fy")
                    fiscal_period = fact.get("fp")
                    series_id = f"sec.{ticker.upper()}.{concept}.{form.replace('/', '_')}"
                    evidence_id = (
                        f"{series_id}.{period_end.isoformat()}.{filed.isoformat()}."
                        f"{accession.replace('-', '')}"
                    )
                    records.append(
                        PointInTimeRecord(
                            symbol=ticker.upper(),
                            series_id=series_id,
                            evidence_id=evidence_id,
                            kind="fundamental",
                            timestamp=datetime.combine(period_end, time.min),
                            # Company Facts includes the filing date but not acceptance time.
                            # End-of-day availability is conservative and avoids same-day lookahead.
                            available_at=datetime.combine(filed, time(23, 59, 59)),
                            value=normalized,
                            source=f"sec-edgar:{accession}",
                            detail=(
                                f"{label}; unit={unit}; form={form}; fy={fiscal_year}; "
                                f"fp={fiscal_period}; period_end={period_end.isoformat()}"
                            ),
                        )
                    )
        records.sort(key=lambda item: (item.available_at, item.series_id, item.timestamp))
        return records
