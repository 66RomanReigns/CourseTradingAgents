from __future__ import annotations

import os
from datetime import date, datetime, time
from typing import Any

from tradinglab_agents.data.http_client import CachedHttpJsonClient, DataApiError, JsonHttpClient
from tradinglab_agents.models import Bar


class TwelveDataClient:
    BASE_URL = "https://api.twelvedata.com/time_series"

    def __init__(
        self,
        api_key: str | None = None,
        http: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY", "")
        self.http = http or CachedHttpJsonClient()
        if not self.api_key:
            raise ValueError("TWELVE_DATA_API_KEY is required")

    @staticmethod
    def _parse_session_date(value: str) -> date:
        text = value.strip()
        try:
            return datetime.fromisoformat(text).date()
        except ValueError as exc:
            raise DataApiError(f"invalid Twelve Data datetime: {value!r}") from exc

    def fetch_daily_bars(
        self,
        symbol: str,
        *,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        outputsize: int = 5000,
        timezone_name: str = "America/New_York",
        cache_ttl_seconds: int = 6 * 3600,
        force_refresh: bool = False,
    ) -> list[Bar]:
        if outputsize <= 0:
            raise ValueError("outputsize must be positive")
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": "1day",
            "outputsize": min(outputsize, 5000),
            "order": "ASC",
            "timezone": timezone_name,
            "apikey": self.api_key,
        }
        if start_date is not None:
            params["start_date"] = str(start_date)
        if end_date is not None:
            params["end_date"] = str(end_date)
        payload = self.http.get_json(
            self.BASE_URL,
            params,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        if payload.get("status") == "error" or "values" not in payload:
            message = payload.get("message") or payload.get("code") or "missing values"
            raise DataApiError(f"Twelve Data error: {message}")
        values = payload.get("values")
        if not isinstance(values, list):
            raise DataApiError("Twelve Data values must be a list")
        bars: list[Bar] = []
        for row in values:
            if not isinstance(row, dict):
                continue
            session_date = self._parse_session_date(str(row.get("datetime", "")))
            open_at = datetime.combine(session_date, time(9, 30))
            close_at = datetime.combine(session_date, time(16, 0))
            try:
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        timestamp=close_at,
                        open_at=open_at,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                        available_at=close_at,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DataApiError(f"invalid Twelve Data bar for {session_date}: {exc}") from exc
        bars.sort(key=lambda item: item.timestamp)
        if not bars:
            raise DataApiError(f"Twelve Data returned no daily bars for {symbol.upper()}")
        return bars
