import tempfile
import unittest
from pathlib import Path

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.evaluation.audit import audit_experiment
from tradinglab_agents.evaluation.benchmark_suite import run_scenario_benchmark
from tradinglab_agents.evaluation.experiments import run_experiment_suite, save_run_bundle
from tradinglab_agents.reporting.provenance import build_manifest, sha256_file
from tradinglab_agents.storage.run_store import RunStore


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTest(unittest.TestCase):
    def test_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            BacktestSettings(initial_cash=0)
        with self.assertRaises(ValueError):
            BacktestSettings(max_position_weight=1.2)
        with self.assertRaises(ValueError):
            BacktestSettings(buy_threshold=-0.1)


class ProvenanceAndAuditTest(unittest.TestCase):
    def setUp(self):
        self.provider = LocalCsvProvider(ROOT / "data/sample/demo.csv", "DEMO")
        self.news = LocalNewsProvider(ROOT / "data/sample/demo_news.jsonl")
        self.settings = BacktestSettings()

    def test_experiment_has_regime_metrics_and_passes_audit(self):
        result = run_experiment_suite(self.provider, self.settings, self.news)
        self.assertTrue(audit_experiment(result)["passed"])
        full = next(item for item in result["variants"] if item["name"] == "full_agent")
        self.assertEqual(set(full["regime_metrics"]), {"bull", "bear", "sideways", "volatile"})
        self.assertTrue(all("available_evidence_ids" in row for row in full["decisions"]))

    def test_manifest_hashes_input_and_bundle_is_complete(self):
        price_path = ROOT / "data/sample/demo.csv"
        manifest = build_manifest(ROOT, self.settings, [price_path], "test")
        self.assertEqual(manifest["inputs"][0]["sha256"], sha256_file(price_path))
        result = run_experiment_suite(self.provider, self.settings, self.news)
        with tempfile.TemporaryDirectory() as temp:
            bundle = save_run_bundle(
                result,
                project_root=ROOT,
                settings=self.settings,
                input_files=[price_path, ROOT / "data/sample/demo_news.jsonl"],
                artifacts_root=Path(temp) / "runs",
            )
            for key in ("json", "markdown", "html", "manifest", "audit"):
                self.assertTrue(Path(bundle[key]).exists())
            html = Path(bundle["html"]).read_text(encoding="utf-8")
            self.assertIn("TradeLab-Agent 实验报告", html)

    def test_sqlite_run_store_round_trip(self):
        result = run_experiment_suite(self.provider, self.settings, self.news)
        with tempfile.TemporaryDirectory() as temp:
            manifest = build_manifest(
                ROOT,
                self.settings,
                [ROOT / "data/sample/demo.csv"],
                "test_store",
            )
            result["manifest"] = manifest
            result["audit"] = audit_experiment(result)
            store = RunStore(Path(temp) / "runs.db")
            run_id = store.save_experiment(result)
            self.assertEqual(store.list_runs(1)[0]["run_id"], run_id)
            restored = store.get_run(run_id)
            self.assertEqual(restored["symbol"], "DEMO")
            self.assertEqual(len(restored["summary"]), len(result["summary"]))


class ScenarioBenchmarkTest(unittest.TestCase):
    def test_scenario_benchmark_runs_all_regimes(self):
        result = run_scenario_benchmark(ROOT / "data/scenarios", BacktestSettings())
        self.assertEqual(result["scenario_count"], 4)
        self.assertTrue(result["all_audits_passed"])
        names = {row["name"] for row in result["aggregate"]}
        self.assertIn("full_agent", names)
        self.assertIn("buy_and_hold", names)


if __name__ == "__main__":
    unittest.main()
