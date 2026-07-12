from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from tradinglab_agents.data.evidence_provider import PointInTimeRecord
from tradinglab_agents.data.news_provider import NewsEvent
from tradinglab_agents.models import Bar


def write_bars_csv(bars: Iterable[Bar], path: str | Path) -> int:
    rows = list(bars)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["timestamp", "open_at", "open", "high", "low", "close", "volume", "available_at"]
        )
        for bar in rows:
            writer.writerow(
                [
                    bar.timestamp.isoformat(),
                    bar.open_at.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.available_at.isoformat(),
                ]
            )
    return len(rows)


def write_news_jsonl(events: Iterable[NewsEvent], path: str | Path) -> int:
    rows = list(events)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for item in rows:
            handle.write(
                json.dumps(
                    {
                        "event_id": item.event_id,
                        "symbol": item.symbol,
                        "published_at": item.published_at.isoformat(),
                        "available_at": item.available_at.isoformat(),
                        "headline": item.headline,
                        "summary": item.summary,
                        "source": item.source,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return len(rows)


def write_evidence_jsonl(records: Iterable[PointInTimeRecord], path: str | Path) -> int:
    rows = list(records)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for item in rows:
            handle.write(
                json.dumps(item.to_json_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    return len(rows)
