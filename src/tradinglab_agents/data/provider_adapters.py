from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.evidence_provider import (
    LocalPointInTimeEvidenceProvider,
    PointInTimeRecord,
)
from tradinglab_agents.data.fallback_router import ProviderRequest
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.http_client import CachedHttpJsonClient
from tradinglab_agents.data.news_provider import LocalNewsProvider, NewsEvent
from tradinglab_agents.data.provider_quality import ProviderCandidate
from tradinglab_agents.data.provider_registry import DataKind, ProviderCapability
from tradinglab_agents.data.sec_edgar import DEFAULT_US_GAAP_CONCEPTS, SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import (
    merge_bars_csv,
    merge_evidence_jsonl,
    merge_news_jsonl,
)
from tradinglab_agents.models import Bar


_LOCAL_TZ = ZoneInfo("America/New_York")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=_LOCAL_TZ).astimezone(timezone.utc)


def _file_modified(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _bar_record(item: Bar) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "timestamp": item.timestamp.isoformat(),
        "open_at": item.open_at.isoformat(),
        "open": item.open,
        "high": item.high,
        "low": item.low,
        "close": item.close,
        "volume": item.volume,
        "available_at": item.available_at.isoformat(),
    }


def _news_record(item: NewsEvent) -> dict[str, Any]:
    return {
        "event_id": item.event_id,
        "symbol": item.symbol,
        "published_at": item.published_at.isoformat(),
        "available_at": item.available_at.isoformat(),
        "headline": item.headline,
        "summary": item.summary,
        "source": item.source,
    }


def _evidence_record(item: PointInTimeRecord) -> dict[str, Any]:
    return item.to_json_dict()


def _bar_from_record(row: dict[str, Any]) -> Bar:
    return Bar(
        symbol=str(row["symbol"]).upper(),
        timestamp=datetime.fromisoformat(str(row["timestamp"])),
        open_at=datetime.fromisoformat(str(row["open_at"])),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        available_at=datetime.fromisoformat(str(row["available_at"])),
    )


def _news_from_record(row: dict[str, Any]) -> NewsEvent:
    return NewsEvent(
        event_id=str(row["event_id"]),
        symbol=str(row["symbol"]).upper(),
        published_at=datetime.fromisoformat(str(row["published_at"])),
        available_at=datetime.fromisoformat(str(row["available_at"])),
        headline=str(row["headline"]),
        summary=str(row.get("summary", "")),
        source=str(row.get("source", "provider_graph")),
    )


def _evidence_from_record(row: dict[str, Any]) -> PointInTimeRecord:
    return PointInTimeRecord(
        symbol=str(row["symbol"]).upper(),
        series_id=str(row["series_id"]),
        evidence_id=str(row["evidence_id"]),
        kind=str(row["kind"]),
        timestamp=datetime.fromisoformat(str(row["timestamp"])),
        available_at=datetime.fromisoformat(str(row["available_at"])),
        value=row["value"],
        source=str(row["source"]),
        detail=str(row.get("detail", "")),
    )


class ProviderAdapterFactory:
    """Construct canonical provider fetchers without eager credential access."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        calendar_name: str = "XNYS",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.calendar_name = calendar_name

    @staticmethod
    def _date(value: Any) -> date | None:
        if value in {None, ""}:
            return None
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    def _market_metadata(self, request: ProviderRequest) -> dict[str, Any]:
        return dict(request.metadata or {})

    @staticmethod
    def _http(capability: ProviderCapability) -> CachedHttpJsonClient:
        return CachedHttpJsonClient(
            timeout_seconds=capability.timeout_seconds,
            max_retries=capability.max_retries,
        )

    def fetch_twelve_data(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> ProviderCandidate:
        metadata = self._market_metadata(request)
        bars = TwelveDataClient(http=self._http(capability)).fetch_daily_bars(
            request.resource,
            start_date=self._date(metadata.get("start_date")),
            end_date=self._date(metadata.get("end_date")),
            outputsize=int(metadata.get("outputsize", 5000)),
            force_refresh=bool(metadata.get("force_refresh", False)),
            calendar_name=self.calendar_name,
        )
        now = datetime.now(timezone.utc)
        expected = max(1, int(metadata.get("expected_min_records", 1)))
        return ProviderCandidate(
            provider="twelve_data",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_bar_record(item) for item in bars),
            observed_at=now,
            latest_data_at=_utc(bars[-1].available_at),
            completeness=min(1.0, len(bars) / expected),
            metadata={
                "latest_record_at": bars[-1].available_at.isoformat(),
                "source": "live",
            },
        )

    def fetch_alpha_vantage_market(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> ProviderCandidate:
        metadata = self._market_metadata(request)
        bars = AlphaVantageNewsClient(
            http=self._http(capability)
        ).fetch_daily_bars(
            request.resource,
            outputsize=(
                "full"
                if int(metadata.get("outputsize", 5000)) > 100
                else "compact"
            ),
            force_refresh=bool(metadata.get("force_refresh", False)),
            calendar_name=self.calendar_name,
            start_date=self._date(metadata.get("start_date")),
            end_date=self._date(metadata.get("end_date")),
        )
        now = datetime.now(timezone.utc)
        expected = max(1, int(metadata.get("expected_min_records", 1)))
        return ProviderCandidate(
            provider="alpha_vantage_market",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_bar_record(item) for item in bars),
            observed_at=now,
            latest_data_at=_utc(bars[-1].available_at),
            completeness=min(1.0, len(bars) / expected),
            metadata={
                "latest_record_at": bars[-1].available_at.isoformat(),
                "source": "live",
            },
        )

    def fetch_local_market(
        self,
        request: ProviderRequest,
        _capability: ProviderCapability,
    ) -> ProviderCandidate:
        from tradinglab_agents.data.csv_provider import LocalCsvProvider

        path = self.data_dir / f"{request.resource}.csv"
        provider = LocalCsvProvider(path, request.resource)
        metadata = self._market_metadata(request)
        start = self._date(metadata.get("start_date"))
        end = self._date(metadata.get("end_date"))
        bars = [
            item
            for item in provider.bars
            if (start is None or item.timestamp.date() >= start)
            and (end is None or item.timestamp.date() <= end)
        ]
        if not bars:
            raise ValueError(f"local market cache contains no rows for {request.resource}")
        return ProviderCandidate(
            provider="local_market_cache",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_bar_record(item) for item in bars),
            observed_at=datetime.now(timezone.utc),
            latest_data_at=_utc(bars[-1].available_at),
            completeness=1.0,
            metadata={
                "path": str(path),
                "file_modified_at": _file_modified(path).isoformat(),
                "latest_record_at": bars[-1].available_at.isoformat(),
                "source": "local_cache",
            },
        )

    def fetch_alpha_vantage_news(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> ProviderCandidate:
        metadata = dict(request.metadata or {})
        events = AlphaVantageNewsClient(
            http=self._http(capability)
        ).fetch_news(
            request.resource,
            time_from=metadata.get("time_from"),
            time_to=metadata.get("time_to"),
            limit=int(metadata.get("limit", 200)),
            sort=str(metadata.get("sort", "EARLIEST")),
            force_refresh=bool(metadata.get("force_refresh", False)),
        )
        now = datetime.now(timezone.utc)
        return ProviderCandidate(
            provider="alpha_vantage",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_news_record(item) for item in events),
            observed_at=now,
            latest_data_at=(
                _utc(events[-1].available_at) if events else now
            ),
            completeness=1.0,
            metadata={"source": "live", "empty_is_valid": True},
        )

    def fetch_local_news(
        self,
        request: ProviderRequest,
        _capability: ProviderCapability,
    ) -> ProviderCandidate:
        path = self.data_dir / f"{request.resource}_news.jsonl"
        provider = LocalNewsProvider(path)
        events = list(provider.events)
        return ProviderCandidate(
            provider="local_news_cache",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_news_record(item) for item in events),
            observed_at=datetime.now(timezone.utc),
            latest_data_at=(
                _utc(events[-1].available_at)
                if events
                else (
                    _file_modified(path)
                    if path.is_file()
                    else datetime.now(timezone.utc)
                )
            ),
            completeness=1.0,
            metadata={
                "path": str(path),
                "source": "local_cache",
                "empty_is_valid": True,
            },
        )

    def fetch_fred(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> ProviderCandidate:
        metadata = dict(request.metadata or {})
        records = FredClient(http=self._http(capability)).fetch_initial_release_records(
            request.resource,
            observation_start=metadata.get("observation_start"),
            observation_end=metadata.get("observation_end"),
            symbol="MACRO",
            detail=metadata.get("detail"),
            force_refresh=bool(metadata.get("force_refresh", False)),
        )
        now = datetime.now(timezone.utc)
        return ProviderCandidate(
            provider="fred",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_evidence_record(item) for item in records),
            observed_at=now,
            latest_data_at=(
                _utc(records[-1].available_at) if records else now
            ),
            completeness=1.0,
            metadata={"source": "live"},
        )

    def fetch_local_macro(
        self,
        request: ProviderRequest,
        _capability: ProviderCapability,
    ) -> ProviderCandidate:
        path = self.data_dir / "macro.jsonl"
        provider = LocalPointInTimeEvidenceProvider(path)
        records = [
            item
            for item in provider.records
            if item.series_id == f"fred.{request.resource.upper()}"
        ]
        if not records:
            raise ValueError(f"local macro cache missing series {request.resource}")
        return ProviderCandidate(
            provider="local_macro_cache",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_evidence_record(item) for item in records),
            observed_at=datetime.now(timezone.utc),
            latest_data_at=_utc(records[-1].available_at),
            completeness=1.0,
            metadata={
                "path": str(path),
                "file_modified_at": _file_modified(path).isoformat(),
                "source": "local_cache",
            },
        )

    def fetch_sec(
        self,
        request: ProviderRequest,
        capability: ProviderCapability,
    ) -> ProviderCandidate:
        metadata = dict(request.metadata or {})
        records = SecEdgarClient(
            http=self._http(capability)
        ).fetch_fundamental_records(
            request.resource,
            concepts=tuple(metadata.get("concepts", DEFAULT_US_GAAP_CONCEPTS)),
            filed_start=metadata.get("filed_start"),
            filed_end=metadata.get("filed_end"),
            force_refresh=bool(metadata.get("force_refresh", False)),
        )
        now = datetime.now(timezone.utc)
        return ProviderCandidate(
            provider="sec_edgar",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_evidence_record(item) for item in records),
            observed_at=now,
            latest_data_at=(
                _utc(records[-1].available_at) if records else now
            ),
            completeness=1.0,
            metadata={"source": "live"},
        )

    def fetch_local_fundamentals(
        self,
        request: ProviderRequest,
        _capability: ProviderCapability,
    ) -> ProviderCandidate:
        path = self.data_dir / f"{request.resource}_fundamentals.jsonl"
        provider = LocalPointInTimeEvidenceProvider(path)
        records = list(provider.records)
        if not records:
            raise ValueError(
                f"local fundamentals cache contains no rows for {request.resource}"
            )
        return ProviderCandidate(
            provider="local_fundamentals_cache",
            data_kind=request.data_kind,
            resource=request.resource,
            records=tuple(_evidence_record(item) for item in records),
            observed_at=datetime.now(timezone.utc),
            latest_data_at=_utc(records[-1].available_at),
            completeness=1.0,
            metadata={
                "path": str(path),
                "file_modified_at": _file_modified(path).isoformat(),
                "source": "local_cache",
            },
        )

    def fetchers(self):
        return {
            "twelve_data": self.fetch_twelve_data,
            "alpha_vantage_market": self.fetch_alpha_vantage_market,
            "local_market_cache": self.fetch_local_market,
            "alpha_vantage": self.fetch_alpha_vantage_news,
            "local_news_cache": self.fetch_local_news,
            "fred": self.fetch_fred,
            "local_macro_cache": self.fetch_local_macro,
            "sec_edgar": self.fetch_sec,
            "local_fundamentals_cache": self.fetch_local_fundamentals,
        }

    def persist_route_result(self, route: dict[str, Any]) -> dict[str, Any]:
        request = dict(route["request"])
        selected = dict(route["selected"])
        records = [dict(item) for item in selected.get("records", [])]
        data_kind = DataKind(str(request["data_kind"]))
        resource = str(request["resource"]).upper()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if data_kind == DataKind.MARKET_DAILY:
            path = self.data_dir / f"{resource}.csv"
            rows = merge_bars_csv((_bar_from_record(item) for item in records), path)
        elif data_kind == DataKind.NEWS:
            path = self.data_dir / f"{resource}_news.jsonl"
            rows = merge_news_jsonl((_news_from_record(item) for item in records), path)
        elif data_kind == DataKind.MACRO:
            path = self.data_dir / "macro.jsonl"
            rows = merge_evidence_jsonl(
                (_evidence_from_record(item) for item in records),
                path,
            )
        elif data_kind == DataKind.FUNDAMENTALS:
            path = self.data_dir / f"{resource}_fundamentals.jsonl"
            rows = merge_evidence_jsonl(
                (_evidence_from_record(item) for item in records),
                path,
            )
        else:  # pragma: no cover - exhaustive enum guard
            raise ValueError(f"unsupported provider data kind: {data_kind}")
        return {
            "request_id": request["request_id"],
            "data_kind": data_kind.value,
            "resource": resource,
            "provider": selected["provider"],
            "rows": rows,
            "fetched_rows": len(records),
            "path": str(path),
            "payload_sha256": selected["payload_sha256"],
            "quality": dict(route["quality"]),
        }


__all__ = ["ProviderAdapterFactory"]
