from __future__ import annotations

import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar

SEED = 7
SYMBOL = "DEMO"
random.seed(SEED)
root = Path(__file__).resolve().parents[1]
price_path = root / "data" / "sample" / "demo.csv"
news_path = root / "data" / "sample" / "demo_news.jsonl"
price_path.parent.mkdir(parents=True, exist_ok=True)


calendar = ExchangeTradingCalendar("XNYS")
sessions = calendar.sessions(date(2024, 1, 2), date(2025, 6, 30))[:260]
price = 100.0
rows = []
news = []
for index, session in enumerate(sessions):
    day = session.session_date
    if index < 90:
        drift, regime = 0.0010, "growth"
    elif index < 165:
        drift, regime = -0.0011, "decline"
    else:
        drift, regime = 0.0013, "recovery"
    cyclical = 0.0025 * math.sin(index / 8)
    shock = random.gauss(0, 0.010)
    open_price = price * (1 + random.gauss(0, 0.0025))
    close = max(10.0, open_price * (1 + drift + cyclical + shock))
    high = max(open_price, close) * (1 + abs(random.gauss(0, 0.005)))
    low = min(open_price, close) * (1 - abs(random.gauss(0, 0.005)))
    volume = 1_000_000 * (1 + abs(shock) * 18 + random.random() * 0.25)
    open_at = session.open_at
    close_at = session.close_at
    rows.append(
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

    if index % 12 == 4:
        if regime == "growth":
            headline = "Company reports strong growth and record product demand"
            summary = "Analysts upgrade expectations after a profit beat and new partnership."
        elif regime == "decline":
            headline = "Company issues weak outlook after revenue miss"
            summary = "Management warning cites demand decline and possible guidance cut."
        else:
            headline = "New product launch supports recovery and profit growth"
            summary = "Partnership approval and stronger demand improve the outlook."
        published = min(
            session.open_at + timedelta(hours=3),
            session.close_at - timedelta(minutes=30),
        )
        available = published + timedelta(minutes=5)
        news.append(
            {
                "event_id": f"evt-{index:03d}",
                "symbol": SYMBOL,
                "published_at": published.isoformat(),
                "available_at": available.isoformat(),
                "headline": headline,
                "summary": summary,
                "source": "synthetic_course_fixture",
            }
        )

with price_path.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(
        [
            "timestamp",
            "open_at",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "available_at",
        ]
    )
    writer.writerows(rows)

with news_path.open("w", encoding="utf-8") as handle:
    for item in news:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")

print(f"prices={price_path} rows={len(rows)}")
print(f"news={news_path} rows={len(news)}")
