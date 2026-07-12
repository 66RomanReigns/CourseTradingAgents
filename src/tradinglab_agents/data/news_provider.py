from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from tradinglab_agents.models import Evidence, EvidencePack


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    symbol: str
    published_at: datetime
    available_at: datetime
    headline: str
    summary: str
    source: str


class LocalNewsProvider:
    """Point-in-time JSONL news source used by the context agent."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._events = self._load()

    @property
    def events(self) -> tuple[NewsEvent, ...]:
        return tuple(self._events)

    def _load(self) -> list[NewsEvent]:
        events: list[NewsEvent] = []
        if not self.path.exists():
            return events
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                events.append(
                    NewsEvent(
                        event_id=str(row["event_id"]),
                        symbol=str(row["symbol"]).upper(),
                        published_at=datetime.fromisoformat(row["published_at"]),
                        available_at=datetime.fromisoformat(
                            row.get("available_at") or row["published_at"]
                        ),
                        headline=str(row["headline"]),
                        summary=str(row.get("summary", "")),
                        source=str(row.get("source", "local_fixture")),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid news JSONL at line {line_number}: {exc}") from exc
        events.sort(key=lambda event: event.available_at)
        return events

    def visible_events(
        self,
        symbol: str,
        decision_time: datetime,
        lookback_days: int = 7,
    ) -> list[NewsEvent]:
        lower = decision_time - timedelta(days=lookback_days)
        symbol = symbol.upper()
        return [
            event
            for event in self._events
            if event.symbol == symbol
            and lower <= event.available_at <= decision_time
        ]

    def add_to_pack(
        self,
        pack: EvidencePack,
        lookback_days: int = 7,
    ) -> EvidencePack:
        for event in self.visible_events(pack.symbol, pack.decision_time, lookback_days):
            pack.add(
                Evidence(
                    evidence_id=f"news.{event.event_id}",
                    kind="news",
                    timestamp=event.published_at,
                    available_at=event.available_at,
                    value=f"{event.headline}. {event.summary}".strip(),
                    source=event.source,
                    detail=event.headline,
                )
            )
        return pack
