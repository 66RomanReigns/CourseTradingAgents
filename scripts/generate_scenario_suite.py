from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar


SCENARIOS = {
    "bull": {"drift": 0.0011, "vol": 0.007, "mean_reversion": 0.00, "sentiment": 0.55},
    "bear": {"drift": -0.0009, "vol": 0.010, "mean_reversion": 0.00, "sentiment": -0.55},
    "sideways": {"drift": 0.0000, "vol": 0.008, "mean_reversion": 0.08, "sentiment": 0.00},
    "volatile": {"drift": 0.0001, "vol": 0.023, "mean_reversion": 0.02, "sentiment": -0.05},
}


def generate_scenario(name: str, output_dir: Path, rows: int, seed: int) -> tuple[Path, Path]:
    params = SCENARIOS[name]
    rng = random.Random(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{name}.csv"
    news_path = output_dir / f"{name}_news.jsonl"
    price = 100.0
    anchor = 100.0
    calendar = ExchangeTradingCalendar("XNYS")
    sessions = calendar.sessions(date(2023, 1, 3), date(2025, 12, 31))[:rows]
    if len(sessions) < rows:
        raise ValueError(f"calendar returned only {len(sessions)} sessions for {rows} rows")
    csv_rows = []
    news_rows = []

    for index, session in enumerate(sessions):
        open_at = session.open_at
        close_at = session.close_at
        reversion = params["mean_reversion"] * (anchor / price - 1.0)
        cycle = 0.0015 * math.sin(index / 11.0)
        shock = rng.gauss(0.0, params["vol"])
        open_price = max(5.0, price * (1.0 + rng.gauss(0.0, params["vol"] * 0.20)))
        close = max(5.0, open_price * (1.0 + params["drift"] + reversion + cycle + shock))
        high = max(open_price, close) * (1.0 + abs(rng.gauss(0.0, params["vol"] * 0.35)))
        low = min(open_price, close) * (1.0 - abs(rng.gauss(0.0, params["vol"] * 0.35)))
        volume = 900_000 * (1.0 + abs(shock) * 25 + rng.random() * 0.25)
        csv_rows.append(
            [
                close_at.isoformat(),
                open_at.isoformat(),
                open_price,
                high,
                low,
                close,
                volume,
                close_at.isoformat(),
            ]
        )
        price = close
        if index % 20 == 0:
            event_noise = rng.uniform(-0.15, 0.15)
            sentiment = max(-1.0, min(1.0, params["sentiment"] + event_noise))
            if sentiment > 0.15:
                summary = "strong growth, profit beat and analyst upgrade"
            elif sentiment < -0.15:
                summary = "weak demand, earnings decline and risk warning"
            else:
                summary = "mixed outlook with limited directional evidence"
            news_rows.append(
                {
                    "event_id": f"{name}-{index:03d}",
                    "symbol": name.upper(),
                    "published_at": (close_at - timedelta(hours=2)).isoformat(),
                    "available_at": (close_at - timedelta(hours=2)).isoformat(),
                    "headline": f"Synthetic {name} regime event {index}",
                    "summary": summary,
                    "source": "scenario_generator",
                }
            )

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["timestamp", "open_at", "open", "high", "low", "close", "volume", "available_at"]
        )
        writer.writerows(csv_rows)
    with news_path.open("w", encoding="utf-8") as handle:
        for row in news_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return csv_path, news_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic market-regime scenarios")
    parser.add_argument("--output-dir", default="data/scenarios")
    parser.add_argument("--rows", type=int, default=280)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    for offset, name in enumerate(SCENARIOS):
        csv_path, news_path = generate_scenario(name, output_dir, args.rows, args.seed + offset)
        print(f"{name}: {csv_path} {news_path}")


if __name__ == "__main__":
    main()
