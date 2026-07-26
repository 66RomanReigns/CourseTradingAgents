from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from pathlib import Path

from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar


def parse_day(value: str) -> date:
    cleaned = value.strip()
    for parser in (date.fromisoformat, lambda text: datetime.fromisoformat(text).date()):
        try:
            return parser(cleaned)
        except ValueError:
            pass
    raise ValueError(f"unsupported date value: {value!r}")


def normalize(
    input_path: Path,
    output_path: Path,
    *,
    calendar_name: str = "XNYS",
) -> int:
    calendar = ExchangeTradingCalendar(calendar_name)
    with input_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        field_lookup = {name.lower().replace(" ", "_"): name for name in reader.fieldnames or []}
        date_key = field_lookup.get("date") or field_lookup.get("timestamp")
        required = {
            "open": field_lookup.get("open"),
            "high": field_lookup.get("high"),
            "low": field_lookup.get("low"),
            "close": field_lookup.get("close"),
            "volume": field_lookup.get("volume"),
        }
        if not date_key or any(value is None for value in required.values()):
            raise ValueError("input must contain Date/Timestamp, Open, High, Low, Close and Volume")
        rows = []
        for raw in reader:
            day = parse_day(raw[date_key])
            session = calendar.session(day)
            open_at = session.open_at
            close_at = session.close_at
            rows.append(
                {
                    "timestamp": close_at.isoformat(),
                    "open_at": open_at.isoformat(),
                    "open": float(raw[required["open"]]),
                    "high": float(raw[required["high"]]),
                    "low": float(raw[required["low"]]),
                    "close": float(raw[required["close"]]),
                    "volume": float(raw[required["volume"]]),
                    "available_at": close_at.isoformat(),
                }
            )
    rows.sort(key=lambda row: row["timestamp"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(
            destination,
            fieldnames=list(rows[0]) if rows else [
                "timestamp", "open_at", "open", "high", "low", "close", "volume", "available_at"
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize common Yahoo-style OHLCV CSV")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--calendar", default="XNYS")
    args = parser.parse_args()
    count = normalize(args.input, args.output, calendar_name=args.calendar)
    print(f"normalized_rows={count} output={args.output}")


if __name__ == "__main__":
    main()
