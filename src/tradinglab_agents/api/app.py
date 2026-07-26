from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from tradinglab_agents import __version__
from tradinglab_agents.agents.langgraph_research import (
    LangGraphResearchRuntime,
    ResearchGraphInterrupted,
    inspect_research_thread,
)
from tradinglab_agents.agents.llm import MockLLM, build_llm_client
from tradinglab_agents.agents.portfolio_supervisor import (
    LangGraphPortfolioSupervisorRuntime,
)
from tradinglab_agents.agents.research import MultiAgentResearchPipeline
from tradinglab_agents.api.security import api_auth_enabled, authenticate_request
from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.provider_registry import default_provider_registry
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.engine.decision_graph import (
    LangGraphDeterministicDecisionRuntime,
)
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.models import EvidencePack
from tradinglab_agents.paper.models import ApprovalPolicy, OrderStatus
from tradinglab_agents.paper.scheduler import run_next_with_lock, run_session_with_lock
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.reporting.paper_dashboard import render_paper_dashboard
from tradinglab_agents.evaluation.experiments import run_experiment_suite, save_run_bundle
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.storage.provider_health import ProviderHealthStore
from tradinglab_agents.storage.provider_usage import ProviderUsageStore
from tradinglab_agents.storage.run_store import RunStore
from tradinglab_agents.workflows.data_provider_graph import (
    LangGraphDataProviderRuntime,
)
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.research_parent import (
    LangGraphResearchParentRuntime,
)
from tradinglab_agents.workflows.workflow_core import LangGraphWorkflowCoreRuntime

PROJECT_ROOT = Path(os.environ.get("TRADINGLAB_ROOT", Path.cwd())).resolve()
DATABASE_PATH = PROJECT_ROOT / "artifacts/tradinglab.db"
PAPER_DATABASE_PATH = PROJECT_ROOT / "artifacts/paper_trading.db"
app = FastAPI(
    title="TradeLab-Agent API",
    version=__version__,
    description="Reproducible, paper-trading-only LangGraph multi-agent service",
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


class ResearchRunRequest(RunRequest):
    human_review_mode: str | None = Field(default=None, max_length=32)
    thread_id: str | None = Field(default=None, max_length=160)
    resume_decision: str | None = Field(default=None, max_length=16)
    resume_target: float | None = Field(default=None, ge=0.0, le=1.0)


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


def _research_pack_from_components(prices, news, evidence) -> EvidencePack:
    decision_bar = prices.bars[-1]
    visible = prices.history(decision_bar.available_at)
    pack = FeatureEngine().build(visible, decision_bar.available_at)
    if news is not None:
        news.add_to_pack(pack)
    for provider in evidence:
        provider.add_to_pack(pack)
    return pack


def _research_pipeline(
    settings: BacktestSettings,
    *,
    human_review_mode: str | None = None,
) -> MultiAgentResearchPipeline:
    if settings.llm_execution_mode == "live":
        raise ValueError("management API does not allow live LLM research")
    quick_client = build_llm_client(
        settings,
        PROJECT_ROOT,
        model=settings.llm_quick_model,
        cache_namespace="quick",
    )
    deep_client = build_llm_client(
        settings,
        PROJECT_ROOT,
        model=settings.llm_deep_model,
        cache_namespace="deep",
    )
    return MultiAgentResearchPipeline(
        quick_client,
        deep_client,
        debate_rounds=settings.workflow_debate_rounds,
        risk_personas=settings.workflow_risk_personas,
        use_langgraph=(settings.workflow_research_runtime == "langgraph"),
        langchain_trace_path=_safe_writable_path(
            settings.workflow_langchain_trace_path,
            "LangChain trace",
        ),
        langgraph_event_path=_safe_writable_path(
            settings.workflow_langgraph_event_path,
            "LangGraph event log",
        ),
        langgraph_retry_attempts=settings.workflow_langgraph_retry_attempts,
        langgraph_node_timeout_seconds=(
            settings.workflow_langgraph_node_timeout_seconds
        ),
        langgraph_cost_aware_routing=(
            settings.workflow_langgraph_cost_aware_routing
        ),
        langgraph_hold_skip_confidence=(
            settings.workflow_langgraph_hold_skip_confidence
        ),
        langgraph_human_review_mode=(
            human_review_mode or settings.workflow_langgraph_human_review_mode
        ),
    )


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
        "orchestration": {
            "research_runtime": "langgraph",
            "portfolio_supervisor_graph": True,
            "research_parent_graph": True,
            "workflow_core_graph": True,
            "deterministic_decision_graph": True,
            "data_provider_graph": True,
            "provider_fallback_router": True,
            "provider_quality_gate": True,
            "provider_health_state_machine": True,
            "dynamic_send_fan_out": True,
            "provider_refresh_in_workflow_core": False,
            "provider_refresh_in_data_provider_graph": True,
            "fallback_only_reduces_risk": True,
            "blocked_provider_data_persisted": False,
            "paper_execution_in_workflow_core": False,
            "account_mutation_in_decision_graph": False,
            "order_persistence_in_decision_graph": False,
            "market_calendar": "XNYS",
            "strict_market_sessions": True,
            "corporate_action_ledger": True,
            "paper_schema_version": PaperTradingStore.CURRENT_SCHEMA_VERSION,
            "exchange_calendars": version("exchange-calendars"),
            "langchain": version("langchain"),
            "langchain_core": version("langchain-core"),
            "langgraph": version("langgraph"),
            "langgraph_checkpoint_sqlite": version(
                "langgraph-checkpoint-sqlite"
            ),
        },
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


@app.get("/providers/usage")
def provider_usage(
    run_id: str | None = None,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        database = _safe_database_path(settings.workflow_provider_usage_database)
        return ProviderUsageStore(database).summary(run_id=run_id)
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/providers/capabilities")
def provider_capabilities() -> dict:
    return {
        **default_provider_registry().as_dict(),
        "credentials_included": False,
        "external_request": False,
    }


@app.get("/providers/health")
def provider_health(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        database = _safe_database_path(
            settings.workflow_provider_health_database
        )
        store = ProviderHealthStore(database)
        return {
            "database": str(database),
            "providers": [item.as_dict() for item in store.list()],
            "credentials_included": False,
            "external_request": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/providers/events")
def provider_events(
    run_id: str | None = None,
    limit: int = 200,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        database = _safe_database_path(
            settings.workflow_provider_health_database
        )
        return {
            "database": str(database),
            "run_id": run_id,
            "events": ProviderHealthStore(database).events(
                run_id=run_id,
                limit=limit,
            ),
            "credentials_included": False,
            "external_request": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/providers/conflicts")
def provider_conflicts(
    run_id: str | None = None,
    limit: int = 200,
    config_path: str | None = "config/default.yaml",
) -> dict:
    payload = provider_events(
        run_id=run_id,
        limit=limit,
        config_path=config_path,
    )
    return {
        **payload,
        "events": [
            item
            for item in payload["events"]
            if item.get("status") == "DATA_CONFLICT"
        ],
    }


@app.get("/market/data-quality")
def market_data_quality(
    run_id: str | None = None,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        health_database = _safe_database_path(
            settings.workflow_provider_health_database
        )
        result: dict = {
            "run_id": run_id,
            "thresholds": {
                "minimum_quality_score": (
                    settings.workflow_provider_min_quality_score
                ),
                "block_quality_score": (
                    settings.workflow_provider_block_quality_score
                ),
                "conflict_warn_relative_difference": (
                    settings.workflow_provider_conflict_warn_relative_difference
                ),
                "conflict_block_relative_difference": (
                    settings.workflow_provider_conflict_block_relative_difference
                ),
            },
            "provider_health": [
                item.as_dict() for item in ProviderHealthStore(health_database).list()
            ],
            "fallback_only_reduces_risk": True,
            "external_request": False,
        }
        if run_id:
            checkpoint = _safe_database_path(
                settings.workflow_provider_checkpoint_database
            )
            result["thread"] = inspect_research_thread(
                checkpoint,
                f"{run_id}:DATA_PROVIDER",
            )
        return result
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/workflow/data-provider-graph")
def data_provider_graph(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphDataProviderRuntime(
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            )
        )
        return {
            "runtime": "langgraph",
            "thread_suffix": "DATA_PROVIDER",
            "mermaid": runtime.mermaid(),
            "checkpoint_database": (
                settings.workflow_provider_checkpoint_database
            ),
            "nodes": ["prepare", "route_request", "persist_and_gate"],
            "dynamic_send_fan_out": True,
            "input_fingerprint": "sha256",
            "fallback_only_reduces_risk": True,
            "blocked_data_persisted": False,
            "external_request": False,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@app.get("/research/graph")
def research_graph(
    symbol: str = Query(default="DEMO", min_length=1, max_length=32),
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphResearchRuntime(
            MockLLM(model=settings.llm_quick_model),
            MockLLM(model=settings.llm_deep_model),
            debate_rounds=settings.workflow_debate_rounds,
            risk_personas=settings.workflow_risk_personas,
            trace_path=_safe_writable_path(
                settings.workflow_langchain_trace_path,
                "LangChain trace",
            ),
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            ),
            retry_attempts=settings.workflow_langgraph_retry_attempts,
            node_timeout_seconds=settings.workflow_langgraph_node_timeout_seconds,
            cost_aware_routing=settings.workflow_langgraph_cost_aware_routing,
            hold_skip_confidence=settings.workflow_langgraph_hold_skip_confidence,
            human_review_mode=settings.workflow_langgraph_human_review_mode,
        )
        pack = EvidencePack(
            symbol=symbol.upper(),
            decision_time=datetime.now(timezone.utc),
        )
        return {
            "runtime": "langgraph",
            "mermaid": runtime.mermaid(pack),
            "debate_rounds": settings.workflow_debate_rounds,
            "risk_personas": list(settings.workflow_risk_personas),
            "native_parallel_branches": True,
            "conditional_routing": True,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/research/portfolio-graph")
def portfolio_research_graph(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphPortfolioSupervisorRuntime(
            MockLLM(model=settings.llm_quick_model),
            MockLLM(model=settings.llm_deep_model),
            trace_path=_safe_writable_path(
                settings.workflow_langchain_trace_path,
                "LangChain trace",
            ),
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            ),
            retry_attempts=settings.workflow_langgraph_retry_attempts,
            max_gross_target=settings.max_gross_exposure,
            max_positions=settings.max_positions,
            high_correlation_threshold=(
                settings.workflow_portfolio_high_correlation_threshold
            ),
            cluster_gross_cap=settings.workflow_portfolio_cluster_gross_cap,
        )
        return {
            "runtime": "langgraph",
            "graph_type": "portfolio_supervisor",
            "enabled": settings.workflow_portfolio_supervisor_enabled,
            "mermaid": runtime.mermaid(),
            "parallel_reviewers": ["correlation", "concentration"],
            "deterministic_non_expansion_guard": True,
            "hard_portfolio_risk_still_required": True,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/research/parent-graph")
def research_parent_graph(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphResearchParentRuntime(
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            )
        )
        return {
            "runtime": "langgraph",
            "graph_type": "research_parent",
            "enabled": settings.workflow_research_parent_graph_enabled,
            "dynamic_send_fan_out": True,
            "mermaid": runtime.mermaid(),
            "checkpoint_database": str(
                _safe_database_path(
                    settings.workflow_research_parent_checkpoint_database
                )
            ),
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/market/calendar")
def market_calendar(
    start: str,
    end: str,
    calendar_name: str | None = None,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        if end_date < start_date:
            raise ValueError("calendar end date cannot precede start date")
        if (end_date - start_date).days > 3653:
            raise ValueError("calendar range cannot exceed ten years")
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        calendar = ExchangeTradingCalendar(
            calendar_name or settings.market_calendar_name
        )
        return {
            **calendar.summary(start_date, end_date),
            "external_request": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/market/semantics")
def market_semantics(
    data_dir: str = "data/multi_sample",
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        directory = _safe_directory(data_dir)
        payload = DailyWorkflow(settings, PROJECT_ROOT)._validate_local_data(
            directory
        )
        return {
            **payload,
            "external_request": False,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/workflow/core-graph")
def workflow_core_graph(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphWorkflowCoreRuntime(
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            )
        )
        return {
            "runtime": "langgraph",
            "graph_type": "workflow_core",
            "enabled": settings.workflow_core_graph_enabled,
            "mermaid": runtime.mermaid(),
            "checkpoint_database": str(
                _safe_database_path(settings.workflow_core_checkpoint_database)
            ),
            "nodes": [
                "data_validation",
                "research_parent",
                "overlay_assembly",
                "decision_preparation",
            ],
            "provider_refresh_in_graph": False,
            "paper_execution_in_graph": False,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/workflow/decision-graph")
def workflow_decision_graph(
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        runtime = LangGraphDeterministicDecisionRuntime(
            PortfolioPlanner(settings),
            event_path=_safe_writable_path(
                settings.workflow_langgraph_event_path,
                "LangGraph event log",
            ),
        )
        return {
            "runtime": "langgraph",
            "graph_type": "deterministic_decision",
            "enabled": settings.workflow_decision_graph_enabled,
            "mermaid": runtime.mermaid(),
            "checkpoint_database": str(
                _safe_database_path(
                    settings.workflow_decision_checkpoint_database
                )
            ),
            "symbol_subgraph_nodes": [
                "evidence_pack",
                "quant_signal",
                "fusion_critic",
                "regime_guard",
                "target_overlay",
            ],
            "portfolio_nodes": ["portfolio_risk", "finalize"],
            "input_fingerprint": "sha256",
            "plan_fingerprint": "sha256",
            "account_mutation_in_graph": False,
            "order_persistence_in_graph": False,
            "external_broker": False,
        }
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/research/threads/{thread_id}")
def research_thread_status(
    thread_id: str,
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        config = _safe_path(config_path, required=False)
        settings = load_settings(config) if config else BacktestSettings()
        thread = thread_id.upper()
        database_value = (
            settings.workflow_provider_checkpoint_database
            if thread.endswith(":DATA_PROVIDER")
            else (
                settings.workflow_core_checkpoint_database
                if thread.endswith(":WORKFLOW_CORE")
                else (
                settings.workflow_decision_checkpoint_database
                if thread.endswith(":DECISION")
                else (
                    settings.workflow_research_parent_checkpoint_database
                    if thread.endswith(":RESEARCH_PARENT")
                        else settings.workflow_langgraph_checkpoint_database
                    )
                )
            )
        )
        database = _safe_database_path(database_value)
        return inspect_research_thread(database, thread_id)
    except HTTPException:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/research")
def research(request: ResearchRunRequest) -> dict:
    try:
        prices, news, evidence, settings, _ = _components(request)
        human_review_mode = getattr(request, "human_review_mode", None)
        resume_decision = getattr(request, "resume_decision", None)
        resume_target = getattr(request, "resume_target", None)
        requested_thread_id = getattr(request, "thread_id", None)
        if human_review_mode not in {
            None,
            "paper_queue",
            "interrupt_directional",
        }:
            raise ValueError("unsupported human_review_mode")
        if resume_decision not in {None, "approve", "reject", "reduce"}:
            raise ValueError("unsupported resume_decision")
        if resume_decision and not requested_thread_id:
            raise ValueError("thread_id is required when resuming human review")
        pack = _research_pack_from_components(prices, news, evidence)
        pipeline = _research_pipeline(
            settings,
            human_review_mode=human_review_mode,
        )
        thread_id = requested_thread_id or f"research:{pack.symbol}:{uuid4().hex}"
        checkpointer = _safe_database_path(
            settings.workflow_langgraph_checkpoint_database
        )
        human_response = None
        if resume_decision:
            human_response = {"decision": resume_decision}
            if resume_target is not None:
                human_response["target_weight"] = resume_target
        try:
            result = pipeline.run_resumable(
                pack,
                current_weight=request.current_weight,
                node_runner=lambda _name, _model, function: function(),
                thread_id=thread_id,
                checkpointer_path=checkpointer,
                resume=bool(resume_decision),
                human_response=human_response,
            )
        except ResearchGraphInterrupted as exc:
            execution = exc.execution
            return {
                "status": "WAITING_FOR_HUMAN_REVIEW",
                "thread_id": execution.thread_id,
                "interrupts": list(execution.interrupt_payloads),
                "checkpoint_database": str(checkpointer),
                "external_broker": False,
            }
        payload = result.model_dump(mode="json")
        execution = pipeline._last_graph_execution
        payload["graph_runtime"] = (
            {
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "execution_path": list(execution.execution_path),
                "stream_event_count": execution.event_count,
                "human_review": execution.latest_state.get("human_review", {}),
                "checkpoint_database": str(checkpointer),
            }
            if execution is not None
            else {"runtime": "legacy_ablation"}
        )
        payload["external_broker"] = False
        return payload
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


@app.get("/paper/accounts/{account_id}/corporate-actions")
def paper_corporate_actions(
    account_id: str,
    limit: int = 100,
    database_path: str = "artifacts/paper_trading.db",
    config_path: str | None = "config/default.yaml",
) -> dict:
    try:
        service = _paper_service(config_path, database_path)
        service.store.require_account(account_id)
        return {
            "events": service.store.list_corporate_action_events(
                account_id,
                limit=limit,
            ),
            "external_broker": False,
        }
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
