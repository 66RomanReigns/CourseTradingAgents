from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class DataKind(StrEnum):
    MARKET_DAILY = "MARKET_DAILY"
    NEWS = "NEWS"
    MACRO = "MACRO"
    FUNDAMENTALS = "FUNDAMENTALS"


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    data_kind: DataKind
    priority: int
    authority_score: float
    freshness_hours: float
    timeout_seconds: float
    max_retries: int
    quota_units: float
    point_in_time: bool
    rate_limit_group: str | None = None
    min_interval_seconds: float = 0.0
    max_concurrency: int = 1
    application_daily_quota: float | None = None
    supports_live: bool = True
    is_cache: bool = False

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        if not provider:
            raise ValueError("provider name cannot be empty")
        if self.priority < 0:
            raise ValueError("provider priority cannot be negative")
        if not 0.0 <= self.authority_score <= 1.0:
            raise ValueError("authority_score must be in [0, 1]")
        if self.freshness_hours <= 0:
            raise ValueError("freshness_hours must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.quota_units < 0:
            raise ValueError("quota_units cannot be negative")
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if (
            self.application_daily_quota is not None
            and self.application_daily_quota <= 0
        ):
            raise ValueError("application_daily_quota must be positive")
        rate_group = (
            self.rate_limit_group.strip().lower()
            if self.rate_limit_group
            else provider
        )
        if not rate_group:
            raise ValueError("rate_limit_group cannot be empty")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "rate_limit_group", rate_group)


class ProviderRegistry:
    def __init__(self, capabilities: Iterable[ProviderCapability] = ()) -> None:
        self._items: dict[tuple[str, DataKind], ProviderCapability] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: ProviderCapability) -> None:
        key = (capability.provider, capability.data_kind)
        if key in self._items:
            raise ValueError(
                f"duplicate provider capability: {capability.provider}/{capability.data_kind.value}"
            )
        self._items[key] = capability

    def capability(self, provider: str, data_kind: DataKind) -> ProviderCapability:
        key = (provider.strip().lower(), data_kind)
        try:
            return self._items[key]
        except KeyError as exc:
            raise KeyError(
                f"provider capability not registered: {key[0]}/{data_kind.value}"
            ) from exc

    def providers_for(
        self,
        data_kind: DataKind,
        *,
        include_cache: bool = True,
        live_only: bool = False,
    ) -> tuple[ProviderCapability, ...]:
        values = [
            item
            for item in self._items.values()
            if item.data_kind == data_kind
            and (include_cache or not item.is_cache)
            and (not live_only or item.supports_live)
        ]
        return tuple(sorted(values, key=lambda item: (item.priority, item.provider)))

    def providers_in_rate_group(self, group: str) -> tuple[str, ...]:
        normalized = group.strip().lower()
        return tuple(
            sorted(
                {
                    item.provider
                    for item in self._items.values()
                    if item.rate_limit_group == normalized
                }
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "providers": [
                {
                    "provider": item.provider,
                    "data_kind": item.data_kind.value,
                    "priority": item.priority,
                    "authority_score": item.authority_score,
                    "freshness_hours": item.freshness_hours,
                    "timeout_seconds": item.timeout_seconds,
                    "max_retries": item.max_retries,
                    "quota_units": item.quota_units,
                    "point_in_time": item.point_in_time,
                    "rate_limit_group": item.rate_limit_group,
                    "min_interval_seconds": item.min_interval_seconds,
                    "max_concurrency": item.max_concurrency,
                    "application_daily_quota": item.application_daily_quota,
                    "supports_live": item.supports_live,
                    "is_cache": item.is_cache,
                }
                for item in sorted(
                    self._items.values(),
                    key=lambda value: (value.data_kind.value, value.priority, value.provider),
                )
            ]
        }


def default_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        (
            ProviderCapability(
                provider="twelve_data",
                data_kind=DataKind.MARKET_DAILY,
                priority=10,
                authority_score=0.92,
                freshness_hours=30.0,
                timeout_seconds=20.0,
                max_retries=2,
                quota_units=1.0,
                point_in_time=True,
                rate_limit_group="twelve_data",
                max_concurrency=8,
                application_daily_quota=20.0,
            ),
            ProviderCapability(
                provider="alpha_vantage_market",
                data_kind=DataKind.MARKET_DAILY,
                priority=20,
                authority_score=0.86,
                freshness_hours=30.0,
                timeout_seconds=20.0,
                max_retries=1,
                quota_units=1.0,
                point_in_time=True,
                rate_limit_group="alpha_vantage",
                min_interval_seconds=15.0,
                max_concurrency=1,
                application_daily_quota=15.0,
            ),
            ProviderCapability(
                provider="local_market_cache",
                data_kind=DataKind.MARKET_DAILY,
                priority=100,
                authority_score=0.72,
                freshness_hours=48.0,
                timeout_seconds=1.0,
                max_retries=0,
                quota_units=0.0,
                point_in_time=True,
                rate_limit_group="local_cache",
                max_concurrency=16,
                supports_live=False,
                is_cache=True,
            ),
            ProviderCapability(
                provider="alpha_vantage",
                data_kind=DataKind.NEWS,
                priority=10,
                authority_score=0.78,
                freshness_hours=6.0,
                timeout_seconds=20.0,
                max_retries=1,
                quota_units=1.0,
                point_in_time=True,
                rate_limit_group="alpha_vantage",
                min_interval_seconds=15.0,
                max_concurrency=1,
                application_daily_quota=15.0,
            ),
            ProviderCapability(
                provider="local_news_cache",
                data_kind=DataKind.NEWS,
                priority=100,
                authority_score=0.62,
                freshness_hours=48.0,
                timeout_seconds=1.0,
                max_retries=0,
                quota_units=0.0,
                point_in_time=True,
                supports_live=False,
                is_cache=True,
            ),
            ProviderCapability(
                provider="fred",
                data_kind=DataKind.MACRO,
                priority=10,
                authority_score=0.98,
                freshness_hours=1080.0,
                timeout_seconds=20.0,
                max_retries=2,
                quota_units=1.0,
                point_in_time=True,
                rate_limit_group="fred",
                min_interval_seconds=1.0,
                max_concurrency=1,
                application_daily_quota=8.0,
            ),
            ProviderCapability(
                provider="local_macro_cache",
                data_kind=DataKind.MACRO,
                priority=100,
                authority_score=0.82,
                freshness_hours=1440.0,
                timeout_seconds=1.0,
                max_retries=0,
                quota_units=0.0,
                point_in_time=True,
                supports_live=False,
                is_cache=True,
            ),
            ProviderCapability(
                provider="sec_edgar",
                data_kind=DataKind.FUNDAMENTALS,
                priority=10,
                authority_score=1.0,
                freshness_hours=2880.0,
                timeout_seconds=20.0,
                max_retries=2,
                quota_units=2.0,
                point_in_time=True,
                rate_limit_group="sec_edgar",
                min_interval_seconds=0.12,
                max_concurrency=1,
                application_daily_quota=20.0,
            ),
            ProviderCapability(
                provider="local_fundamentals_cache",
                data_kind=DataKind.FUNDAMENTALS,
                priority=100,
                authority_score=0.88,
                freshness_hours=4320.0,
                timeout_seconds=1.0,
                max_retries=0,
                quota_units=0.0,
                point_in_time=True,
                supports_live=False,
                is_cache=True,
            ),
        )
    )


__all__ = [
    "DataKind",
    "ProviderCapability",
    "ProviderRegistry",
    "default_provider_registry",
]
