from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.benchmark_suite import run_scenario_benchmark, save_benchmark
from tradinglab_agents.evaluation.experiments import (
    run_experiment_suite,
    save_experiment,
    save_run_bundle,
)
from tradinglab_agents.storage.run_store import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _provider(args):
    prices = LocalCsvProvider(args.csv, args.symbol)
    news = LocalNewsProvider(args.news) if getattr(args, "news", None) else None
    return prices, news


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
    return values


def _run_backtest(args) -> None:
    prices, news = _provider(args)
    result = BacktestEngine(_settings(args)).run(prices, news)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result["metrics"], indent=2))
    print(f"full report: {output}")


def _run_experiment(args) -> None:
    prices, news = _provider(args)
    settings = _settings(args)
    result = run_experiment_suite(prices, settings, news)
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
    print(f"run id: {run_id}")
    print(f"run bundle: {bundle['run_dir']}")
    print(f"html report: {bundle['html']}")
    print(f"database: {args.database}")


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


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--csv", required=True, help="OHLCV CSV file")
    parser.add_argument("--news", help="optional point-in-time JSONL news file")
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--config", help="YAML configuration file")
    parser.add_argument("--cash", type=float, help="override initial cash")


def main() -> None:
    parser = argparse.ArgumentParser(description="TradeLab-Agent course trading system")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="run one agent backtest")
    _add_common(backtest)
    backtest.add_argument("--output", default="artifacts/backtest.json")
    backtest.set_defaults(handler=_run_backtest)

    experiment = subparsers.add_parser("experiment", help="run baselines and ablations")
    _add_common(experiment)
    experiment.add_argument("--output", default="artifacts/experiment.json")
    experiment.add_argument("--markdown", default="artifacts/experiment.md")
    experiment.add_argument("--runs-dir", default="artifacts/runs")
    experiment.add_argument("--database", default="artifacts/tradinglab.db")
    experiment.set_defaults(handler=_run_experiment)

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

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
