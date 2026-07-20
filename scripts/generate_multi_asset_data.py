from __future__ import annotations

import csv
import json
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "multi_sample"
SYMBOLS = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
SEED = 20260720

ASSET_PARAMETERS = {
    "SPY": {"price": 250.0, "beta": 1.00, "alpha": 0.00005, "idio": 0.0030},
    "QQQ": {"price": 160.0, "beta": 1.16, "alpha": 0.00013, "idio": 0.0040},
    "AAPL": {"price": 42.0, "beta": 1.12, "alpha": 0.00015, "idio": 0.0060},
    "MSFT": {"price": 86.0, "beta": 1.08, "alpha": 0.00016, "idio": 0.0052},
    "NVDA": {"price": 12.0, "beta": 1.42, "alpha": 0.00028, "idio": 0.0090},
}


def business_days(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def regime_parameters(index: int) -> tuple[float, float, str]:
    phase = index % 1040
    if phase < 260:
        return 0.00035, 0.0070, "expansion"
    if phase < 390:
        return -0.00065, 0.0140, "contraction"
    if phase < 650:
        return 0.00050, 0.0090, "recovery"
    if phase < 820:
        return 0.00002, 0.0060, "sideways"
    return 0.00020, 0.0120, "volatile"


def headline_for(symbol: str, regime: str, index: int) -> tuple[str, str]:
    if regime in {"expansion", "recovery"}:
        return (
            f"{symbol} reports strong growth and upgraded demand outlook",
            "Management cites product momentum, improving margins and a new partnership.",
        )
    if regime == "contraction":
        return (
            f"{symbol} warns of weaker demand and lower near-term guidance",
            "Analysts discuss a revenue miss, cost pressure and a possible estimate cut.",
        )
    if regime == "volatile":
        direction = "surge" if index % 2 == 0 else "decline"
        return (
            f"{symbol} sees volatile trading after a sharp {direction}",
            "The move follows mixed expectations and unusually high market uncertainty.",
        )
    return (
        f"{symbol} outlook remains stable amid balanced market signals",
        "Demand and profitability indicators are mixed with no major guidance change.",
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    random_source = random.Random(SEED)
    prices = {
        symbol: float(ASSET_PARAMETERS[symbol]["price"]) for symbol in SYMBOLS
    }
    rows: dict[str, list[list[object]]] = {symbol: [] for symbol in SYMBOLS}
    news: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in SYMBOLS}

    days = list(business_days(date(2018, 1, 2), date(2025, 12, 31)))
    for index, day in enumerate(days):
        market_drift, market_volatility, regime = regime_parameters(index)
        cyclical = 0.0018 * math.sin(index / 23.0) + 0.0010 * math.sin(index / 71.0)
        market_shock = random_source.gauss(0.0, market_volatility)
        tech_factor = random_source.gauss(0.0, market_volatility * 0.45)

        for symbol in SYMBOLS:
            params = ASSET_PARAMETERS[symbol]
            technology_loading = 0.0 if symbol == "SPY" else 0.55
            idiosyncratic = random_source.gauss(0.0, float(params["idio"]))
            daily_return = (
                float(params["alpha"])
                + float(params["beta"]) * (market_drift + cyclical + market_shock)
                + technology_loading * tech_factor
                + idiosyncratic
            )
            daily_return = max(-0.18, min(0.18, daily_return))
            previous_close = prices[symbol]
            overnight = random_source.gauss(0.0, market_volatility * 0.18)
            open_price = max(1.0, previous_close * (1.0 + overnight))
            close_price = max(1.0, open_price * (1.0 + daily_return))
            intraday_range = abs(random_source.gauss(0.0, market_volatility * 0.50))
            high = max(open_price, close_price) * (1.0 + intraday_range)
            low = min(open_price, close_price) * (1.0 - intraday_range)
            volume_base = 70_000_000 if symbol in {"SPY", "QQQ"} else 45_000_000
            volume = volume_base * (
                0.75
                + random_source.random() * 0.50
                + abs(daily_return) * 8.0
            )
            open_at = datetime.combine(day, time(9, 30))
            close_at = datetime.combine(day, time(16, 0))
            rows[symbol].append(
                [
                    close_at.isoformat(),
                    open_at.isoformat(),
                    open_price,
                    high,
                    low,
                    close_price,
                    volume,
                    close_at.isoformat(),
                ]
            )
            prices[symbol] = close_price

            if index % 42 == SYMBOLS.index(symbol) * 3:
                headline, summary = headline_for(symbol, regime, index)
                published = datetime.combine(day, time(12, 30))
                available = datetime.combine(day, time(12, 35))
                news[symbol].append(
                    {
                        "event_id": f"{symbol.lower()}-{index:04d}",
                        "symbol": symbol,
                        "published_at": published.isoformat(),
                        "available_at": available.isoformat(),
                        "headline": headline,
                        "summary": summary,
                        "source": "synthetic_multi_asset_fixture",
                    }
                )

    header = [
        "timestamp",
        "open_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "available_at",
    ]
    for symbol in SYMBOLS:
        price_path = OUTPUT_DIR / f"{symbol}.csv"
        news_path = OUTPUT_DIR / f"{symbol}_news.jsonl"
        with price_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows[symbol])
        with news_path.open("w", encoding="utf-8") as handle:
            for item in news[symbol]:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(
            f"{symbol}: prices={price_path} rows={len(rows[symbol])} "
            f"news={news_path} rows={len(news[symbol])}"
        )

    manifest = {
        "kind": "synthetic_multi_asset_fixture",
        "symbols": list(SYMBOLS),
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "sessions": len(days),
        "seed": SEED,
        "warning": "Synthetic data for deterministic software testing; not historical market data.",
    }
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
