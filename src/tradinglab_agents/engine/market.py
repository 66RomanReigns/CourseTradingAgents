from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.models import Bar, MarketSnapshot


class AlignedMarketData:
    """Synchronized daily market view over multiple point-in-time providers.

    The engine trades only timestamps present for every symbol. This keeps one
    portfolio valuation clock and prevents stale or missing marks from entering
    allocation, risk or execution calculations.
    """

    def __init__(self, providers: Mapping[str, LocalCsvProvider]):
        if len(providers) < 2:
            raise ValueError("multi-asset market data requires at least two providers")
        normalized: dict[str, LocalCsvProvider] = {}
        maps: dict[str, dict[datetime, Bar]] = {}
        for raw_symbol, provider in providers.items():
            symbol = raw_symbol.upper()
            if symbol != provider.symbol:
                raise ValueError(
                    f"provider symbol mismatch: key={symbol}, provider={provider.symbol}"
                )
            if symbol in normalized:
                raise ValueError(f"duplicate provider symbol: {symbol}")
            normalized[symbol] = provider
            maps[symbol] = {bar.timestamp: bar for bar in provider.bars}

        common = set.intersection(*(set(items) for items in maps.values()))
        self.providers = normalized
        self._maps = maps
        self.timestamps = tuple(sorted(common))
        if len(self.timestamps) < 2:
            raise ValueError("providers do not share at least two synchronized timestamps")

        for timestamp in self.timestamps:
            open_times = {maps[symbol][timestamp].open_at for symbol in normalized}
            available_times = {
                maps[symbol][timestamp].available_at for symbol in normalized
            }
            if len(open_times) != 1:
                raise ValueError(
                    f"assets have unsynchronized open times at {timestamp.isoformat()}"
                )
            if any(available_at < timestamp for available_at in available_times):
                raise ValueError("market data contains evidence available before bar completion")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.providers))

    @property
    def dropped_timestamp_count(self) -> int:
        total_unique = set().union(*(set(items) for items in self._maps.values()))
        return len(total_unique.difference(self.timestamps))

    def bar(self, symbol: str, timestamp: datetime) -> Bar:
        normalized = symbol.upper()
        try:
            return self._maps[normalized][timestamp]
        except KeyError as exc:
            raise ValueError(
                f"missing synchronized bar for {normalized} at {timestamp.isoformat()}"
            ) from exc

    def decision_time(self, timestamp: datetime) -> datetime:
        return max(self.bar(symbol, timestamp).available_at for symbol in self.symbols)

    def close_snapshot(self, timestamp: datetime) -> MarketSnapshot:
        return MarketSnapshot(
            timestamp=self.decision_time(timestamp),
            prices={symbol: self.bar(symbol, timestamp).close for symbol in self.symbols},
            field="close",
        )

    def open_snapshot(self, timestamp: datetime) -> MarketSnapshot:
        bars = [self.bar(symbol, timestamp) for symbol in self.symbols]
        open_times = {bar.open_at for bar in bars}
        if len(open_times) != 1:
            raise ValueError(
                f"assets have unsynchronized execution opens at {timestamp.isoformat()}"
            )
        return MarketSnapshot(
            timestamp=next(iter(open_times)),
            prices={bar.symbol: bar.open for bar in bars},
            field="open",
        )

    def history(self, symbol: str, decision_time: datetime) -> list[Bar]:
        return self.providers[symbol.upper()].history(decision_time)

    def index_of(self, timestamp: datetime) -> int:
        try:
            return self.timestamps.index(timestamp)
        except ValueError as exc:
            raise ValueError(f"timestamp is not a synchronized market session: {timestamp}") from exc

    def timestamp_for_date(self, session_date: date | str) -> datetime:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        matches = [timestamp for timestamp in self.timestamps if timestamp.date() == target]
        if len(matches) != 1:
            raise ValueError(f"expected one synchronized session for {target}, found {len(matches)}")
        return matches[0]

    def next_timestamp(self, timestamp: datetime) -> datetime | None:
        index = self.index_of(timestamp)
        return self.timestamps[index + 1] if index + 1 < len(self.timestamps) else None

    def next_after_date(self, session_date: date | str | None) -> datetime | None:
        if session_date is None:
            return self.timestamps[0]
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        return next((timestamp for timestamp in self.timestamps if timestamp.date() > target), None)
