from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tradinglab_agents.data.http_client import CachedHttpJsonClient, DataApiError, JsonHttpClient
from tradinglab_agents.data.news_provider import NewsEvent


class AlphaVantageNewsClient:
    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(
        self,
        api_key: str | None = None,
        http: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        self.http = http or CachedHttpJsonClient()
        if not self.api_key:
            raise ValueError("ALPHA_VANTAGE_API_KEY is required")

    @staticmethod
    def _format_time(value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime("%Y%m%dT%H%M")
        text = str(value).strip()
        if len(text) == 10 and text[4] == "-":
            return datetime.fromisoformat(text).strftime("%Y%m%dT%H%M")
        return text

    @staticmethod
    def _parse_time(value: str) -> datetime:
        for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
            try:
                published_utc = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return published_utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
            except ValueError:
                continue
        raise DataApiError(f"invalid Alpha Vantage time_published: {value!r}")

    @staticmethod
    def _event_id(symbol: str, row: dict[str, Any]) -> str:
        identity = "|".join(
            [
                symbol.upper(),
                str(row.get("time_published", "")),
                str(row.get("url", "")),
                str(row.get("title", "")),
            ]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

    def fetch_news(
        self,
        symbol: str,
        *,
        time_from: datetime | str | None = None,
        time_to: datetime | str | None = None,
        limit: int = 200,
        sort: str = "EARLIEST",
        cache_ttl_seconds: int = 1800,
        force_refresh: bool = False,
    ) -> list[NewsEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": symbol.upper(),
            "time_from": self._format_time(time_from),
            "time_to": self._format_time(time_to),
            "sort": sort.upper(),
            "limit": limit,
            "apikey": self.api_key,
        }
        payload = self.http.get_json(
            self.BASE_URL,
            params,
            cache_ttl_seconds=cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        if "Information" in payload or "Note" in payload or "Error Message" in payload:
            message = payload.get("Information") or payload.get("Note") or payload.get("Error Message")
            raise DataApiError(f"Alpha Vantage error: {message}")
        feed = payload.get("feed", [])
        if not isinstance(feed, list):
            raise DataApiError("Alpha Vantage feed must be a list")
        events: list[NewsEvent] = []
        for row in feed:
            if not isinstance(row, dict):
                continue
            published_at = self._parse_time(str(row.get("time_published", "")))
            source = str(row.get("source") or "alpha_vantage")
            ticker_notes: list[str] = []
            ticker_sentiment = row.get("ticker_sentiment", [])
            if isinstance(ticker_sentiment, list):
                for item in ticker_sentiment:
                    if isinstance(item, dict) and str(item.get("ticker", "")).upper() == symbol.upper():
                        score = item.get("ticker_sentiment_score")
                        label = item.get("ticker_sentiment_label")
                        relevance = item.get("relevance_score")
                        ticker_notes.append(
                            f"ticker sentiment={label} score={score} relevance={relevance}"
                        )
            summary = str(row.get("summary") or "").strip()
            if ticker_notes:
                summary = (summary + " " + " ".join(ticker_notes)).strip()
            events.append(
                NewsEvent(
                    event_id=self._event_id(symbol, row),
                    symbol=symbol.upper(),
                    published_at=published_at,
                    available_at=published_at,
                    headline=str(row.get("title") or "Untitled news"),
                    summary=summary,
                    source=source,
                )
            )
        events.sort(key=lambda item: (item.available_at, item.event_id))
        return events
