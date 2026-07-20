from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import (
    LocalPointInTimeEvidenceProvider,
    PointInTimeRecord,
)
from tradinglab_agents.data.news_provider import LocalNewsProvider, NewsEvent
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


def merge_bars_csv(bars: Iterable[Bar], path: str | Path) -> int:
    incoming = list(bars)
    output = Path(path)
    if not output.is_file():
        return write_bars_csv(incoming, output)
    if not incoming:
        with output.open(newline="", encoding="utf-8") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    symbol = incoming[0].symbol
    if any(item.symbol != symbol for item in incoming):
        raise ValueError("cannot merge bars from multiple symbols into one CSV")
    existing = LocalCsvProvider(output, symbol).bars
    merged = {item.timestamp: item for item in existing}
    merged.update({item.timestamp: item for item in incoming})
    return write_bars_csv(
        [merged[key] for key in sorted(merged)],
        output,
    )


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


def merge_news_jsonl(events: Iterable[NewsEvent], path: str | Path) -> int:
    incoming = list(events)
    output = Path(path)
    existing = LocalNewsProvider(output).events if output.is_file() else ()
    merged = {item.event_id: item for item in existing}
    merged.update({item.event_id: item for item in incoming})
    rows = sorted(merged.values(), key=lambda item: (item.available_at, item.event_id))
    return write_news_jsonl(rows, output)


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


def merge_evidence_jsonl(
    records: Iterable[PointInTimeRecord],
    path: str | Path,
) -> int:
    incoming = list(records)
    output = Path(path)
    existing = (
        LocalPointInTimeEvidenceProvider(output).records
        if output.is_file()
        else ()
    )
    merged = {item.evidence_id: item for item in existing}
    merged.update({item.evidence_id: item for item in incoming})
    rows = sorted(
        merged.values(),
        key=lambda item: (item.available_at, item.timestamp, item.evidence_id),
    )
    return write_evidence_jsonl(rows, output)
