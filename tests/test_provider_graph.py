import os
import shutil
import tempfile
import threading
import time
import unittest
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tradinglab_agents.config import load_settings
from tradinglab_agents.data.fallback_router import (
    ProviderFallbackRouter,
    ProviderRequest,
)
from tradinglab_agents.data.http_client import DataApiError
from tradinglab_agents.data.provider_quality import (
    ProviderCandidate,
    QualityGateStatus,
)
from tradinglab_agents.data.provider_registry import (
    DataKind,
    ProviderCapability,
    ProviderRegistry,
)
from tradinglab_agents.storage.provider_health import (
    ProviderHealthStatus,
    ProviderHealthStore,
)
from tradinglab_agents.storage.provider_usage import (
    ProviderCallTracker,
    ProviderState,
    ProviderUsageStore,
)
from tradinglab_agents.workflows.data_provider_graph import (
    DataProviderGraphExecution,
    LangGraphDataProviderRuntime,
)
from tradinglab_agents.workflows.daily import DailyWorkflow


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "config" / "default.yaml")


def _capability(
    provider: str,
    priority: int,
    *,
    authority: float = 0.9,
    is_cache: bool = False,
) -> ProviderCapability:
    return ProviderCapability(
        provider=provider,
        data_kind=DataKind.MARKET_DAILY,
        priority=priority,
        authority_score=authority,
        freshness_hours=48.0,
        timeout_seconds=2.0,
        max_retries=0,
        quota_units=0.0 if is_cache else 1.0,
        point_in_time=True,
        supports_live=not is_cache,
        is_cache=is_cache,
    )


def _candidate(provider: str, resource: str, close: float) -> ProviderCandidate:
    now = datetime.now(timezone.utc)
    return ProviderCandidate(
        provider=provider,
        data_kind=DataKind.MARKET_DAILY,
        resource=resource,
        records=(
            {
                "symbol": resource,
                "timestamp": "2026-07-24T16:00:00",
                "open_at": "2026-07-24T09:30:00",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000.0,
                "available_at": "2026-07-24T16:00:00",
            },
        ),
        observed_at=now,
        latest_data_at=now,
        completeness=1.0,
        metadata={"source": "test_fixture"},
    )


class ProviderFallbackRouterTest(unittest.TestCase):
    def test_rate_limited_primary_falls_back_and_blocks_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ProviderRegistry(
                (
                    _capability("primary", 10),
                    _capability("local_cache", 100, authority=0.72, is_cache=True),
                )
            )
            health = ProviderHealthStore(Path(directory) / "health.db")

            def primary(_request, _capability):
                raise DataApiError("HTTP 429 rate limit")

            def cache(request, _capability):
                return _candidate("local_cache", request.resource, 100.0)

            router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="fallback-run",
                fetchers={"primary": primary, "local_cache": cache},
            )
            result = router.route(
                ProviderRequest(
                    request_id="market.SPY",
                    data_kind=DataKind.MARKET_DAILY,
                    resource="SPY",
                )
            )

            self.assertEqual(result.selected.provider, "local_cache")
            self.assertEqual(result.fallback_depth, 1)
            self.assertEqual(result.quality.status, QualityGateStatus.DEGRADED)
            self.assertFalse(result.quality.allow_position_increase)
            self.assertIn("fallback_provider", result.quality.reasons)
            primary_health = health.get("primary", DataKind.MARKET_DAILY)
            self.assertEqual(primary_health.status, ProviderHealthStatus.RATE_LIMITED)
            self.assertFalse(primary_health.is_available)

    def test_shared_daily_quota_blocks_before_fetch_and_uses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = ProviderUsageStore(root / "usage.db")
            prior = ProviderCallTracker(usage, "prior-run")
            prior.record_state(
                provider="alpha_news",
                operation="news",
                resource="SPY",
                status=ProviderState.LIVE_OK,
                units=1.0,
            )
            registry = ProviderRegistry(
                (
                    ProviderCapability(
                        provider="alpha_market",
                        data_kind=DataKind.MARKET_DAILY,
                        priority=10,
                        authority_score=0.86,
                        freshness_hours=48.0,
                        timeout_seconds=2.0,
                        max_retries=0,
                        quota_units=1.0,
                        point_in_time=True,
                        rate_limit_group="alpha",
                        application_daily_quota=1.0,
                    ),
                    ProviderCapability(
                        provider="alpha_news",
                        data_kind=DataKind.NEWS,
                        priority=10,
                        authority_score=0.78,
                        freshness_hours=6.0,
                        timeout_seconds=2.0,
                        max_retries=0,
                        quota_units=1.0,
                        point_in_time=True,
                        rate_limit_group="alpha",
                        application_daily_quota=1.0,
                    ),
                    ProviderCapability(
                        provider="local_cache",
                        data_kind=DataKind.MARKET_DAILY,
                        priority=100,
                        authority_score=0.72,
                        freshness_hours=48.0,
                        timeout_seconds=1.0,
                        max_retries=0,
                        quota_units=0.0,
                        point_in_time=True,
                        is_cache=True,
                        supports_live=False,
                    ),
                )
            )
            calls: Counter[str] = Counter()

            def market(request, _capability):
                calls["market"] += 1
                return _candidate("alpha_market", request.resource, 100.0)

            def cache(request, _capability):
                calls["cache"] += 1
                return _candidate("local_cache", request.resource, 100.0)

            health = ProviderHealthStore(root / "health.db")
            router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="quota-run",
                fetchers={"alpha_market": market, "local_cache": cache},
                tracker=ProviderCallTracker(usage, "quota-run"),
            )
            result = router.route(
                ProviderRequest(
                    "market.SPY",
                    DataKind.MARKET_DAILY,
                    "SPY",
                )
            )
            self.assertEqual(calls["market"], 0)
            self.assertEqual(calls["cache"], 1)
            self.assertEqual(result.selected.provider, "local_cache")
            self.assertFalse(result.quality.allow_position_increase)
            first_attempt = result.attempts[0]
            self.assertEqual(first_attempt.status, "SKIPPED")
            self.assertEqual(
                first_attempt.skipped_reason,
                "application_daily_quota_exhausted",
            )
            snapshot = health.get("alpha_market", DataKind.MARKET_DAILY)
            self.assertEqual(snapshot.status, ProviderHealthStatus.RATE_LIMITED)
            self.assertFalse(snapshot.is_available)
            self.assertIsNotNone(snapshot.cooldown_until_utc)
            usage_rows = usage.summary(run_id="quota-run")["providers"]
            quota_row = next(
                item
                for item in usage_rows
                if item["provider"] == "alpha_market"
            )
            self.assertEqual(quota_row["status"], "RATE_LIMITED")
            self.assertEqual(quota_row["units"], 0.0)

    def test_shared_provider_group_enforces_minimum_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            capability = ProviderCapability(
                provider="paced",
                data_kind=DataKind.MARKET_DAILY,
                priority=10,
                authority_score=0.9,
                freshness_hours=48.0,
                timeout_seconds=2.0,
                max_retries=0,
                quota_units=1.0,
                point_in_time=True,
                rate_limit_group="shared",
                min_interval_seconds=0.05,
                max_concurrency=1,
            )
            starts: list[float] = []
            start_lock = threading.Lock()
            barrier = threading.Barrier(2)

            def fetch(request, _capability):
                with start_lock:
                    starts.append(time.monotonic())
                return _candidate("paced", request.resource, 100.0)

            router = ProviderFallbackRouter(
                registry=ProviderRegistry((capability,)),
                health_store=ProviderHealthStore(Path(directory) / "health.db"),
                run_id="paced-run",
                fetchers={"paced": fetch},
            )
            errors: list[Exception] = []

            def run(symbol: str) -> None:
                try:
                    barrier.wait()
                    router.route(
                        ProviderRequest(
                            f"market.{symbol}",
                            DataKind.MARKET_DAILY,
                            symbol,
                        )
                    )
                except Exception as exc:  # pragma: no cover - diagnostic
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(symbol,))
                for symbol in ("SPY", "QQQ")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(starts), 2)
            self.assertGreaterEqual(abs(starts[1] - starts[0]), 0.045)

    def test_transient_offline_state_recovers_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            health = ProviderHealthStore(Path(directory) / "health.db")
            health.record_failure(
                run_id="failure",
                provider="primary",
                data_kind=DataKind.MARKET_DAILY,
                provider_state=ProviderState.NETWORK_BLOCKED,
                reason="network unavailable",
            )
            snapshot = health.get("primary", DataKind.MARKET_DAILY)
            self.assertEqual(snapshot.status, ProviderHealthStatus.OFFLINE)
            self.assertFalse(snapshot.is_available)
            recovered = health.record_success(
                run_id="recovered",
                provider="primary",
                data_kind=DataKind.MARKET_DAILY,
                latency_ms=10.0,
                quality_score=0.9,
                latency_threshold_ms=1000.0,
            )
            self.assertEqual(recovered.status, ProviderHealthStatus.HEALTHY)
            self.assertTrue(recovered.is_available)


class DataProviderGraphTest(unittest.TestCase):
    def test_major_conflict_blocks_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ProviderRegistry(
                (
                    _capability("primary", 10),
                    _capability("secondary", 20, authority=0.85),
                )
            )
            health = ProviderHealthStore(root / "health.db")
            router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="conflict-run",
                fetchers={
                    "primary": lambda request, _capability: _candidate(
                        "primary", request.resource, 100.0
                    ),
                    "secondary": lambda request, _capability: _candidate(
                        "secondary", request.resource, 110.0
                    ),
                },
                conflict_warn_relative_difference=0.005,
                conflict_block_relative_difference=0.02,
            )
            persisted: list[dict] = []
            runtime = LangGraphDataProviderRuntime(root / "events.jsonl")
            with self.assertRaisesRegex(ValueError, "blocked persistence"):
                runtime.run(
                    thread_id="conflict:DATA_PROVIDER",
                    requests=(
                        ProviderRequest(
                            request_id="market.SPY",
                            data_kind=DataKind.MARKET_DAILY,
                            resource="SPY",
                            shadow_validate=True,
                        ),
                    ),
                    router=router,
                    persist_result=lambda item: persisted.append(item) or item,
                    checkpointer_path=root / "provider.db",
                )
            self.assertEqual(persisted, [])
            self.assertEqual(
                health.get("primary", DataKind.MARKET_DAILY).status,
                ProviderHealthStatus.DATA_CONFLICT,
            )
            self.assertEqual(
                health.get("secondary", DataKind.MARKET_DAILY).status,
                ProviderHealthStatus.DATA_CONFLICT,
            )

    def test_resume_reruns_only_failed_dynamic_resource(self):
        counts: Counter[str] = Counter()
        fail_once = {"QQQ": True}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ProviderRegistry((_capability("primary", 10),))
            health = ProviderHealthStore(root / "health.db")

            def fetch(request, _capability):
                counts[request.resource] += 1
                if request.resource == "QQQ" and fail_once["QQQ"]:
                    fail_once["QQQ"] = False
                    raise RuntimeError("injected QQQ provider failure")
                return _candidate("primary", request.resource, 100.0)

            router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="resume-run",
                fetchers={"primary": fetch},
            )
            runtime = LangGraphDataProviderRuntime(root / "events.jsonl")
            requests = (
                ProviderRequest("market.SPY", DataKind.MARKET_DAILY, "SPY"),
                ProviderRequest("market.QQQ", DataKind.MARKET_DAILY, "QQQ"),
            )
            arguments = dict(
                thread_id="resume:DATA_PROVIDER",
                requests=requests,
                router=router,
                persist_result=lambda item: {
                    "request_id": item["request"]["request_id"]
                },
                checkpointer_path=root / "provider.db",
            )
            with self.assertRaisesRegex(RuntimeError, "injected QQQ"):
                runtime.run(**arguments)
            health.record_success(
                run_id="manual-recovery",
                provider="primary",
                data_kind=DataKind.MARKET_DAILY,
                latency_ms=1.0,
                quality_score=0.9,
                latency_threshold_ms=1000.0,
            )
            resumed = runtime.run(**arguments, resume=True)
            self.assertEqual(counts["SPY"], 1)
            self.assertEqual(counts["QQQ"], 2)
            self.assertEqual(len(resumed.persisted_results), 2)
            self.assertEqual(resumed.quality_summary["status"], "NORMAL")

    def test_resume_rejects_changed_router_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ProviderRegistry((_capability("primary", 10),))
            health = ProviderHealthStore(root / "health.db")
            request = ProviderRequest(
                "market.SPY",
                DataKind.MARKET_DAILY,
                "SPY",
            )
            runtime = LangGraphDataProviderRuntime()
            router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="policy-run",
                fetchers={
                    "primary": lambda item, _capability: _candidate(
                        "primary", item.resource, 100.0
                    )
                },
                minimum_quality_score=0.75,
            )
            common = dict(
                thread_id="policy:DATA_PROVIDER",
                requests=(request,),
                persist_result=lambda item: item["request"],
                checkpointer_path=root / "provider.db",
            )
            runtime.run(router=router, **common)
            changed = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="policy-run",
                fetchers={
                    "primary": lambda item, _capability: _candidate(
                        "primary", item.resource, 100.0
                    )
                },
                minimum_quality_score=0.80,
            )
            with self.assertRaisesRegex(ValueError, "resume input mismatch"):
                runtime.run(router=changed, resume=True, **common)


class ProviderGraphWorkflowIntegrationTest(unittest.TestCase):
    def test_degraded_provider_graph_flows_through_workflow_core_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "live"
            live.mkdir()
            for symbol in ("SPY", "QQQ"):
                shutil.copy2(
                    ROOT / f"data/multi_sample/{symbol}.csv",
                    live / f"{symbol}.csv",
                )
                shutil.copy2(
                    ROOT / f"data/multi_sample/{symbol}_actions.jsonl",
                    live / f"{symbol}_actions.jsonl",
                )
            settings = replace(
                SETTINGS,
                workflow_symbols=("SPY", "QQQ"),
                workflow_artifact_dir=str(root / "workflows"),
                workflow_live_data_dir=str(live),
                workflow_provider_usage_database=str(root / "usage.db"),
                workflow_provider_health_database=str(root / "health.db"),
                workflow_provider_checkpoint_database=str(root / "provider.db"),
                workflow_core_checkpoint_database=str(root / "core.db"),
                workflow_langgraph_event_path=str(root / "events.jsonl"),
                workflow_network_preflight=False,
                workflow_refresh_market=True,
                workflow_refresh_news=False,
                workflow_refresh_macro=False,
                workflow_refresh_fundamentals=False,
                workflow_run_research=False,
                workflow_run_paper=False,
            )
            quality = {
                "status": "DEGRADED",
                "request_count": 2,
                "minimum_score": 0.62,
                "mean_score": 0.68,
                "allow_position_increase": False,
                "requires_human_review": True,
                "blocked_resources": [],
                "degraded_resources": ["SPY", "QQQ"],
                "assessments": [],
            }
            execution = DataProviderGraphExecution(
                thread_id="integration:DATA_PROVIDER",
                input_sha256="a" * 64,
                route_results=(),
                persisted_results=(
                    {
                        "request_id": "market.SPY",
                        "provider": "local_market_cache",
                        "path": str(live / "SPY.csv"),
                    },
                    {
                        "request_id": "market.QQQ",
                        "provider": "local_market_cache",
                        "path": str(live / "QQQ.csv"),
                    },
                ),
                quality_summary=quality,
                checkpoint_count=5,
                event_count=4,
                execution_path=(
                    "provider.prepare",
                    "provider.route.market.SPY",
                    "provider.route.market.QQQ",
                    "provider.persist_and_quality_gate",
                ),
                latest_state={},
            )
            workflow = DailyWorkflow(settings, ROOT)
            with patch.dict(os.environ, {"ZHIPU_API_KEY": "placeholder"}), patch.object(
                workflow,
                "_run_data_provider_graph",
                return_value=execution,
            ) as graph:
                result = workflow.execute(
                    mode="live",
                    confirm_live=True,
                    run_id="provider-integration",
                )
            graph.assert_called_once()
            self.assertEqual(result["data_provider"]["quality"]["status"], "DEGRADED")
            self.assertEqual(
                result["data_validation"]["provider_quality"]["status"],
                "DEGRADED",
            )
            decision_quality = result["decision_preparation"]["provider_quality"]
            self.assertEqual(decision_quality["status"], "DEGRADED")
            self.assertFalse(decision_quality["allow_position_increase"])
            self.assertTrue(decision_quality["non_expansion_gate_applied"])
            self.assertTrue(result["decision_preparation"]["safe_for_paper_input"])
            self.assertEqual(result["research"]["status"], "disabled")
            self.assertEqual(result["paper"]["status"], "disabled")
            self.assertFalse(result["workflow_core"]["external_broker"])


class DataQualityOverlayTest(unittest.TestCase):
    @staticmethod
    def _research_result() -> dict:
        return {
            "candidates": {
                "SPY": {
                    "clients": {},
                    "result": {
                        "news": None,
                        "macro": None,
                        "fundamental": None,
                        "debate_rounds": [],
                        "manager": None,
                        "preliminary_trader": None,
                        "risk_reviews": [],
                        "trader": {
                            "action": "BUY",
                            "confidence": 0.90,
                            "target_weight": 0.15,
                            "order_type": "MARKET_NEXT_OPEN",
                            "rationale": "positive setup",
                            "evidence_ids": [],
                            "requires_human_approval": True,
                        },
                    },
                }
            }
        }

    def test_degraded_quality_converts_buy_to_hold(self):
        workflow = DailyWorkflow(SETTINGS, ROOT)
        quality = {
            "status": "DEGRADED",
            "minimum_score": 0.62,
            "mean_score": 0.68,
            "allow_position_increase": False,
            "requires_human_review": True,
            "degraded_resources": ["SPY"],
            "blocked_resources": [],
        }
        overlays = workflow._assemble_research_overlays(
            self._research_result(),
            quality,
        )
        overlay = overlays["SPY"]
        self.assertEqual(overlay["action"], "HOLD")
        self.assertEqual(overlay["target_weight"], 0.0)
        self.assertEqual(overlay["order_type"], "NO_ORDER")
        gate = overlay["trace"]["data_quality_gate"]
        self.assertTrue(gate["enforced_hold"])
        self.assertEqual(gate["original_action"], "BUY")
        self.assertTrue(overlay["trace"]["non_expansion_verified"])

        preparation = workflow._prepare_decision_input(
            {"status": "completed"},
            self._research_result(),
            overlays,
            quality,
        )
        self.assertTrue(preparation["safe_for_paper_input"])
        self.assertEqual(preparation["action_counts"]["HOLD"], 1)
        self.assertTrue(
            preparation["provider_quality"]["non_expansion_gate_applied"]
        )

    def test_blocked_quality_cannot_enter_overlay_assembly(self):
        workflow = DailyWorkflow(SETTINGS, ROOT)
        with self.assertRaisesRegex(ValueError, "blocked provider quality"):
            workflow._assemble_research_overlays(
                self._research_result(),
                {
                    "status": "BLOCKED",
                    "allow_position_increase": False,
                },
            )


if __name__ == "__main__":
    unittest.main()
