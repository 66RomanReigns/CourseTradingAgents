from __future__ import annotations

import importlib
import json
from importlib.metadata import PackageNotFoundError, version
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tradinglab_agents.agents.langgraph_research import LangGraphResearchRuntime
from tradinglab_agents.agents.llm import MockLLM, build_llm_client
from tradinglab_agents.agents.portfolio_supervisor import (
    LangGraphPortfolioSupervisorRuntime,
    build_portfolio_market_context,
)
from tradinglab_agents.config import load_settings
from tradinglab_agents.data.connectivity import probe_external_access
from tradinglab_agents.data.corporate_actions import LocalCorporateActionProvider
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.fallback_router import (
    ProviderFallbackRouter,
    ProviderRequest,
)
from tradinglab_agents.data.http_client import DataApiError
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.provider_quality import ProviderCandidate
from tradinglab_agents.data.provider_registry import (
    DataKind,
    ProviderCapability,
    ProviderRegistry,
)
from tradinglab_agents.engine.decision_graph import (
    LangGraphDeterministicDecisionRuntime,
)
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.models import Portfolio
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.storage.provider_health import ProviderHealthStore
from tradinglab_agents.storage.provider_usage import ProviderUsageStore
from tradinglab_agents.workflows.data_provider_graph import (
    LangGraphDataProviderRuntime,
)
from tradinglab_agents.workflows.daily import DailyWorkflow
from tradinglab_agents.workflows.research_parent import (
    LangGraphResearchParentRuntime,
)
from tradinglab_agents.workflows.workflow_core import LangGraphWorkflowCoreRuntime


def _check_import(name: str, distribution: str | None = None) -> dict:
    try:
        module = importlib.import_module(name)
        package = distribution or name.split(".", 1)[0]
        try:
            package_version = version(package)
        except PackageNotFoundError:
            package_version = getattr(module, "__version__", "unknown")
        return {"ok": True, "version": package_version}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _windows_secret_acl_status(path: Path) -> tuple[bool, str]:
    try:
        path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return False, "Windows secret file must be under the current user directory"
    try:
        whoami = subprocess.run(
            ["whoami"], capture_output=True, text=True, check=False
        )
        acl = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        return False, f"Windows ACL inspection unavailable: {type(exc).__name__}"
    if acl.returncode != 0:
        return False, "Windows ACL inspection failed"
    identity = whoami.stdout.strip().lower()
    lines = acl.stdout.splitlines()
    entries = "\n".join(lines[1:]).lower()
    broad_principals = (
        "everyone",
        "authenticated users",
        "\\users:",
        "builtin\\users",
    )
    if any(principal in entries for principal in broad_principals):
        return False, "Windows secret file ACL grants access to a broad principal"
    if not identity or identity not in entries:
        return False, "Windows secret file ACL does not grant the current user access"
    return True, "Windows ACL is explicit and limited to the current user"


def _secret_file_status() -> dict:
    path = Path(
        os.environ.get(
            "TRADINGLAB_KEYS_FILE",
            Path.home() / ".config" / "tradinglab" / "tradinglab.keys",
        )
    )
    required = {
        "ALPHA_VANTAGE_API_KEY",
        "SEC_USER_AGENT",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "TRADINGLAB_API_TOKEN",
    }
    if not path.is_file():
        return {
            "ok": False,
            "severity": "warning",
            "path": str(path),
            "detail": "external secret file is not configured",
        }
    if os.name == "nt":
        permissions_ok, permissions = _windows_secret_acl_status(path)
    else:
        permissions = f"{path.stat().st_mode & 0o777:03o}"
        permissions_ok = permissions == "600"
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
        "ok": permissions_ok and not missing,
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
        "langchain",
        "langchain_core",
        "langgraph",
        "exchange_calendars",
    ):
        distribution = {
            "langchain_core": "langchain-core",
        }.get(name)
        checks[f"import:{name}"] = _check_import(name, distribution)
    checks["import:langgraph.checkpoint.sqlite"] = _check_import(
        "langgraph.checkpoint.sqlite",
        "langgraph-checkpoint-sqlite",
    )

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

    try:
        settings = load_settings(ROOT / "config/default.yaml")
        calendar = ExchangeTradingCalendar(settings.market_calendar_name)
        semantics = DailyWorkflow(settings, ROOT)._validate_local_data(
            ROOT / "data/multi_sample"
        )
        early = calendar.session("2025-11-28")
        aapl_actions = LocalCorporateActionProvider(
            ROOT / "data/multi_sample/AAPL_actions.jsonl",
            "AAPL",
            calendar=calendar,
        )
        checks["market_semantics"] = {
            "ok": (
                not calendar.is_session("2025-12-25")
                and early.is_early_close
                and early.close_at.hour == 13
                and semantics["dropped_timestamp_count"] == 0
                and semantics["corporate_action_count"] >= 5
                and aapl_actions.summary()["split_count"] >= 1
            ),
            "calendar": settings.market_calendar_name,
            "timezone": str(calendar.timezone),
            "christmas_2025_closed": not calendar.is_session("2025-12-25"),
            "post_thanksgiving_close": early.close_at.isoformat(),
            "synchronized_sessions": semantics["synchronized_sessions"],
            "early_close_count": semantics["calendar"]["early_close_count"],
            "dropped_timestamp_count": semantics["dropped_timestamp_count"],
            "corporate_action_count": semantics["corporate_action_count"],
            "aapl_split_count": aapl_actions.summary()["split_count"],
            "strict_session_times": semantics["strict_session_times"],
            "adjust_history_for_splits": semantics[
                "adjust_history_for_splits"
            ],
            "external_request": False,
        }
    except Exception as exc:
        checks["market_semantics"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            graph_prices = LocalCsvProvider(ROOT / "data/sample/demo.csv", "DEMO")
            decision_bar = graph_prices.bars[-1]
            pack = FeatureEngine().build(
                graph_prices.history(decision_bar.available_at),
                decision_bar.available_at,
            )
            LocalNewsProvider(ROOT / "data/sample/demo_news.jsonl").add_to_pack(pack)
            runtime = LangGraphResearchRuntime(
                MockLLM(model="doctor-quick"),
                MockLLM(model="doctor-deep"),
                debate_rounds=1,
                risk_personas=("aggressive", "balanced", "conservative"),
                trace_path=graph_root / "langchain.jsonl",
                retry_attempts=1,
            )
            execution = runtime.run(
                pack,
                thread_id="doctor-thread",
                node_runner=lambda _name, _model, function: function(),
                checkpointer_path=graph_root / "langgraph.db",
            )
            mermaid = runtime.mermaid(pack)
            checks["langgraph_runtime"] = {
                "ok": (
                    execution.result is not None
                    and execution.checkpoint_count > 0
                    and (graph_root / "langgraph.db").is_file()
                    and (graph_root / "langchain.jsonl").is_file()
                ),
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "native_parallel_branches": True,
                "conditional_routing": True,
                "human_interrupt_supported": True,
                "mermaid_contains_human_gate": "human_review_gate" in mermaid,
                "strict_msgpack": os.environ.get("LANGGRAPH_STRICT_MSGPACK"),
                "external_request": False,
            }
    except Exception as exc:
        checks["langgraph_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            providers = {
                symbol: LocalCsvProvider(
                    ROOT / "data" / "multi_sample" / f"{symbol}.csv",
                    symbol,
                )
                for symbol in ("SPY", "QQQ")
            }
            context = build_portfolio_market_context(
                providers,
                window_sessions=60,
                high_correlation_threshold=0.80,
            )
            runtime = LangGraphPortfolioSupervisorRuntime(
                MockLLM(model="doctor-quick"),
                MockLLM(model="doctor-deep"),
                trace_path=graph_root / "langchain.jsonl",
                event_path=graph_root / "langgraph.jsonl",
                retry_attempts=1,
                max_gross_target=0.90,
                max_positions=5,
                high_correlation_threshold=0.80,
                cluster_gross_cap=0.25,
            )
            execution = runtime.run(
                [
                    {
                        "symbol": "SPY",
                        "action": "BUY",
                        "target_weight": 0.18,
                        "confidence": 0.80,
                    },
                    {
                        "symbol": "QQQ",
                        "action": "BUY",
                        "target_weight": 0.17,
                        "confidence": 0.75,
                    },
                ],
                context,
                thread_id="doctor-portfolio-thread",
                node_runner=lambda _name, _model, function: function(),
                checkpointer_path=graph_root / "portfolio.db",
            )
            mermaid = runtime.mermaid()
            checks["portfolio_supervisor_runtime"] = {
                "ok": (
                    execution.result.gross_target <= 0.25 + 1e-12
                    and execution.checkpoint_count > 0
                    and (graph_root / "portfolio.db").is_file()
                ),
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "stream_event_count": execution.event_count,
                "gross_target": execution.result.gross_target,
                "high_correlation_pairs": context["high_correlation_pairs"],
                "parallel_reviewers": True,
                "non_expansion_guard": True,
                "hard_portfolio_risk_still_required": True,
                "mermaid_contains_guard": "deterministic_guard" in mermaid,
                "external_request": False,
            }
    except Exception as exc:
        checks["portfolio_supervisor_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            runtime = LangGraphResearchParentRuntime(
                event_path=graph_root / "parent-events.jsonl"
            )
            execution = runtime.run(
                thread_id="doctor-research-parent",
                candidate_selector=lambda: [
                    {"symbol": "SPY", "priority": 0.9},
                    {"symbol": "QQQ", "priority": 0.8},
                    {"symbol": "AAPL", "priority": 0.7},
                ],
                symbol_runner=lambda candidate: {
                    "symbol": candidate["symbol"],
                    "status": "completed",
                },
                portfolio_runner=lambda rows: {
                    "status": "completed",
                    "candidate_count": len(rows),
                },
                audit_runner=lambda _name, function: function(),
                checkpointer_path=graph_root / "parent.db",
            )
            mermaid = runtime.mermaid()
            checks["research_parent_runtime"] = {
                "ok": (
                    len(execution.candidate_results) == 3
                    and execution.portfolio_summary.get("candidate_count") == 3
                    and execution.checkpoint_count > 0
                    and (graph_root / "parent.db").is_file()
                ),
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "stream_event_count": execution.event_count,
                "dynamic_send_fan_out": True,
                "candidate_symbols": [
                    row["symbol"] for row in execution.candidate_results
                ],
                "portfolio_fan_in": True,
                "mermaid_contains_symbol_research": (
                    "symbol_research" in mermaid
                ),
                "external_request": False,
            }
    except Exception as exc:
        checks["research_parent_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            runtime = LangGraphWorkflowCoreRuntime(
                event_path=graph_root / "core-events.jsonl"
            )
            execution = runtime.run(
                thread_id="doctor-workflow-core",
                research_enabled=True,
                data_validator=lambda: {
                    "status": "completed",
                    "market_files": 2,
                    "data_dir": "doctor",
                },
                research_runner=lambda: {
                    "status": "completed",
                    "candidates": {"SPY": {}, "QQQ": {}},
                },
                overlay_assembler=lambda _research: {
                    "SPY": {
                        "action": "HOLD",
                        "target_weight": 0.0,
                        "requires_human_approval": True,
                    },
                    "QQQ": {
                        "action": "HOLD",
                        "target_weight": 0.0,
                        "requires_human_approval": True,
                    },
                },
                decision_preparer=lambda _validation, _research, overlays: {
                    "status": "ready",
                    "overlay_count": len(overlays),
                    "safe_for_paper_input": True,
                    "external_broker": False,
                },
                audit_runner=lambda _name, function: function(),
                checkpointer_path=graph_root / "core.db",
            )
            mermaid = runtime.mermaid()
            checks["workflow_core_runtime"] = {
                "ok": (
                    execution.decision_preparation.get("safe_for_paper_input")
                    is True
                    and len(execution.research_overlays) == 2
                    and execution.checkpoint_count > 0
                    and (graph_root / "core.db").is_file()
                ),
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "stream_event_count": execution.event_count,
                "overlay_count": len(execution.research_overlays),
                "safe_for_paper_input": execution.decision_preparation.get(
                    "safe_for_paper_input"
                ),
                "provider_refresh_in_graph": False,
                "paper_execution_in_graph": False,
                "mermaid_contains_decision_preparation": (
                    "decision_preparation" in mermaid
                ),
                "external_request": False,
            }
    except Exception as exc:
        checks["workflow_core_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            symbols = ("SPY", "QQQ", "AAPL")
            market = AlignedMarketData(
                {
                    symbol: LocalCsvProvider(
                        ROOT / "data/multi_sample" / f"{symbol}.csv",
                        symbol,
                    )
                    for symbol in symbols
                }
            )
            planner = PortfolioPlanner(settings)
            timestamp = market.timestamps[max(20, settings.warmup_bars)]
            portfolio = Portfolio(cash=100_000.0, peak_equity=100_000.0)
            before = (
                portfolio.cash,
                dict(portfolio.positions),
                portfolio.peak_equity,
            )
            context = planner.prepare_context(
                market,
                timestamp,
                portfolio,
            )
            runtime = LangGraphDeterministicDecisionRuntime(
                planner,
                event_path=graph_root / "decision-events.jsonl",
            )
            execution = runtime.run(
                thread_id="doctor-deterministic-decision",
                context=context,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=graph_root / "decision.db",
            )
            mermaid = runtime.mermaid()
            checks["deterministic_decision_runtime"] = {
                "ok": (
                    execution.checkpoint_count > 0
                    and len(execution.plan_sha256) == 64
                    and len(execution.plan.graph_runtime.get("input_sha256", ""))
                    == 64
                    and len(execution.plan.target_weights) == len(symbols)
                    and before
                    == (
                        portfolio.cash,
                        dict(portfolio.positions),
                        portfolio.peak_equity,
                    )
                    and (graph_root / "decision.db").is_file()
                ),
                "thread_id": execution.thread_id,
                "checkpoint_count": execution.checkpoint_count,
                "stream_event_count": execution.event_count,
                "symbol_count": len(execution.plan.target_weights),
                "input_sha256": execution.plan.graph_runtime.get(
                    "input_sha256"
                ),
                "plan_sha256": execution.plan_sha256,
                "risk_state": execution.plan.risk_decision.state,
                "input_portfolio_mutated": False,
                "account_mutation_in_graph": False,
                "order_persistence_in_graph": False,
                "mermaid_contains_quant": "quant_signal" in mermaid,
                "mermaid_contains_regime": "regime_guard" in mermaid,
                "mermaid_contains_portfolio_risk": "portfolio_risk" in mermaid,
                "external_request": False,
            }
    except Exception as exc:
        checks["deterministic_decision_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        with tempfile.TemporaryDirectory() as directory:
            graph_root = Path(directory)
            registry = ProviderRegistry(
                (
                    ProviderCapability(
                        provider="primary",
                        data_kind=DataKind.MARKET_DAILY,
                        priority=10,
                        authority_score=0.9,
                        freshness_hours=48.0,
                        timeout_seconds=2.0,
                        max_retries=0,
                        quota_units=1.0,
                        point_in_time=True,
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
                        supports_live=False,
                        is_cache=True,
                    ),
                )
            )
            health = ProviderHealthStore(graph_root / "health.db")
            now = datetime.now(timezone.utc)

            def candidate(provider: str, resource: str, close: float):
                return ProviderCandidate(
                    provider=provider,
                    data_kind=DataKind.MARKET_DAILY,
                    resource=resource,
                    records=(
                        {
                            "timestamp": "2026-07-24T16:00:00",
                            "close": close,
                        },
                    ),
                    observed_at=now,
                    latest_data_at=now,
                    metadata={"source": "doctor_fixture"},
                )

            fallback_router = ProviderFallbackRouter(
                registry=registry,
                health_store=health,
                run_id="doctor-fallback",
                fetchers={
                    "primary": lambda _request, _capability: (_ for _ in ()).throw(
                        DataApiError("HTTP 429 rate limit")
                    ),
                    "local_cache": lambda request, _capability: candidate(
                        "local_cache", request.resource, 100.0
                    ),
                },
            )
            fallback = fallback_router.route(
                ProviderRequest(
                    "market.SPY",
                    DataKind.MARKET_DAILY,
                    "SPY",
                )
            )

            conflict_registry = ProviderRegistry(
                (
                    registry.capability("primary", DataKind.MARKET_DAILY),
                    ProviderCapability(
                        provider="secondary",
                        data_kind=DataKind.MARKET_DAILY,
                        priority=20,
                        authority_score=0.85,
                        freshness_hours=48.0,
                        timeout_seconds=2.0,
                        max_retries=0,
                        quota_units=1.0,
                        point_in_time=True,
                    ),
                )
            )
            conflict_health = ProviderHealthStore(graph_root / "conflict-health.db")
            conflict_router = ProviderFallbackRouter(
                registry=conflict_registry,
                health_store=conflict_health,
                run_id="doctor-conflict",
                fetchers={
                    "primary": lambda request, _capability: candidate(
                        "primary", request.resource, 100.0
                    ),
                    "secondary": lambda request, _capability: candidate(
                        "secondary", request.resource, 110.0
                    ),
                },
            )
            persisted: list[dict] = []
            conflict_blocked = False
            runtime = LangGraphDataProviderRuntime(
                event_path=graph_root / "events.jsonl"
            )
            try:
                runtime.run(
                    thread_id="doctor:DATA_PROVIDER",
                    requests=(
                        ProviderRequest(
                            "market.SPY",
                            DataKind.MARKET_DAILY,
                            "SPY",
                            shadow_validate=True,
                        ),
                    ),
                    router=conflict_router,
                    persist_result=lambda item: persisted.append(item) or item,
                    checkpointer_path=graph_root / "provider.db",
                )
            except ValueError as exc:
                conflict_blocked = "blocked persistence" in str(exc)
            mermaid = runtime.mermaid()
            checks["data_provider_runtime"] = {
                "ok": (
                    fallback.selected.provider == "local_cache"
                    and fallback.quality.status.value == "DEGRADED"
                    and not fallback.quality.allow_position_increase
                    and conflict_blocked
                    and persisted == []
                    and (graph_root / "provider.db").is_file()
                ),
                "fallback_provider": fallback.selected.provider,
                "fallback_depth": fallback.fallback_depth,
                "fallback_quality_status": fallback.quality.status.value,
                "fallback_allows_position_increase": (
                    fallback.quality.allow_position_increase
                ),
                "primary_health": health.get(
                    "primary", DataKind.MARKET_DAILY
                ).status.value,
                "conflict_blocked_before_persistence": conflict_blocked,
                "persisted_blocked_records": len(persisted),
                "checkpoint_database_created": (
                    graph_root / "provider.db"
                ).is_file(),
                "mermaid_contains_route": "route_request" in mermaid,
                "mermaid_contains_quality_gate": "persist_and_gate" in mermaid,
                "external_request": False,
                "credentials_sent": False,
            }
    except Exception as exc:
        checks["data_provider_runtime"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

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

    try:
        connectivity = probe_external_access(timeout_seconds=3.0)
        checks["external_connectivity"] = {
            "ok": connectivity.ok,
            "severity": "warning",
            "state": connectivity.state.value,
            "final_host": urlsplit(connectivity.final_url or "").hostname,
            "detail": connectivity.detail,
            "credentials_sent": False,
        }
    except Exception as exc:
        checks["external_connectivity"] = {
            "ok": False,
            "severity": "warning",
            "detail": f"{type(exc).__name__}: {exc}",
            "credentials_sent": False,
        }

    try:
        usage = ProviderUsageStore(
            ROOT / settings.workflow_provider_usage_database
        )
        checks["provider_usage"] = {
            "ok": True,
            "path": str(usage.path),
            "schema_version": usage.SCHEMA_VERSION,
            "summary": usage.summary(),
        }
    except Exception as exc:
        checks["provider_usage"] = {"ok": False, "error": str(exc)}

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
