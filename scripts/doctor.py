from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tradinglab_agents.config import load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider


def _check_import(name: str) -> dict:
    try:
        module = importlib.import_module(name)
        return {"ok": True, "version": getattr(module, "__version__", "unknown")}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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
        "ok": sys.version_info >= (3, 10),
        "version": sys.version.replace("\n", " "),
        "executable": sys.executable,
    }
    for name in ("yaml", "fastapi", "uvicorn"):
        checks[f"import:{name}"] = _check_import(name)

    try:
        settings = load_settings(ROOT / "config/default.yaml")
        checks["config"] = {"ok": True, "settings": settings.__dict__}
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
