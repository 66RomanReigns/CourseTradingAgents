import json
import ssl
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tradinglab_agents.config import load_settings
from tradinglab_agents.data.connectivity import (
    ConnectivityResult,
    NetworkState,
    probe_external_access,
)
from tradinglab_agents.data.http_client import DataApiError
from tradinglab_agents.agents.schemas import TradePlan
from tradinglab_agents.storage.provider_usage import (
    ProviderCallTracker,
    ProviderState,
    ProviderUsageStore,
)
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.smoke import ProviderSmokeRunner
from tradinglab_agents.workflows.state import WorkflowExecutionError


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "config" / "default.yaml")


class _ProbeResponse:
    def __init__(self, *, status=200, url="", body=b"", headers=None):
        self.status = status
        self.url = url
        self.body = body
        self.headers = dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class ConnectivityPreflightTest(unittest.TestCase):
    def test_javascript_captive_portal_is_detected_without_tls_probe(self):
        portal = _ProbeResponse(
            status=200,
            url="http://connectivitycheck.gstatic.com/generate_204",
            body=(
                b'<html><script>location.href="https://p.nju.edu.cn/"</script>'
                b"Authentication is required</html>"
            ),
        )
        with patch(
            "tradinglab_agents.data.connectivity.urlopen",
            return_value=portal,
        ) as mocked:
            result = probe_external_access(timeout_seconds=1)
        self.assertEqual(result.state, NetworkState.CAPTIVE_PORTAL)
        self.assertFalse(result.ok)
        self.assertEqual(result.final_url, "https://p.nju.edu.cn/")
        self.assertEqual(mocked.call_count, 1)

    def test_verified_https_path_is_online(self):
        responses = [
            _ProbeResponse(
                status=204,
                url="http://connectivitycheck.gstatic.com/generate_204",
            ),
            _ProbeResponse(status=200, url="https://api.twelvedata.com/"),
        ]
        with patch(
            "tradinglab_agents.data.connectivity.urlopen",
            side_effect=responses,
        ):
            result = probe_external_access(timeout_seconds=1)
        self.assertEqual(result.state, NetworkState.ONLINE)
        self.assertTrue(result.ok)

    def test_tls_interception_is_distinct_from_offline(self):
        verification_error = ssl.SSLCertVerificationError(1, "self signed")
        with patch(
            "tradinglab_agents.data.connectivity.urlopen",
            side_effect=[
                _ProbeResponse(
                    status=204,
                    url="http://connectivitycheck.gstatic.com/generate_204",
                ),
                URLError(verification_error),
            ],
        ):
            result = probe_external_access(timeout_seconds=1)
        self.assertEqual(result.state, NetworkState.TLS_INTERCEPTED)
        self.assertFalse(result.ok)


class ProviderUsageLedgerTest(unittest.TestCase):
    def test_success_rate_limit_and_stale_degradation_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderUsageStore(Path(directory) / "usage.db")
            tracker = ProviderCallTracker(store, "run-1")
            result = tracker.call(
                provider="twelve_data",
                operation="time_series",
                resource="AAPL",
                units=1,
                function=lambda: [1, 2],
            )
            self.assertEqual(result, [1, 2])
            with self.assertRaises(DataApiError):
                tracker.call(
                    provider="alpha_vantage",
                    operation="news",
                    resource="AAPL",
                    units=1,
                    function=lambda: (_ for _ in ()).throw(
                        DataApiError("HTTP 429 rate limit")
                    ),
                )
            tracker.record_state(
                provider="alpha_vantage",
                operation="news",
                resource="AAPL",
                status=ProviderState.DEGRADED_STALE,
                detail={"rows": 10},
            )
            rows = store.summary(run_id="run-1")["providers"]
            states = {(row["provider"], row["status"]) for row in rows}
            self.assertIn(("twelve_data", "LIVE_OK"), states)
            self.assertIn(("alpha_vantage", "RATE_LIMITED"), states)
            self.assertIn(("alpha_vantage", "DEGRADED_STALE"), states)


class ProviderSmokeRunnerTest(unittest.TestCase):
    def test_smoke_requires_confirmation(self):
        with self.assertRaisesRegex(ValueError, "confirm_live"):
            ProviderSmokeRunner(SETTINGS, ROOT).run(provider="zhipu")

    def test_single_zhipu_smoke_is_schema_validated_and_tracked(self):
        online = ConnectivityResult(
            state=NetworkState.ONLINE,
            ok=True,
            probe_url="http://connectivitycheck.gstatic.com/generate_204",
            final_url="http://connectivitycheck.gstatic.com/generate_204",
            http_status=204,
            tls_url="https://api.twelvedata.com/",
            detail="verified",
        )

        class FakeLlm:
            def complete(self, **kwargs):
                return TradePlan(
                    action="HOLD",
                    confidence=0.5,
                    target_weight=0.0,
                    order_type="NO_ORDER",
                    rationale="smoke",
                    evidence_ids=(),
                    requires_human_approval=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                SETTINGS,
                workflow_provider_usage_database=str(Path(directory) / "usage.db"),
            )
            runner = ProviderSmokeRunner(settings, ROOT)
            with patch(
                "tradinglab_agents.workflows.smoke.probe_external_access",
                return_value=online,
            ), patch(
                "tradinglab_agents.workflows.smoke.build_llm_client",
                return_value=FakeLlm(),
            ):
                result = runner.run(
                    provider="zhipu",
                    confirm_live=True,
                    run_id="smoke-test",
                )
            self.assertEqual(result["results"]["zhipu"]["action"], "HOLD")
            self.assertTrue(result["results"]["zhipu"]["schema_valid"])
            usage = result["usage"]["providers"]
            zhipu = next(row for row in usage if row["provider"] == "zhipu")
            network = next(row for row in usage if row["provider"] == "network")
            self.assertEqual(zhipu["status"], "LIVE_OK")
            self.assertEqual(zhipu["units"], 1.0)
            self.assertEqual(network["status"], "LIVE_OK")
            self.assertEqual(network["units"], 0.0)


class LiveWorkflowGateTest(unittest.TestCase):
    def _settings(self, directory: str):
        return replace(
            SETTINGS,
            workflow_artifact_dir=str(Path(directory) / "workflows"),
            workflow_live_data_dir=str(Path(directory) / "real"),
            workflow_provider_usage_database=str(Path(directory) / "usage.db"),
            workflow_refresh_market=False,
            workflow_refresh_news=False,
            workflow_refresh_macro=False,
            workflow_refresh_fundamentals=False,
            workflow_run_research=False,
            workflow_run_paper=False,
        )

    def test_blocked_network_is_checkpointed_before_provider_calls(self):
        blocked = ConnectivityResult(
            state=NetworkState.CAPTIVE_PORTAL,
            ok=False,
            probe_url="http://connectivitycheck.gstatic.com/generate_204",
            final_url="https://p.nju.edu.cn/",
            http_status=200,
            tls_url="https://api.twelvedata.com/",
            detail="campus authentication required",
        )
        with tempfile.TemporaryDirectory() as directory:
            workflow = DailyWorkflow(self._settings(directory), ROOT)
            with patch.dict("os.environ", {"ZHIPU_API_KEY": "placeholder"}), patch(
                "tradinglab_agents.workflows.daily.probe_external_access",
                return_value=blocked,
            ), patch.object(
                workflow,
                "_refresh_market",
                side_effect=AssertionError("provider must not be called"),
            ):
                with self.assertRaises(WorkflowExecutionError):
                    workflow.execute(
                        mode="live",
                        confirm_live=True,
                        run_id="workflow-blocked",
                    )
            state_path = Path(directory) / "workflows/workflow-blocked/state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["failed_node"], "network_gate")
            self.assertIn("network_probe", state["completed_nodes"])
            rows = ProviderUsageStore(Path(directory) / "usage.db").summary(
                run_id="workflow-blocked"
            )["providers"]
            self.assertEqual(rows[0]["status"], "NETWORK_BLOCKED")

    def test_resume_recomputes_volatile_network_probe(self):
        blocked = ConnectivityResult(
            state=NetworkState.CAPTIVE_PORTAL,
            ok=False,
            probe_url="http://connectivitycheck.gstatic.com/generate_204",
            final_url="https://p.nju.edu.cn/",
            http_status=200,
            tls_url="https://api.twelvedata.com/",
            detail="campus authentication required",
        )
        online = ConnectivityResult(
            state=NetworkState.ONLINE,
            ok=True,
            probe_url="http://connectivitycheck.gstatic.com/generate_204",
            final_url="http://connectivitycheck.gstatic.com/generate_204",
            http_status=204,
            tls_url="https://api.twelvedata.com/",
            detail="verified",
        )
        with tempfile.TemporaryDirectory() as directory:
            workflow = DailyWorkflow(self._settings(directory), ROOT)
            with patch.dict("os.environ", {"ZHIPU_API_KEY": "placeholder"}), patch(
                "tradinglab_agents.workflows.daily.probe_external_access",
                return_value=blocked,
            ):
                with self.assertRaises(WorkflowExecutionError):
                    workflow.execute(
                        mode="live",
                        confirm_live=True,
                        run_id="workflow-resume",
                    )
            with patch.dict("os.environ", {"ZHIPU_API_KEY": "placeholder"}), patch(
                "tradinglab_agents.workflows.daily.probe_external_access",
                return_value=online,
            ):
                result = workflow.execute(
                    mode="live",
                    confirm_live=True,
                    run_id="workflow-resume",
                    resume=True,
                )
            self.assertEqual(result["network"]["result"]["state"], "ONLINE")
            self.assertEqual(result["provider_usage"]["run_id"], "workflow-resume")
            state = json.loads(
                (Path(directory) / "workflows/workflow-resume/state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["status"], "COMPLETE")

    def test_recent_last_known_good_is_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                self._settings(directory),
                workflow_provider_failure_policy="last_known_good",
            )
            workflow = DailyWorkflow(settings, ROOT)
            path = Path(directory) / "real/AAPL.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "timestamp,open_at,available_at,open,high,low,close,volume\n"
                "2026-07-17T16:00:00,2026-07-17T09:30:00,2026-07-17T16:00:00,1,1,1,1,1\n",
                encoding="utf-8",
            )
            store = ProviderUsageStore(Path(directory) / "usage.db")
            tracker = ProviderCallTracker(store, "run-fallback")
            result = workflow._last_known_good(
                path,
                tracker=tracker,
                provider="twelve_data",
                operation="time_series",
                resource="AAPL",
                exc=DataApiError("provider unavailable"),
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "DEGRADED_STALE")


if __name__ == "__main__":
    unittest.main()
