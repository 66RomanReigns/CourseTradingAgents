from __future__ import annotations

import os
from dataclasses import replace
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from tradinglab_agents import __version__
from tradinglab_agents.agents.llm import build_llm_client
from tradinglab_agents.agents.research import MultiAgentResearchPipeline
from tradinglab_agents.api.security import api_auth_enabled, authenticate_request
from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.paper.models import ApprovalPolicy, OrderStatus
from tradinglab_agents.paper.scheduler import run_next_with_lock, run_session_with_lock
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.reporting.paper_dashboard import render_paper_dashboard
from tradinglab_agents.evaluation.experiments import run_experiment_suite, save_run_bundle
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.storage.run_store import RunStore
from tradinglab_agents.workflows.daily import DailyWorkflow

PROJECT_ROOT = Path(os.environ.get("TRADINGLAB_ROOT", Path.cwd())).resolve()
DATABASE_PATH = PROJECT_ROOT / "artifacts/tradinglab.db"
PAPER_DATABASE_PATH = PROJECT_ROOT / "artifacts/paper_trading.db"
app = FastAPI(
    title="TradeLab-Agent API",
    version=__version__,
    description="Course-oriented, paper-trading-only multi-agent service",
)
app.middleware("http")(authenticate_request)


class RunRequest(BaseModel):
    csv_path: str = "data/sample/demo.csv"
    news_path: str | None = "data/sample/demo_news.jsonl"
    evidence_paths: list[str] = Field(default_factory=list)
    symbol: str = Field(default="DEMO", min_length=1, max_length=32)
    config_path: str | None = "config/default.yaml"
    initial_cash: float | None = Field(default=None, gt=0)
    current_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    persist: bool = False


class PaperAccountCreate(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="TradeLab Paper Account", min_length=1, max_length=128)
    symbols: list[str] = Field(min_length=2, max_length=20)
    initial_cash: float | None = Field(default=None, gt=0)
    approval_policy: ApprovalPolicy = ApprovalPolicy.ALL
    config_path: str | None = "config/default.yaml"
    database_path: str = "artifacts/paper_trading.db"


class PaperSessionRequest(BaseModel):
    session_date: str | None = None
    data_dir: str = "data/multi_sample"
    evidence_paths: list[str] = Field(default_factory=list)
    config_path: str | None = "config/default.yaml"
    database_path: str = "artifacts/paper_trading.db"


class PaperReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)
    database_path: str = "artifacts/paper_trading.db"
    config_path: str | None = "config/default.yaml"


class WorkflowDryRunRequest(BaseModel):
    account_id: str | None = Field(default=None, max_length=64)
    config_path: str | None = "config/default.yaml"
    run_id: str | None = Field(default=None, max_length=128)
    resume: bool = False


class PaperCancelRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    note: str = Field(default="", max_length=1000)
    database_path: str = "artifacts/paper_trading.db"
    config_path: str | None = "config/default.yaml"


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


def _safe_directory(value: str) -> Path:
    candidate = Path(value)
    resolved = (PROJECT_ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="directory must stay inside project root") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail=f"directory not found: {value}")
    return resolved


def _safe_writable_path(value: str, kind: str) -> Path:
    candidate = Path(value)
    resolved = (PROJECT_ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{kind} must stay inside project root") from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _safe_database_path(value: str) -> Path:
    return _safe_writable_path(value, "database")


def _paper_service(config_path: str | None, database_path: str) -> PaperTradingService:
    config = _safe_path(config_path, required=False)
    settings = load_settings(config) if config else BacktestSettings()
    database = _safe_database_path(database_path)
    return PaperTradingService(PaperTradingStore(database), settings)


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
        "paper_database": str(PAPER_DATABASE_PATH),
        "external_broker": False,
        "api_authentication": api_auth_enabled(),
    }


@app.get("/workflow/plan")
def workflow_plan(
    account_id: str | None = None,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        return DailyWorkflow(settings, PROJECT_ROOT).plan(
            mode="dry_run",
            account_id=account_id,
        )
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/workflow/dry-run")
def workflow_dry_run(request: WorkflowDryRunRequest) -> dict:
    try:
        config = _safe_path(request.config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        return DailyWorkflow(settings, PROJECT_ROOT).execute(
            mode="dry_run",
            account_id=request.account_id,
            run_id=request.run_id,
            resume=request.resume,
        )
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/workflow/runs/{run_id}")
def workflow_run_status(run_id: str) -> dict:
    state = _safe_path(
        f"artifacts/workflows/{run_id}/state.json",
        required=True,
    )
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="invalid workflow state") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="invalid workflow state")
    return payload


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


@app.post("/research")
def research(request: RunRequest) -> dict:
    try:
        prices, news, evidence, settings, _ = _components(request)
        decision_bar = prices.bars[-1]
        visible = prices.history(decision_bar.available_at)
        pack = FeatureEngine().build(visible, decision_bar.available_at)
        if news is not None:
            news.add_to_pack(pack)
        for provider in evidence:
            provider.add_to_pack(pack)
        client = build_llm_client(settings, PROJECT_ROOT)
        result = MultiAgentResearchPipeline(client).run(
            pack,
            current_weight=request.current_weight,
        )
        return result.model_dump(mode="json")
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/paper/accounts")
def create_paper_account(request: PaperAccountCreate) -> dict:
    try:
        service = _paper_service(request.config_path, request.database_path)
        account = service.initialize_account(
            request.account_id,
            name=request.name,
            symbols=request.symbols,
            initial_cash=request.initial_cash,
            approval_policy=request.approval_policy,
        )
        return {
            "account": service.serialize_account(account),
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/paper/accounts/{account_id}")
def paper_account(
    account_id: str,
    database_path: str = "artifacts/paper_trading.db",
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        return _paper_service(config_path, database_path).account_summary(account_id)
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/paper/accounts/{account_id}/dashboard", response_class=HTMLResponse)
def paper_dashboard(
    account_id: str,
    database_path: str = "artifacts/paper_trading.db",
    config_path: str | None = "config/default.yaml",
) -> HTMLResponse:
    try:
        summary = _paper_service(config_path, database_path).account_summary(account_id)
        return HTMLResponse(render_paper_dashboard(summary))
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/paper/accounts/{account_id}/orders")
def paper_orders(
    account_id: str,
    status: OrderStatus | None = None,
    limit: int = 200,
    database_path: str = "artifacts/paper_trading.db",
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        service = _paper_service(config_path, database_path)
        statuses = [status] if status is not None else None
        orders = service.store.list_orders(account_id, statuses=statuses, limit=limit)
        return {"orders": [service.serialize_order(order) for order in orders]}
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/paper/accounts/{account_id}/memories")
def paper_memories(
    account_id: str,
    symbol: str | None = None,
    outcome_status: str | None = None,
    limit: int = Query(default=100, ge=1, le=5000),
    database_path: str = "artifacts/paper_trading.db",
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        service = _paper_service(config_path, database_path)
        return {
            "memories": service.store.list_decision_memories(
                account_id,
                symbol=symbol,
                outcome_status=outcome_status,
                limit=limit,
            )
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/paper/accounts/{account_id}/sessions")
def run_paper_session(account_id: str, request: PaperSessionRequest) -> dict:
    try:
        service = _paper_service(request.config_path, request.database_path)
        data_dir = _safe_directory(request.data_dir)
        evidence = [str(_safe_path(path)) for path in request.evidence_paths]
        lock_path = _safe_writable_path(
            service.settings.paper_lock_path,
            "paper lock",
        )
        if request.session_date:
            result = run_session_with_lock(
                service,
                account_id,
                data_dir=data_dir,
                session_date=request.session_date,
                lock_path=lock_path,
                evidence_paths=evidence,
            )
        else:
            result = run_next_with_lock(
                service,
                account_id,
                data_dir=data_dir,
                lock_path=lock_path,
                evidence_paths=evidence,
            )
        result["external_broker"] = False
        return result
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/paper/orders/{order_id}/approve")
def approve_paper_order(order_id: str, request: PaperReviewRequest) -> dict:
    try:
        service = _paper_service(request.config_path, request.database_path)
        order = service.approve_order(
            order_id,
            reviewer=request.reviewer,
            note=request.note,
        )
        return {"order": service.serialize_order(order), "external_broker": False}
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/paper/orders/{order_id}/reject")
def reject_paper_order(order_id: str, request: PaperReviewRequest) -> dict:
    try:
        service = _paper_service(request.config_path, request.database_path)
        order = service.reject_order(
            order_id,
            reviewer=request.reviewer,
            note=request.note,
        )
        return {"order": service.serialize_order(order), "external_broker": False}
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/paper/orders/{order_id}/cancel")
def cancel_paper_order(order_id: str, request: PaperCancelRequest) -> dict:
    try:
        service = _paper_service(request.config_path, request.database_path)
        order = service.cancel_order(
            order_id,
            actor=request.actor,
            note=request.note,
        )
        return {"order": service.serialize_order(order), "external_broker": False}
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
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
