from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from tradinglab_agents.agents.llm import build_llm_client
from tradinglab_agents.agents.schemas import TradePlan
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.connectivity import probe_external_access, require_external_access
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.sec_edgar import SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.storage.provider_usage import (
    ProviderCallTracker,
    ProviderState,
    ProviderUsageStore,
)


class ProviderSmokeRunner:
    """Minimal, quota-conscious validation for each configured external service."""

    PROVIDERS = (
        "alpha_vantage",
        "sec_edgar",
        "deepseek",
    )
    LEGACY_PROVIDERS = ("twelve_data", "fred", "zhipu")

    def __init__(self, settings: BacktestSettings, project_root: str | Path):
        self.settings = settings
        self.root = Path(project_root).resolve()

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def run(
        self,
        *,
        provider: str = "all",
        symbol: str = "AAPL",
        confirm_live: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        selected = provider.lower().strip()
        if selected not in {*self.PROVIDERS, *self.LEGACY_PROVIDERS, "all"}:
            raise ValueError(
                "provider must be one of: all, " + ", ".join(self.PROVIDERS)
            )
        if not confirm_live:
            raise ValueError("provider smoke test requires confirm_live=True")

        resolved_run_id = run_id or f"smoke-{uuid4().hex}"
        usage_store = ProviderUsageStore(
            self._resolve(self.settings.workflow_provider_usage_database)
        )
        tracker = ProviderCallTracker(usage_store, resolved_run_id)

        network_started = time.perf_counter()
        network = probe_external_access(
            self.settings.workflow_connectivity_probe_url,
            timeout_seconds=min(10.0, self.settings.llm_timeout_seconds),
        )
        tracker.record_state(
            provider="network",
            operation="connectivity_preflight",
            resource="external_https",
            status=(
                ProviderState.LIVE_OK if network.ok else ProviderState.NETWORK_BLOCKED
            ),
            duration_ms=round((time.perf_counter() - network_started) * 1000, 3),
            detail={
                "state": network.state.value,
                "http_status": network.http_status,
            },
            error=None if network.ok else network.detail,
        )
        require_external_access(network)

        targets = self.PROVIDERS if selected == "all" else (selected,)
        normalized_symbol = symbol.upper()
        results: dict[str, Any] = {}
        for item in targets:
            if item == "twelve_data":
                bars = tracker.call(
                    provider=item,
                    operation="time_series_1day_smoke",
                    resource=normalized_symbol,
                    units=1,
                    function=lambda: TwelveDataClient().fetch_daily_bars(
                        normalized_symbol,
                        outputsize=2,
                        cache_ttl_seconds=0,
                        force_refresh=True,
                    ),
                )
                results[item] = {
                    "status": "LIVE_OK",
                    "rows": len(bars),
                    "first_session": bars[0].timestamp.date().isoformat(),
                    "last_session": bars[-1].timestamp.date().isoformat(),
                }
            elif item == "alpha_vantage":
                events = tracker.call(
                    provider=item,
                    operation="news_sentiment_smoke",
                    resource=normalized_symbol,
                    units=1,
                    function=lambda: AlphaVantageNewsClient().fetch_news(
                        normalized_symbol,
                        limit=1,
                        sort="LATEST",
                        cache_ttl_seconds=0,
                        force_refresh=True,
                    ),
                )
                results[item] = {
                    "status": "LIVE_OK",
                    "rows": len(events),
                    "latest_available_at": (
                        events[-1].available_at.isoformat() if events else None
                    ),
                }
            elif item == "fred":
                today = date.today()
                records = tracker.call(
                    provider=item,
                    operation="series_observations_smoke",
                    resource="DGS10",
                    units=1,
                    function=lambda today=today: FredClient().fetch_initial_release_records(
                        "DGS10",
                        observation_start=today - timedelta(days=21),
                        observation_end=today,
                        symbol="MACRO",
                        cache_ttl_seconds=0,
                        force_refresh=True,
                    ),
                )
                results[item] = {
                    "status": "LIVE_OK",
                    "rows": len(records),
                    "latest_observation": (
                        records[-1].timestamp.date().isoformat() if records else None
                    ),
                }
            elif item == "sec_edgar":
                records = tracker.call(
                    provider=item,
                    operation="company_facts_smoke",
                    resource=normalized_symbol,
                    units=2,
                    function=lambda: SecEdgarClient().fetch_fundamental_records(
                        normalized_symbol,
                        concepts=("NetIncomeLoss",),
                        cache_ttl_seconds=0,
                        force_refresh=True,
                    ),
                )
                results[item] = {
                    "status": "LIVE_OK",
                    "rows": len(records),
                    "latest_available_at": (
                        records[-1].available_at.isoformat() if records else None
                    ),
                }
            elif item in {"deepseek", "zhipu"}:
                live_settings = replace(
                    self.settings,
                    llm_execution_mode="live",
                    llm_cache_dir=f"artifacts/smoke_cache/{resolved_run_id}",
                )
                client = build_llm_client(live_settings, self.root)
                plan = tracker.call(
                    provider=item,
                    operation="structured_chat_smoke",
                    resource=self.settings.llm_model,
                    units=1,
                    function=lambda client=client: client.complete(
                        task="trader_plan",
                        system_prompt=(
                            "Return only a JSON object matching the supplied schema. "
                            "This is a connectivity test; choose HOLD and cite no evidence."
                        ),
                        payload={
                            "research_decision": {
                                "action": "HOLD",
                                "confidence": 0.5,
                                "target_weight": 0.0,
                                "thesis": "connectivity smoke test",
                                "evidence_ids": [],
                                "dissent": [],
                            }
                        },
                        response_model=TradePlan,
                    ),
                )
                results[item] = {
                    "status": "LIVE_OK",
                    "model": self.settings.llm_model,
                    "action": plan.action,
                    "schema_valid": True,
                }

        return {
            "run_id": resolved_run_id,
            "network": network.to_dict(),
            "results": results,
            "usage": usage_store.summary(run_id=resolved_run_id),
            "real_broker_connected": False,
        }


__all__ = ["ProviderSmokeRunner"]
