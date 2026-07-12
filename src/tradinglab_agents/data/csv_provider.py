from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from tradinglab_agents.models import Bar


class LocalCsvProvider:
    """Read deterministic OHLCV data and enforce point-in-time availability."""

    REQUIRED = {"timestamp", "open", "high", "low", "close", "volume"}

    def __init__(self, path: str | Path, symbol: str):
        self.path = Path(path)
        self.symbol = symbol.upper()
        self._bars = self._load()

    def _load(self) -> list[Bar]:
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = self.REQUIRED.difference(reader.fieldnames or [])
            if missing:
                raise ValueError(f"missing CSV columns: {sorted(missing)}")
            bars: list[Bar] = []
            for row in reader:
                timestamp = datetime.fromisoformat(row["timestamp"])
                available_at = datetime.fromisoformat(row.get("available_at") or row["timestamp"])
                bars.append(
                    Bar(
                        symbol=self.symbol,
                        timestamp=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        available_at=available_at,
                    )
                )
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    @property
    def bars(self) -> tuple[Bar, ...]:
        return tuple(self._bars)

    def history(self, decision_time: datetime, limit: int | None = None) -> list[Bar]:
        visible = [bar for bar in self._bars if bar.available_at <= decision_time]
        return visible[-limit:] if limit else visible
