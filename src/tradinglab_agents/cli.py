from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradinglab_agents.config import BacktestSettings, load_settings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.experiments import run_experiment_suite, save_experiment


def _provider(args):
    prices = LocalCsvProvider(args.csv, args.symbol)
    news = LocalNewsProvider(args.news) if getattr(args, "news", None) else None
    return prices, news


def _settings(args) -> BacktestSettings:
    settings = load_settings(args.config) if getattr(args, "config", None) else BacktestSettings()
    if getattr(args, "cash", None) is not None:
        settings = BacktestSettings(**{**settings.__dict__, "initial_cash": args.cash})
    return settings


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
    result = run_experiment_suite(prices, _settings(args), news)
    save_experiment(result, args.output, args.markdown)
    for row in result["summary"]:
        print(
            f"{row['name']:<24} return={row['total_return']:>8.2%} "
            f"sharpe={row['sharpe']:>6.3f} drawdown={row['max_drawdown']:>7.2%} "
            f"trades={row['trade_count']:>3}"
        )
    print(f"json report: {args.output}")
    print(f"markdown report: {args.markdown}")


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
    experiment.set_defaults(handler=_run_experiment)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
