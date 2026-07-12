from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.backtest import BacktestEngine


def main() -> None:
    parser = argparse.ArgumentParser(description="TradeLab-Agent offline paper-trading backtest")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--output", default="artifacts/backtest.json")
    args = parser.parse_args()

    result = BacktestEngine(args.cash).run(LocalCsvProvider(args.csv, args.symbol))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("symbol", "final_equity", "total_return", "buy_hold_return")}, indent=2))
    print(f"full report: {output}")


if __name__ == "__main__":
    main()
