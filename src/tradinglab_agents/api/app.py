from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.experiments import run_experiment_suite

PROJECT_ROOT = Path(os.environ.get("TRADINGLAB_ROOT", Path.cwd())).resolve()
app = FastAPI(
    title="TradeLab-Agent API",
    version="0.2.0",
    description="Course-oriented, paper-trading-only multi-agent service",
)


class RunRequest(BaseModel):
    csv_path: str = "data/sample/demo.csv"
    news_path: str | None = "data/sample/demo_news.jsonl"
    symbol: str = Field(default="DEMO", min_length=1, max_length=32)
    config_path: str | None = "config/default.yaml"
    initial_cash: float | None = Field(default=None, gt=0)


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
    settings = load_settings(config_path) if config_path else BacktestSettings()
    if request.initial_cash is not None:
        settings = BacktestSettings(**{**settings.__dict__, "initial_cash": request.initial_cash})
    prices = LocalCsvProvider(csv_path, request.symbol)
    news = LocalNewsProvider(news_path) if news_path else None
    return prices, news, settings


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": "paper-trading-only",
        "project_root": str(PROJECT_ROOT),
    }


@app.post("/backtest")
def backtest(request: RunRequest) -> dict:
    try:
        prices, news, settings = _components(request)
        result = BacktestEngine(settings).run(prices, news)
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
        prices, news, settings = _components(request)
        result = run_experiment_suite(prices, settings, news)
        return {"symbol": result["symbol"], "summary": result["summary"]}
    except HTTPException:
        raise
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
