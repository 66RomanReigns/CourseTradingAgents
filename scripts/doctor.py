from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tradinglab_agents.agents.llm import build_llm_client
from tradinglab_agents.config import load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.workflows.daily import DailyWorkflow


def _check_import(name: str) -> dict:
    try:
        module = importlib.import_module(name)
        return {"ok": True, "version": getattr(module, "__version__", "unknown")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _secret_file_status() -> dict:
    path = Path(
        os.environ.get(
            "TRADINGLAB_KEYS_FILE",
            Path.home() / ".config" / "tradinglab" / "tradinglab.keys",
        )
    )
    required = {
        "TWELVE_DATA_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "FRED_API_KEY",
        "GOOGLE_API_KEY",
        "ZHIPU_API_KEY",
        "TRADINGLAB_API_TOKEN",
    }
    if not path.is_file():
        return {
            "ok": False,
            "severity": "warning",
            "path": str(path),
            "detail": "external secret file is not configured",
        }
    permissions = f"{path.stat().st_mode & 0o777:03o}"
    configured: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if value.strip().strip("'\""):
            configured.add(name.strip())
    missing = sorted(required.difference(configured))
    return {
        "ok": permissions == "600" and not missing,
        "severity": "warning",
        "path": str(path),
        "permissions": permissions,
        "configured_names": sorted(required.intersection(configured)),
        "missing_names": missing,
        "values_exposed": False,
    }


def _docker_status() -> dict:
    if not shutil.which("docker"):
        return {"ok": False, "severity": "warning", "detail": "docker command not found"}
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        info = json.loads(result.stdout)
    except Exception as exc:
        return {"ok": False, "severity": "warning", "detail": str(exc)}
    proxy = info.get("HttpProxy") or info.get("HTTPProxy")
    if proxy and "127.0.0.1:17890" in proxy:
        return {
            "ok": False,
            "severity": "warning",
            "detail": "Docker daemon uses inactive-looking proxy 127.0.0.1:17890",
            "proxy": proxy,
        }
    return {"ok": True, "proxy": proxy}


def run_doctor() -> dict:
    checks: dict[str, dict] = {}
    checks["python"] = {
        "ok": sys.version_info[:2] == (3, 11),
        "version": sys.version.replace("\n", " "),
        "executable": sys.executable,
        "required": "3.11.x",
    }
    for name in (
        "yaml",
        "pydantic",
        "fastapi",
        "uvicorn",
        "httpx",
        "openai",
        "pytest",
    ):
        checks[f"import:{name}"] = _check_import(name)

    checks["external_secrets"] = _secret_file_status()

    lock_path = ROOT / "requirements.lock"
    checks["dependency_lock"] = {
        "ok": lock_path.is_file(),
        "path": str(lock_path),
    }

    try:
        settings = load_settings(ROOT / "config/default.yaml")
        checks["config"] = {"ok": True, "settings": settings.__dict__}
        llm = build_llm_client(settings, ROOT)
        checks["llm_execution"] = {
            "ok": settings.llm_execution_mode != "live",
            "execution_mode": settings.llm_execution_mode,
            "identity": llm.identity,
            "external_request": False,
        }
        workflow_plan = DailyWorkflow(settings, ROOT).plan()
        checks["workflow_plan"] = {
            "ok": not workflow_plan["external_requests_enabled"],
            "mode": workflow_plan["mode"],
            "planned_external_requests": workflow_plan["planned_external_requests"],
            "planned_llm_calls": workflow_plan["remote_llm"]["planned_calls"],
            "max_llm_calls": workflow_plan["remote_llm"]["max_calls_per_run"],
        }
    except Exception as exc:
        checks["config"] = {"ok": False, "error": str(exc)}

    try:
        prices = LocalCsvProvider(ROOT / "data/sample/demo.csv", "DEMO")
        news = LocalNewsProvider(ROOT / "data/sample/demo_news.jsonl")
        checks["sample_data"] = {
            "ok": len(prices.bars) >= 100,
            "price_rows": len(prices.bars),
            "news_rows": len(news.events),
        }
    except Exception as exc:
        checks["sample_data"] = {"ok": False, "error": str(exc)}

    artifacts = ROOT / "artifacts"
    try:
        artifacts.mkdir(exist_ok=True)
        probe = artifacts / ".doctor-write-test"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
        checks["artifacts_writable"] = {"ok": True, "path": str(artifacts)}
    except Exception as exc:
        checks["artifacts_writable"] = {"ok": False, "error": str(exc)}

    try:
        with tempfile.TemporaryDirectory(dir=artifacts) as directory:
            store = PaperTradingStore(Path(directory) / "paper-doctor.db")
            store.create_account(
                "doctor",
                name="Doctor Probe",
                initial_cash=1000.0,
                symbols=("AAA", "BBB"),
            )
            restored = store.require_account("doctor")
            checks["paper_sqlite"] = {
                "ok": (
                    restored.cash == 1000.0
                    and restored.symbols == ("AAA", "BBB")
                    and store.schema_version == store.CURRENT_SCHEMA_VERSION
                ),
                "journal": "WAL",
                "schema_version": store.schema_version,
                "supported_schema_version": store.CURRENT_SCHEMA_VERSION,
            }
    except Exception as exc:
        checks["paper_sqlite"] = {"ok": False, "error": str(exc)}

    checks["docker"] = _docker_status()
    critical_failures = [
        name
        for name, check in checks.items()
        if not check.get("ok") and check.get("severity", "critical") == "critical"
    ]
    return {
        "project_root": str(ROOT),
        "environment": {"TRADINGLAB_ROOT": os.environ.get("TRADINGLAB_ROOT")},
        "ok": not critical_failures,
        "critical_failures": critical_failures,
        "checks": checks,
    }


def main() -> None:
    result = run_doctor()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
