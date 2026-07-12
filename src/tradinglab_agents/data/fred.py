from __future__ import annotations

import os
from datetime import date, datetime, time
from typing import Any

from tradinglab_agents.data.evidence_provider import PointInTimeRecord
from tradinglab_agents.data.http_client import CachedHttpJsonClient, DataApiError, JsonHttpClient


class FredClient:
    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(
        self,
        api_key: str | None = None,
        http: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("FRED_API_KEY", "")
        self.http = http or CachedHttpJsonClient()
        if not self.api_key:
            raise ValueError("FRED_API_KEY is required")

    def fetch_initial_release_records(
        self,
        series_id: str,
        *,
        observation_start: str | date | None = None,
        observation_end: str | date | None = None,
        symbol: str = "MACRO",
        detail: str | None = None,
        cache_ttl_seconds: int = 24 * 3600,
        force_refresh: bool = False,
    ) -> list[PointInTimeRecord]:
        """Fetch the earliest vintage for each observation using FRED output_type=4."""
        normalized_series = series_id.upper()
        params: dict[str, Any] = {
            "series_id": normalized_series,
            "api_key": self.api_key,
            "file_type": "json",
            "output_type": 4,
            "sort_order": "asc",
            "observation_start": str(observation_start) if observation_start else None,
            "observation_end": str(observation_end) if observation_end else None,
        }
        payload = self.http.get_json(
            self.BASE_URL,
            params,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        if "error_code" in payload or "error_message" in payload:
            raise DataApiError(
                f"FRED error: {payload.get('error_message') or payload.get('error_code')}"
            )
        observations = payload.get("observations", [])
        if not isinstance(observations, list):
            raise DataApiError("FRED observations must be a list")

        earliest: dict[date, tuple[date, float]] = {}
        for row in observations:
            if not isinstance(row, dict):
                continue
            raw_value = str(row.get("value", "."))
            if raw_value in {".", "", "nan", "NaN"}:
                continue
            try:
                observation_date = date.fromisoformat(str(row["date"]))
                release_date = date.fromisoformat(str(row["realtime_start"]))
                value = float(raw_value)
            except (KeyError, TypeError, ValueError) as exc:
                raise DataApiError(f"invalid FRED observation: {row!r}") from exc
            previous = earliest.get(observation_date)
            if previous is None or release_date < previous[0]:
                earliest[observation_date] = (release_date, value)

        records: list[PointInTimeRecord] = []
        for observation_date, (release_date, value) in sorted(earliest.items()):
            timestamp = datetime.combine(observation_date, time.min)
            # The vintage date has no intraday timestamp. End-of-day is conservative.
            available_at = datetime.combine(release_date, time(23, 59, 59))
            records.append(
                PointInTimeRecord(
                    symbol=symbol.upper(),
                    series_id=f"fred.{normalized_series}",
                    evidence_id=(
                        f"fred.{normalized_series}.{observation_date.isoformat()}."
                        f"{release_date.isoformat()}"
                    ),
                    kind="macro",
                    timestamp=timestamp,
                    available_at=available_at,
                    value=value,
                    source=f"fred:{normalized_series}",
                    detail=detail or f"FRED {normalized_series} initial release",
                )
            )
        return records
