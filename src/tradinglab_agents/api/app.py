from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from tradinglab_agents import __version__
from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.experiments import run_experiment_suite, save_run_bundle
from tradinglab_agents.storage.run_store import RunStore

PROJECT_ROOT = Path(os.environ.get("TRADINGLAB_ROOT", Path.cwd())).resolve()
DATABASE_PATH = PROJECT_ROOT / "artifacts/tradinglab.db"
app = FastAPI(
    title="TradeLab-Agent API",
    version=__version__,
    description="Course-oriented, paper-trading-only multi-agent service",
)


class RunRequest(BaseModel):
    csv_path: str = "data/sample/demo.csv"
    news_path: str | None = "data/sample/demo_news.jsonl"
    evidence_paths: list[str] = Field(default_factory=list)
    symbol: str = Field(default="DEMO", min_length=1, max_length=32)
    config_path: str | None = "config/default.yaml"
    initial_cash: float | None = Field(default=None, gt=0)
    persist: bool = False


def _safe_path(value: str | None, required: bool = True) -> Path | None:
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail="required path is missing")
        return None
    candidate = Path(value)
    resolved = (PROJECT_ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="path must stay inside project root") from exc
    if required and not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {value}")
    if not required and not resolved.exists():
        return None
    return resolved


def _components(request: RunRequest):
    csv_path = _safe_path(request.csv_path)
    news_path = _safe_path(request.news_path, required=False)
    config_path = _safe_path(request.config_path, required=False)
    evidence_paths = [_safe_path(value) for value in request.evidence_paths]
    settings = load_settings(config_path) if config_path else BacktestSettings()
    if request.initial_cash is not None:
        settings = replace(settings, initial_cash=request.initial_cash)
    prices = LocalCsvProvider(csv_path, request.symbol)
    news = LocalNewsProvider(news_path) if news_path else None
    evidence = [LocalPointInTimeEvidenceProvider(path) for path in evidence_paths]
    inputs = [csv_path, *evidence_paths]
    if news_path:
        inputs.append(news_path)
    if config_path:
        inputs.append(config_path)
    return prices, news, evidence, settings, inputs


@app.get("/")
def root() -> dict:
    return {
        "service": "TradeLab-Agent",
        "version": app.version,
        "mode": "paper-trading-only",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": app.version,
        "mode": "paper-trading-only",
        "project_root": str(PROJECT_ROOT),
        "database": str(DATABASE_PATH),
    }


@app.post("/backtest")
def backtest(request: RunRequest) -> dict:
    try:
        prices, news, evidence, settings, _ = _components(request)
        result = BacktestEngine(settings).run(
            prices, news, evidence_providers=evidence
        )
        return {
            "name": result["name"],
            "symbol": result["symbol"],
            "metrics": result["metrics"],
            "positions": result["positions"],
            "trade_count": len(result["fills"]),
            "decision_count": len(result["decisions"]),
        }
    except HTTPException:
        raise
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/experiments")
def experiments(request: RunRequest) -> dict:
    try:
        prices, news, evidence, settings, input_files = _components(request)
        result = run_experiment_suite(prices, settings, news, evidence)
        response = {
            "symbol": result["symbol"],
            "summary": result["summary"],
            "audit": result["audit"],
        }
        if request.persist:
            bundle = save_run_bundle(
                result,
                project_root=PROJECT_ROOT,
                settings=settings,
                input_files=input_files,
            )
            RunStore(DATABASE_PATH).save_experiment(result)
            response["run_id"] = bundle["run_id"]
            response["report_path"] = bundle["html"]
        return response
    except HTTPException:
        raise
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/runs")
def runs(limit: int = Query(default=20, ge=1, le=200)) -> dict:
    return {"runs": RunStore(DATABASE_PATH).list_runs(limit)}


@app.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    result = RunStore(DATABASE_PATH).get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    return result


@app.get("/reports/latest", response_class=FileResponse)
def latest_report():
    report = PROJECT_ROOT / "artifacts/latest/report.html"
    if not report.is_file():
        raise HTTPException(status_code=404, detail="no persisted report is available")
    return FileResponse(report, media_type="text/html", filename="tradinglab-latest.html")
