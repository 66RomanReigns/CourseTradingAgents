from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(7)
out = Path(__file__).resolve().parents[1] / "data" / "sample" / "demo.csv"
out.parent.mkdir(parents=True, exist_ok=True)
price = 100.0
start = datetime(2025, 1, 2, 16, 0)
rows = []
for i in range(180):
    drift = 0.0008 if i < 70 else (-0.0005 if i < 115 else 0.0012)
    cyc = 0.003 * math.sin(i / 7)
    shock = random.gauss(0, 0.008)
    open_price = price * (1 + random.gauss(0, 0.002))
    close = max(10.0, open_price * (1 + drift + cyc + shock))
    high = max(open_price, close) * (1 + abs(random.gauss(0, 0.004)))
    low = min(open_price, close) * (1 - abs(random.gauss(0, 0.004)))
    volume = 1_000_000 * (1 + abs(shock) * 20 + random.random() * 0.2)
    ts = start + timedelta(days=i)
    rows.append([ts.isoformat(), open_price, high, low, close, volume, ts.isoformat()])
    price = close
with out.open("w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "available_at"])
    writer.writerows(rows)
print(out)
