from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.sec_edgar import DEFAULT_US_GAAP_CONCEPTS, SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import (
    write_bars_csv,
    write_evidence_jsonl,
    write_news_jsonl,
)
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
    except (ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
