from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.provider_registry import default_provider_registry
from tradinglab_agents.data.sec_edgar import DEFAULT_US_GAAP_CONCEPTS, SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import (
    write_bars_csv,
    write_evidence_jsonl,
    write_news_jsonl,
)
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.engine.decision_graph import (
    LangGraphDeterministicDecisionRuntime,
)
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.engine.multi_backtest import MultiAssetBacktestEngine
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.models import EvidencePack
from tradinglab_agents.paper.models import ApprovalPolicy, OrderStatus
from tradinglab_agents.paper.scheduler import run_next_with_lock, run_session_with_lock
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.reporting.paper_dashboard import render_paper_dashboard
from tradinglab_agents.evaluation.benchmark_suite import run_scenario_benchmark, save_benchmark
from tradinglab_agents.evaluation.experiments import (
    run_experiment_suite,
    save_experiment,
    save_run_bundle,
)
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
from tradinglab_agents.workflows.smoke import ProviderSmokeRunner
from tradinglab_agents.workflows.workflow_core import LangGraphWorkflowCoreRuntime


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _provider(args):
    prices = LocalCsvProvider(args.csv, args.symbol)
    news = LocalNewsProvider(args.news) if getattr(args, "news", None) else None
    evidence = [
        LocalPointInTimeEvidenceProvider(path)
        for path in (getattr(args, "evidence", None) or [])
    ]
    return prices, news, evidence


def _settings(args) -> BacktestSettings:
    settings = load_settings(args.config) if getattr(args, "config", None) else BacktestSettings()
    if getattr(args, "cash", None) is not None:
        settings = replace(settings, initial_cash=args.cash)
    return settings


def _input_files(args) -> list[Path]:
    values = [Path(args.csv)]
    for name in ("news", "config"):
        value = getattr(args, name, None)
        if value:
            values.append(Path(value))
    values.extend(Path(item) for item in (getattr(args, "evidence", None) or []))
    return values


def _run_backtest(args) -> None:
    prices, news, evidence = _provider(args)
    result = BacktestEngine(_settings(args)).run(
        prices,
        news,
        evidence_providers=evidence,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result["metrics"], indent=2))
    print(f"external evidence files: {len(evidence)}")
    print(f"full report: {output}")


def _run_multi_backtest(args) -> None:
    data_dir = Path(args.data_dir)
    symbols = tuple(dict.fromkeys(symbol.upper() for symbol in args.symbols))
    if len(symbols) < 2:
        raise ValueError("multi-backtest requires at least two unique symbols")
    providers: dict[str, LocalCsvProvider] = {}
    news_providers: dict[str, LocalNewsProvider] = {}
    for symbol in symbols:
        price_path = data_dir / f"{symbol}.csv"
        if not price_path.is_file():
            raise ValueError(f"missing price file for {symbol}: {price_path}")
        providers[symbol] = LocalCsvProvider(price_path, symbol)
        news_path = data_dir / f"{symbol}_news.jsonl"
        if news_path.is_file():
            news_providers[symbol] = LocalNewsProvider(news_path)
    evidence = [
        LocalPointInTimeEvidenceProvider(path)
        for path in (getattr(args, "evidence", None) or [])
    ]
    result = MultiAssetBacktestEngine(_settings(args)).run(
        providers,
        news_providers,
        evidence_providers=evidence,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    print(f"symbols: {','.join(result['symbols'])}")
    print(f"final gross exposure: {result['gross_exposure']:.2%}")
    print(f"full report: {output}")


def _paper_components(args) -> tuple[PaperTradingService, BacktestSettings, Path, Path]:
    settings = _settings(args)
    database = Path(getattr(args, "database", None) or settings.paper_database_path)
    data_dir = Path(getattr(args, "data_dir", None) or settings.paper_data_dir)
    service = PaperTradingService(PaperTradingStore(database), settings)
    return service, settings, database, data_dir


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _run_paper_init(args) -> None:
    service, settings, database, _ = _paper_components(args)
    policy = ApprovalPolicy(
        (args.approval_policy or settings.paper_approval_policy).upper()
    )
    account = service.initialize_account(
        args.account,
        name=args.name,
        symbols=args.symbols,
        initial_cash=args.cash,
        approval_policy=policy,
    )
    _print_json(service.serialize_account(account))
    print(f"paper database: {database}")
    print("external broker: disabled")


def _run_paper_account(args) -> None:
    service, _, database, _ = _paper_components(args)
    _print_json(service.account_summary(args.account))
    print(f"paper database: {database}")


def _run_paper_dashboard(args) -> None:
    service, _, _, _ = _paper_components(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_paper_dashboard(service.account_summary(args.account)),
        encoding="utf-8",
    )
    print(f"paper dashboard: {output}")
    print("external broker: disabled")


def _run_paper_orders(args) -> None:
    service, _, _, _ = _paper_components(args)
    statuses = [OrderStatus(value) for value in args.status] if args.status else None
    orders = service.store.list_orders(
        args.account,
        statuses=statuses,
        limit=args.limit,
    )
    _print_json([service.serialize_order(order) for order in orders])


def _run_paper_memories(args) -> None:
    service, _, _, _ = _paper_components(args)
    memories = service.store.list_decision_memories(
        args.account,
        symbol=args.symbol,
        outcome_status=args.outcome_status,
        limit=args.limit,
    )
    _print_json(memories)


def _run_paper_actions(args) -> None:
    service, _, _, _ = _paper_components(args)
    _print_json(
        service.store.list_corporate_action_events(
            args.account,
            limit=args.limit,
        )
    )


def _run_market_calendar(args) -> None:
    settings = _settings(args)
    calendar = ExchangeTradingCalendar(
        args.calendar or settings.market_calendar_name
    )
    payload = calendar.summary(args.start, args.end)
    _print_json(payload)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"calendar report: {output}")


def _run_market_validate(args) -> None:
    settings = _settings(args)
    if args.symbols:
        settings = replace(
            settings,
            workflow_symbols=tuple(
                dict.fromkeys(symbol.upper() for symbol in args.symbols)
            ),
        )
    data_dir = Path(args.data_dir or settings.paper_data_dir)
    payload = DailyWorkflow(settings, PROJECT_ROOT)._validate_local_data(data_dir)
    _print_json(payload)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"market validation report: {output}")


def _run_paper_approve(args) -> None:
    service, _, _, _ = _paper_components(args)
    order = service.approve_order(
        args.order_id,
        reviewer=args.reviewer,
        note=args.note,
    )
    _print_json(service.serialize_order(order))


def _run_paper_reject(args) -> None:
    service, _, _, _ = _paper_components(args)
    order = service.reject_order(
        args.order_id,
        reviewer=args.reviewer,
        note=args.note,
    )
    _print_json(service.serialize_order(order))


def _run_paper_cancel(args) -> None:
    service, _, _, _ = _paper_components(args)
    order = service.cancel_order(
        args.order_id,
        actor=args.actor,
        note=args.note,
    )
    _print_json(service.serialize_order(order))


def _run_paper_approve_all(args) -> None:
    service, _, _, _ = _paper_components(args)
    orders = service.approve_all(args.account, reviewer=args.reviewer)
    _print_json([service.serialize_order(order) for order in orders])


def _run_paper_session(args) -> None:
    service, settings, database, data_dir = _paper_components(args)
    result = run_session_with_lock(
        service,
        args.account,
        data_dir=data_dir,
        session_date=args.session,
        lock_path=settings.paper_lock_path,
        evidence_paths=args.evidence,
    )
    _print_json(result)
    print(f"paper database: {database}")
    print("external broker: disabled")


def _run_paper_next(args) -> None:
    service, settings, database, data_dir = _paper_components(args)
    result = run_next_with_lock(
        service,
        args.account,
        data_dir=data_dir,
        lock_path=args.lock_path or settings.paper_lock_path,
        evidence_paths=args.evidence,
    )
    _print_json(result)
    print(f"paper database: {database}")
    print("external broker: disabled")


def _run_workflow_plan(args) -> None:
    settings = _settings(args)
    workflow = DailyWorkflow(settings, PROJECT_ROOT)
    _print_json(
        workflow.plan(
            mode=args.mode,
            account_id=args.account,
        )
    )


def _run_workflow(args) -> None:
    settings = _settings(args)
    workflow = DailyWorkflow(settings, PROJECT_ROOT)
    result = workflow.execute(
        mode=args.mode,
        account_id=args.account,
        confirm_live=args.confirm_live,
        run_id=args.run_id,
        resume=args.resume,
    )
    _print_json(result)
    print(f"workflow artifact: {result['artifact']}")
    print(f"workflow state: {result['workflow_state']}")
    print(f"external requests enabled: {result['plan']['external_requests_enabled']}")
    print("external broker: disabled")


def _run_provider_smoke(args) -> None:
    settings = _settings(args)
    result = ProviderSmokeRunner(settings, PROJECT_ROOT).run(
        provider=args.provider,
        symbol=args.symbol,
        confirm_live=args.confirm_live,
        run_id=args.run_id,
    )
    _print_json(result)
    print("external broker: disabled")


def _run_provider_usage(args) -> None:
    settings = _settings(args)
    path = Path(args.database or settings.workflow_provider_usage_database)
    _print_json(ProviderUsageStore(path).summary(run_id=args.run_id))


def _run_provider_capabilities(args) -> None:
    _print_json(default_provider_registry().as_dict())


def _run_provider_health(args) -> None:
    settings = _settings(args)
    path = Path(args.database or settings.workflow_provider_health_database)
    store = ProviderHealthStore(path)
    _print_json(
        {
            "database": str(path),
            "providers": [item.as_dict() for item in store.list()],
        }
    )


def _run_provider_events(args) -> None:
    settings = _settings(args)
    path = Path(args.database or settings.workflow_provider_health_database)
    store = ProviderHealthStore(path)
    _print_json(
        {
            "database": str(path),
            "run_id": args.run_id,
            "events": store.events(run_id=args.run_id, limit=args.limit),
        }
    )


def _run_workflow_status(args) -> None:
    settings = _settings(args)
    state_path = Path(settings.workflow_artifact_dir) / args.run_id / "state.json"
    if not state_path.is_file():
        raise ValueError(f"workflow state not found: {state_path}")
    _print_json(json.loads(state_path.read_text(encoding="utf-8")))


def _run_experiment(args) -> None:
    prices, news, evidence = _provider(args)
    settings = _settings(args)
    result = run_experiment_suite(prices, settings, news, evidence)
    bundle = save_run_bundle(
        result,
        project_root=PROJECT_ROOT,
        settings=settings,
        input_files=_input_files(args),
        artifacts_root=args.runs_dir,
    )
    save_experiment(result, args.output, args.markdown)
    run_id = RunStore(args.database).save_experiment(result)
    for row in result["summary"]:
        print(
            f"{row['name']:<24} return={row['total_return']:>8.2%} "
            f"sharpe={row['sharpe']:>6.3f} drawdown={row['max_drawdown']:>7.2%} "
            f"trades={row['trade_count']:>3}"
        )
    print(
        f"audit={'PASS' if result['audit']['passed'] else 'FAIL'} "
        f"score={result['audit']['minimum_audit_score']:.3f} "
        f"citation={result['audit']['minimum_citation_coverage']:.2%}"
    )
    print(f"external evidence files: {len(evidence)}")
    print(f"run id: {run_id}")
    print(f"run bundle: {bundle['run_dir']}")
    print(f"html report: {bundle['html']}")
    print(f"database: {args.database}")


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _research_pack(args) -> tuple[EvidencePack, BacktestSettings]:
    prices, news, evidence = _provider(args)
    settings = _settings(args)
    decision_bar = prices.bars[-1]
    visible = prices.history(decision_bar.available_at)
    pack = FeatureEngine().build(visible, decision_bar.available_at)
    if news is not None:
        news.add_to_pack(pack)
    for provider in evidence:
        provider.add_to_pack(pack)
    return pack, settings


def _research_pipeline(
    settings: BacktestSettings,
    *,
    human_review_mode: str | None = None,
) -> MultiAgentResearchPipeline:
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
        langchain_trace_path=_project_path(settings.workflow_langchain_trace_path),
        langgraph_event_path=_project_path(settings.workflow_langgraph_event_path),
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


def _run_research(args) -> None:
    pack, settings = _research_pack(args)
    pipeline = _research_pipeline(
        settings,
        human_review_mode=args.human_review_mode,
    )
    if args.resume_decision and not args.thread_id:
        raise ValueError("--thread-id is required with --resume-decision")
    thread_id = args.thread_id or (
        f"research:{pack.symbol}:{uuid4().hex}"
    )
    human_response = None
    if args.resume_decision:
        human_response = {"decision": args.resume_decision}
        if args.resume_target is not None:
            human_response["target_weight"] = args.resume_target
    checkpointer = _project_path(
        args.checkpoint_database
        or settings.workflow_langgraph_checkpoint_database
    )
    try:
        result = pipeline.run_resumable(
            pack,
            current_weight=args.current_weight,
            node_runner=lambda _name, _model, function: function(),
            thread_id=thread_id,
            checkpointer_path=checkpointer,
            resume=bool(args.resume_decision),
            human_response=human_response,
        )
    except ResearchGraphInterrupted as exc:
        execution = exc.execution
        payload = {
            "status": "WAITING_FOR_HUMAN_REVIEW",
            "thread_id": execution.thread_id,
            "interrupts": list(execution.interrupt_payloads),
            "checkpoint_database": str(checkpointer),
            "external_broker": False,
        }
    else:
        execution = pipeline._last_graph_execution
        payload = result.model_dump(mode="json")
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
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"research artifact: {output}")
    print("execution: paper intent only; no broker order was submitted")


def _research_graph(args) -> None:
    settings = _settings(args)
    pack = EvidencePack(
        symbol=args.symbol.upper(),
        decision_time=datetime.now(timezone.utc),
    )
    runtime = LangGraphResearchRuntime(
        MockLLM(model=settings.llm_quick_model),
        MockLLM(model=settings.llm_deep_model),
        debate_rounds=settings.workflow_debate_rounds,
        risk_personas=settings.workflow_risk_personas,
        trace_path=_project_path(settings.workflow_langchain_trace_path),
        event_path=_project_path(settings.workflow_langgraph_event_path),
        retry_attempts=settings.workflow_langgraph_retry_attempts,
        node_timeout_seconds=settings.workflow_langgraph_node_timeout_seconds,
        cost_aware_routing=settings.workflow_langgraph_cost_aware_routing,
        hold_skip_confidence=settings.workflow_langgraph_hold_skip_confidence,
        human_review_mode=settings.workflow_langgraph_human_review_mode,
    )
    mermaid = runtime.mermaid(pack)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"research graph: {output}")


def _portfolio_graph(args) -> None:
    settings = _settings(args)
    runtime = LangGraphPortfolioSupervisorRuntime(
        MockLLM(model=settings.llm_quick_model),
        MockLLM(model=settings.llm_deep_model),
        trace_path=_project_path(settings.workflow_langchain_trace_path),
        event_path=_project_path(settings.workflow_langgraph_event_path),
        retry_attempts=settings.workflow_langgraph_retry_attempts,
        max_gross_target=settings.max_gross_exposure,
        max_positions=settings.max_positions,
        high_correlation_threshold=(
            settings.workflow_portfolio_high_correlation_threshold
        ),
        cluster_gross_cap=settings.workflow_portfolio_cluster_gross_cap,
    )
    mermaid = runtime.mermaid()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"portfolio graph: {output}")


def _research_parent_graph(args) -> None:
    settings = _settings(args)
    runtime = LangGraphResearchParentRuntime(
        event_path=_project_path(settings.workflow_langgraph_event_path)
    )
    mermaid = runtime.mermaid()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"research parent graph: {output}")


def _data_provider_graph(args) -> None:
    settings = _settings(args)
    runtime = LangGraphDataProviderRuntime(
        event_path=_project_path(settings.workflow_langgraph_event_path)
    )
    mermaid = runtime.mermaid()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"data provider graph: {output}")


def _decision_graph(args) -> None:
    settings = _settings(args)
    runtime = LangGraphDeterministicDecisionRuntime(
        PortfolioPlanner(settings),
        event_path=_project_path(settings.workflow_langgraph_event_path),
    )
    mermaid = runtime.mermaid()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"decision graph: {output}")


def _workflow_core_graph(args) -> None:
    settings = _settings(args)
    runtime = LangGraphWorkflowCoreRuntime(
        event_path=_project_path(settings.workflow_langgraph_event_path)
    )
    mermaid = runtime.mermaid()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    print(mermaid)
    print(f"workflow core graph: {output}")


def _research_thread_status(args) -> None:
    settings = _settings(args)
    thread = args.thread_id.upper()
    default_database = (
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
    database = _project_path(args.database or default_database)
    print(
        json.dumps(
            inspect_research_thread(database, args.thread_id),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def _run_benchmark(args) -> None:
    result = run_scenario_benchmark(args.scenario_dir, _settings(args))
    save_benchmark(result, args.output, args.markdown)
    print(
        f"scenarios={result['scenario_count']} audits={result['all_audits_passed']} "
        f"full_agent_positive={result['full_agent_positive_scenarios']}"
    )
    for row in result["aggregate"]:
        print(
            f"{row['name']:<24} avg_return={row['average_return']:>8.2%} "
            f"avg_sharpe={row['average_sharpe']:>6.3f} "
            f"worst_dd={row['worst_drawdown']:>7.2%} "
            f"positive={row['positive_scenarios']}/{row['scenario_count']}"
        )
    print(f"benchmark JSON: {args.output}")
    print(f"benchmark Markdown: {args.markdown}")


def _list_runs(args) -> None:
    rows = RunStore(args.database).list_runs(args.limit)
    if not rows:
        print("no stored runs")
        return
    for row in rows:
        print(
            f"{row['run_id']} symbol={row['symbol']} audit={bool(row['audit_passed'])} "
            f"dirty={bool(row['git_dirty'])} git={str(row['git_revision'])[:10]}"
        )


def _show_run(args) -> None:
    result = RunStore(args.database).get_run(args.run_id)
    if result is None:
        raise SystemExit(f"run not found: {args.run_id}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _fetch_market(args) -> None:
    bars = TwelveDataClient().fetch_daily_bars(
        args.symbol,
        start_date=args.start,
        end_date=args.end,
        outputsize=args.outputsize,
        cache_ttl_seconds=args.cache_ttl,
        force_refresh=args.force,
    )
    count = write_bars_csv(bars, args.output)
    print(
        f"provider=twelve_data symbol={args.symbol.upper()} rows={count} "
        f"first={bars[0].timestamp.date()} last={bars[-1].timestamp.date()} output={args.output}"
    )


def _fetch_news(args) -> None:
    events = AlphaVantageNewsClient().fetch_news(
        args.symbol,
        time_from=args.start,
        time_to=args.end,
        limit=args.limit,
        sort=args.sort,
        cache_ttl_seconds=args.cache_ttl,
        force_refresh=args.force,
    )
    count = write_news_jsonl(events, args.output)
    print(f"provider=alpha_vantage symbol={args.symbol.upper()} rows={count} output={args.output}")


def _fetch_fred(args) -> None:
    client = FredClient()
    records = []
    for series_id in args.series:
        records.extend(
            client.fetch_initial_release_records(
                series_id,
                observation_start=args.start,
                observation_end=args.end,
                symbol=args.symbol,
                detail=args.detail,
                cache_ttl_seconds=args.cache_ttl,
                force_refresh=args.force,
            )
        )
    records.sort(key=lambda item: (item.available_at, item.series_id, item.timestamp))
    count = write_evidence_jsonl(records, args.output)
    print(
        f"provider=fred series={','.join(value.upper() for value in args.series)} "
        f"rows={count} point_in_time=initial_release output={args.output}"
    )


def _fetch_sec(args) -> None:
    concepts = tuple(args.concept) if args.concept else DEFAULT_US_GAAP_CONCEPTS
    forms = tuple(args.form) if args.form else ("10-K", "10-Q", "10-K/A", "10-Q/A")
    records = SecEdgarClient().fetch_fundamental_records(
        args.ticker,
        concepts=concepts,
        forms=forms,
        filed_start=args.start,
        filed_end=args.end,
        cache_ttl_seconds=args.cache_ttl,
        force_refresh=args.force,
    )
    count = write_evidence_jsonl(records, args.output)
    print(
        f"provider=sec_edgar ticker={args.ticker.upper()} rows={count} "
        f"concepts={len(concepts)} output={args.output}"
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--csv", required=True, help="OHLCV CSV file")
    parser.add_argument("--news", help="optional point-in-time JSONL news file")
    parser.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="point-in-time macro/fundamental JSONL; may be repeated",
    )
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--config", help="YAML configuration file")
    parser.add_argument("--cash", type=float, help="override initial cash")


def _add_fetch_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-ttl", type=int, default=3600, help="HTTP cache lifetime in seconds")
    parser.add_argument("--force", action="store_true", help="ignore the HTTP cache")


def _add_paper_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="demo-paper")
    parser.add_argument("--database")
    parser.add_argument("--data-dir")
    parser.add_argument("--config", default="config/default.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="TradeLab-Agent course trading system")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="run one agent backtest")
    _add_common(backtest)
    backtest.add_argument("--output", default="artifacts/backtest.json")
    backtest.set_defaults(handler=_run_backtest)

    multi = subparsers.add_parser(
        "multi-backtest",
        help="run one synchronized multi-asset portfolio backtest",
    )
    multi.add_argument("--data-dir", required=True)
    multi.add_argument("--symbols", nargs="+", required=True)
    multi.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="shared point-in-time macro/fundamental JSONL; may be repeated",
    )
    multi.add_argument("--config", help="YAML configuration file")
    multi.add_argument("--cash", type=float, help="override initial cash")
    multi.add_argument("--output", default="artifacts/multi_backtest.json")
    multi.set_defaults(handler=_run_multi_backtest)

    market_calendar = subparsers.add_parser(
        "market-calendar",
        help="show exchange sessions and early closes without external requests",
    )
    market_calendar.add_argument("--start", required=True, help="YYYY-MM-DD")
    market_calendar.add_argument("--end", required=True, help="YYYY-MM-DD")
    market_calendar.add_argument("--calendar")
    market_calendar.add_argument("--config", default="config/default.yaml")
    market_calendar.add_argument("--output")
    market_calendar.set_defaults(handler=_run_market_calendar)

    market_validate = subparsers.add_parser(
        "market-validate",
        help="validate synchronized bars, exchange sessions and corporate actions",
    )
    market_validate.add_argument("--data-dir")
    market_validate.add_argument("--symbols", nargs="+")
    market_validate.add_argument("--config", default="config/default.yaml")
    market_validate.add_argument("--output")
    market_validate.set_defaults(handler=_run_market_validate)

    paper_init = subparsers.add_parser(
        "paper-init",
        help="create a persistent internal paper account",
    )
    _add_paper_common(paper_init)
    paper_init.add_argument("--name", default="TradeLab Paper Account")
    paper_init.add_argument("--symbols", nargs="+", required=True)
    paper_init.add_argument("--cash", type=float)
    paper_init.add_argument(
        "--approval-policy",
        choices=[value.value for value in ApprovalPolicy],
    )
    paper_init.set_defaults(handler=_run_paper_init)

    paper_account = subparsers.add_parser(
        "paper-account",
        help="show persistent paper account state",
    )
    _add_paper_common(paper_account)
    paper_account.set_defaults(handler=_run_paper_account)

    paper_dashboard = subparsers.add_parser(
        "paper-dashboard",
        help="render a local HTML paper account dashboard",
    )
    _add_paper_common(paper_dashboard)
    paper_dashboard.add_argument(
        "--output",
        default="artifacts/paper_dashboard.html",
    )
    paper_dashboard.set_defaults(handler=_run_paper_dashboard)

    paper_orders = subparsers.add_parser(
        "paper-orders",
        help="list paper order queue entries",
    )
    _add_paper_common(paper_orders)
    paper_orders.add_argument(
        "--status",
        action="append",
        choices=[value.value for value in OrderStatus],
        default=[],
    )
    paper_orders.add_argument("--limit", type=int, default=200)
    paper_orders.set_defaults(handler=_run_paper_orders)

    paper_memories = subparsers.add_parser(
        "paper-memories",
        help="list immutable decisions and matured outcome reflections",
    )
    _add_paper_common(paper_memories)
    paper_memories.add_argument("--symbol")
    paper_memories.add_argument(
        "--outcome-status",
        choices=("PENDING", "MATURED"),
    )
    paper_memories.add_argument("--limit", type=int, default=100)
    paper_memories.set_defaults(handler=_run_paper_memories)

    paper_actions = subparsers.add_parser(
        "paper-actions",
        help="list idempotently posted split and dividend events",
    )
    _add_paper_common(paper_actions)
    paper_actions.add_argument("--limit", type=int, default=100)
    paper_actions.set_defaults(handler=_run_paper_actions)

    paper_approve = subparsers.add_parser(
        "paper-approve",
        help="manually approve one queued paper order",
    )
    _add_paper_common(paper_approve)
    paper_approve.add_argument("order_id")
    paper_approve.add_argument("--reviewer", required=True)
    paper_approve.add_argument("--note", default="")
    paper_approve.set_defaults(handler=_run_paper_approve)

    paper_reject = subparsers.add_parser(
        "paper-reject",
        help="manually reject one queued paper order",
    )
    _add_paper_common(paper_reject)
    paper_reject.add_argument("order_id")
    paper_reject.add_argument("--reviewer", required=True)
    paper_reject.add_argument("--note", default="")
    paper_reject.set_defaults(handler=_run_paper_reject)

    paper_cancel = subparsers.add_parser(
        "paper-cancel",
        help="cancel one pending or approved paper order",
    )
    _add_paper_common(paper_cancel)
    paper_cancel.add_argument("order_id")
    paper_cancel.add_argument("--actor", required=True)
    paper_cancel.add_argument("--note", default="")
    paper_cancel.set_defaults(handler=_run_paper_cancel)

    paper_approve_all = subparsers.add_parser(
        "paper-approve-all",
        help="approve all currently pending orders for an account",
    )
    _add_paper_common(paper_approve_all)
    paper_approve_all.add_argument("--reviewer", required=True)
    paper_approve_all.set_defaults(handler=_run_paper_approve_all)

    paper_run = subparsers.add_parser(
        "paper-run",
        help="run one explicit synchronized paper session",
    )
    _add_paper_common(paper_run)
    paper_run.add_argument("--session", required=True, help="YYYY-MM-DD")
    paper_run.add_argument("--evidence", action="append", default=[])
    paper_run.set_defaults(handler=_run_paper_session)

    paper_next = subparsers.add_parser(
        "paper-next",
        help="run the next unprocessed paper session under a process lock",
    )
    _add_paper_common(paper_next)
    paper_next.add_argument("--lock-path")
    paper_next.add_argument("--evidence", action="append", default=[])
    paper_next.set_defaults(handler=_run_paper_next)

    workflow_plan = subparsers.add_parser(
        "workflow-plan",
        help="print the complete data/research/paper workflow without executing it",
    )
    workflow_plan.add_argument(
        "--mode",
        choices=("dry_run", "offline", "live"),
    )
    workflow_plan.add_argument("--account")
    workflow_plan.add_argument("--config", default="config/default.yaml")
    workflow_plan.set_defaults(handler=_run_workflow_plan)

    workflow_run = subparsers.add_parser(
        "workflow-run",
        help="execute the daily workflow; dry_run is the safe default",
    )
    workflow_run.add_argument(
        "--mode",
        choices=("dry_run", "offline", "live"),
    )
    workflow_run.add_argument("--account")
    workflow_run.add_argument(
        "--run-id",
        help="explicit run identifier; required with --resume",
    )
    workflow_run.add_argument(
        "--resume",
        action="store_true",
        help="resume completed nodes from the matching run checkpoint",
    )
    workflow_run.add_argument(
        "--confirm-live",
        action="store_true",
        help="required before any external data or LLM request is allowed",
    )
    workflow_run.add_argument("--config", default="config/default.yaml")
    workflow_run.set_defaults(handler=_run_workflow)

    provider_smoke = subparsers.add_parser(
        "provider-smoke",
        help="run one minimal real request per selected external provider",
    )
    provider_smoke.add_argument(
        "--provider",
        choices=(
            "all",
            "twelve_data",
            "alpha_vantage",
            "fred",
            "sec_edgar",
            "deepseek",
            "zhipu",
            "twelve_data",
            "fred",
        ),
        default="all",
    )
    provider_smoke.add_argument("--symbol", default="AAPL")
    provider_smoke.add_argument("--run-id")
    provider_smoke.add_argument(
        "--confirm-live",
        action="store_true",
        help="required before the credential-free network gate and real requests",
    )
    provider_smoke.add_argument("--config", default="config/default.yaml")
    provider_smoke.set_defaults(handler=_run_provider_smoke)

    provider_usage = subparsers.add_parser(
        "provider-usage",
        help="show secret-free provider call, status and quota-unit summaries",
    )
    provider_usage.add_argument("--run-id")
    provider_usage.add_argument("--database")
    provider_usage.add_argument("--config", default="config/default.yaml")
    provider_usage.set_defaults(handler=_run_provider_usage)

    provider_capabilities = subparsers.add_parser(
        "provider-capabilities",
        help="show the deterministic Provider Graph capability registry",
    )
    provider_capabilities.set_defaults(handler=_run_provider_capabilities)

    provider_health = subparsers.add_parser(
        "provider-health",
        help="show persistent provider health and cooldown state",
    )
    provider_health.add_argument("--database")
    provider_health.add_argument("--config", default="config/default.yaml")
    provider_health.set_defaults(handler=_run_provider_health)

    provider_events = subparsers.add_parser(
        "provider-events",
        help="show bounded provider health, fallback and conflict events",
    )
    provider_events.add_argument("--run-id")
    provider_events.add_argument("--limit", type=int, default=200)
    provider_events.add_argument("--database")
    provider_events.add_argument("--config", default="config/default.yaml")
    provider_events.set_defaults(handler=_run_provider_events)

    workflow_status = subparsers.add_parser(
        "workflow-status",
        help="show checkpoint state for one workflow run",
    )
    workflow_status.add_argument("run_id")
    workflow_status.add_argument("--config", default="config/default.yaml")
    workflow_status.set_defaults(handler=_run_workflow_status)

    experiment = subparsers.add_parser("experiment", help="run baselines and ablations")
    _add_common(experiment)
    experiment.add_argument("--output", default="artifacts/experiment.json")
    experiment.add_argument("--markdown", default="artifacts/experiment.md")
    experiment.add_argument("--runs-dir", default="artifacts/runs")
    experiment.add_argument("--database", default="artifacts/tradinglab.db")
    experiment.set_defaults(handler=_run_experiment)

    research = subparsers.add_parser(
        "research",
        help="run the structured analyst/debate/manager/trader pipeline",
    )
    _add_common(research)
    research.add_argument(
        "--current-weight",
        type=float,
        default=0.0,
        help="current paper portfolio weight for the research manager",
    )
    research.add_argument(
        "--human-review-mode",
        choices=("paper_queue", "interrupt_directional"),
        help="override the configured LangGraph human-review mode",
    )
    research.add_argument(
        "--thread-id",
        help="durable LangGraph thread; required when resuming an interrupt",
    )
    research.add_argument(
        "--checkpoint-database",
        help="override the LangGraph SQLite checkpoint database",
    )
    research.add_argument(
        "--resume-decision",
        choices=("approve", "reject", "reduce"),
        help="resume an interrupted graph thread with a human decision",
    )
    research.add_argument(
        "--resume-target",
        type=float,
        help="reduced BUY target used with --resume-decision reduce",
    )
    research.add_argument("--output", default="artifacts/research.json")
    research.set_defaults(handler=_run_research)

    research_graph = subparsers.add_parser(
        "research-graph",
        help="export the native LangGraph research topology as Mermaid",
    )
    research_graph.add_argument("--symbol", default="DEMO")
    research_graph.add_argument("--config", default="config/default.yaml")
    research_graph.add_argument("--output", default="artifacts/research_graph.mmd")
    research_graph.set_defaults(handler=_research_graph)

    portfolio_graph = subparsers.add_parser(
        "portfolio-graph",
        help="export the cross-asset LangGraph portfolio supervisor as Mermaid",
    )
    portfolio_graph.add_argument("--config", default="config/default.yaml")
    portfolio_graph.add_argument(
        "--output",
        default="artifacts/portfolio_supervisor_graph.mmd",
    )
    portfolio_graph.set_defaults(handler=_portfolio_graph)

    research_parent_graph = subparsers.add_parser(
        "research-parent-graph",
        help="export the top-level dynamic research parent graph as Mermaid",
    )
    research_parent_graph.add_argument("--config", default="config/default.yaml")
    research_parent_graph.add_argument(
        "--output",
        default="artifacts/research_parent_graph.mmd",
    )
    research_parent_graph.set_defaults(handler=_research_parent_graph)

    data_provider_graph = subparsers.add_parser(
        "data-provider-graph",
        help="export the checkpointed Provider Graph as Mermaid",
    )
    data_provider_graph.add_argument(
        "--output",
        default="artifacts/data_provider_graph.mmd",
    )
    data_provider_graph.add_argument("--config", default="config/default.yaml")
    data_provider_graph.set_defaults(handler=_data_provider_graph)

    workflow_core_graph = subparsers.add_parser(
        "workflow-core-graph",
        help="export the v0.15 workflow core graph as Mermaid",
    )
    workflow_core_graph.add_argument("--config", default="config/default.yaml")
    workflow_core_graph.add_argument(
        "--output",
        default="artifacts/workflow_core_graph.mmd",
    )
    workflow_core_graph.set_defaults(handler=_workflow_core_graph)

    decision_graph = subparsers.add_parser(
        "decision-graph",
        help="export the deterministic portfolio decision graph as Mermaid",
    )
    decision_graph.add_argument("--config", default="config/default.yaml")
    decision_graph.add_argument(
        "--output",
        default="artifacts/decision_graph.mmd",
    )
    decision_graph.set_defaults(handler=_decision_graph)

    research_thread = subparsers.add_parser(
        "research-thread-status",
        help="inspect one durable LangGraph research thread without model output",
    )
    research_thread.add_argument("thread_id")
    research_thread.add_argument("--database")
    research_thread.add_argument("--config", default="config/default.yaml")
    research_thread.set_defaults(handler=_research_thread_status)

    benchmark = subparsers.add_parser("benchmark", help="run deterministic market scenarios")
    benchmark.add_argument("--scenario-dir", default="data/scenarios")
    benchmark.add_argument("--config", help="YAML configuration file")
    benchmark.add_argument("--cash", type=float, help="override initial cash")
    benchmark.add_argument("--output", default="artifacts/scenario_benchmark.json")
    benchmark.add_argument("--markdown", default="artifacts/scenario_benchmark.md")
    benchmark.set_defaults(handler=_run_benchmark)

    runs = subparsers.add_parser("runs", help="list stored experiment runs")
    runs.add_argument("--database", default="artifacts/tradinglab.db")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(handler=_list_runs)

    show = subparsers.add_parser("show-run", help="print one stored experiment")
    show.add_argument("run_id")
    show.add_argument("--database", default="artifacts/tradinglab.db")
    show.set_defaults(handler=_show_run)

    market = subparsers.add_parser("fetch-market", help="download daily OHLCV from Twelve Data")
    market.add_argument("--symbol", required=True)
    market.add_argument("--start")
    market.add_argument("--end")
    market.add_argument("--outputsize", type=int, default=5000)
    market.add_argument("--output", required=True)
    _add_fetch_common(market)
    market.set_defaults(handler=_fetch_market)

    news = subparsers.add_parser("fetch-news", help="download point-in-time news from Alpha Vantage")
    news.add_argument("--symbol", required=True)
    news.add_argument("--start", help="YYYY-MM-DD or YYYYMMDDTHHMM")
    news.add_argument("--end", help="YYYY-MM-DD or YYYYMMDDTHHMM")
    news.add_argument("--limit", type=int, default=200)
    news.add_argument("--sort", choices=("EARLIEST", "LATEST", "RELEVANCE"), default="EARLIEST")
    news.add_argument("--output", required=True)
    _add_fetch_common(news)
    news.set_defaults(handler=_fetch_news)

    fred = subparsers.add_parser("fetch-fred", help="download FRED initial-release macro evidence")
    fred.add_argument(
        "--series",
        required=True,
        nargs="+",
        help="one or more series, for example DGS10 CPIAUCSL UNRATE",
    )
    fred.add_argument("--start")
    fred.add_argument("--end")
    fred.add_argument("--symbol", default="MACRO")
    fred.add_argument("--detail")
    fred.add_argument("--output", required=True)
    _add_fetch_common(fred)
    fred.set_defaults(handler=_fetch_fred)

    sec = subparsers.add_parser("fetch-sec", help="download SEC EDGAR company facts")
    sec.add_argument("--ticker", required=True)
    sec.add_argument("--start", help="minimum filing date")
    sec.add_argument("--end", help="maximum filing date")
    sec.add_argument("--concept", action="append", help="US-GAAP concept; may be repeated")
    sec.add_argument("--form", action="append", help="filing form; may be repeated")
    sec.add_argument("--output", required=True)
    _add_fetch_common(sec)
    sec.set_defaults(handler=_fetch_sec)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
