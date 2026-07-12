from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from tradinglab_agents.models import Evidence, EvidencePack


@dataclass(frozen=True)
class PointInTimeRecord:
    symbol: str
    series_id: str
    evidence_id: str
    kind: str
    timestamp: datetime
    available_at: datetime
    value: float | str
    source: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.series_id.strip():
            raise ValueError("series_id cannot be empty")
        if not self.evidence_id.strip():
            raise ValueError("evidence_id cannot be empty")
        if not self.kind.strip():
            raise ValueError("kind cannot be empty")

    def to_json_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "series_id": self.series_id,
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "timestamp": self.timestamp.isoformat(),
            "available_at": self.available_at.isoformat(),
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
        }


class LocalPointInTimeEvidenceProvider:
    """Load macro/fundamental evidence and expose only records visible at decision time."""

    REQUIRED = {
        "symbol",
        "series_id",
        "evidence_id",
        "kind",
        "timestamp",
        "available_at",
        "value",
        "source",
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._records = self._load()

    def _load(self) -> list[PointInTimeRecord]:
        records: list[PointInTimeRecord] = []
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise TypeError("row must be a JSON object")
                missing = self.REQUIRED.difference(row)
                if missing:
                    raise ValueError(f"missing fields: {sorted(missing)}")
                records.append(
                    PointInTimeRecord(
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
                )
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"invalid evidence JSONL at line {line_number}: {exc}") from exc
        records.sort(key=lambda item: (item.available_at, item.timestamp, item.evidence_id))
        duplicate_ids = len({item.evidence_id for item in records}) != len(records)
        if duplicate_ids:
            raise ValueError("evidence_id values must be unique within a file")
        return records

    @property
    def records(self) -> tuple[PointInTimeRecord, ...]:
        return tuple(self._records)

    def visible_records(
        self,
        symbol: str,
        decision_time: datetime,
        *,
        lookback_days: int | None = None,
        latest_only: bool = True,
    ) -> list[PointInTimeRecord]:
        symbol = symbol.upper()
        lower = decision_time - timedelta(days=lookback_days) if lookback_days else None
        visible = [
            item
            for item in self._records
            if item.available_at <= decision_time
            and item.symbol in {symbol, "*", "MACRO"}
            and (lower is None or item.available_at >= lower)
        ]
        if not latest_only:
            return visible
        latest: dict[str, PointInTimeRecord] = {}
        for item in visible:
            previous = latest.get(item.series_id)
            if previous is None or (item.available_at, item.timestamp) > (
                previous.available_at,
                previous.timestamp,
            ):
                latest[item.series_id] = item
        return sorted(latest.values(), key=lambda item: item.series_id)

    def add_to_pack(
        self,
        pack: EvidencePack,
        *,
        lookback_days: int | None = None,
        latest_only: bool = True,
    ) -> EvidencePack:
        for item in self.visible_records(
            pack.symbol,
            pack.decision_time,
            lookback_days=lookback_days,
            latest_only=latest_only,
        ):
            pack.add(
                Evidence(
                    evidence_id=item.evidence_id,
                    kind=item.kind,
                    timestamp=item.timestamp,
                    available_at=item.available_at,
                    value=item.value,
                    source=item.source,
                    detail=item.detail,
                )
            )
        return pack
