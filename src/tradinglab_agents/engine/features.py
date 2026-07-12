from __future__ import annotations

import math
from statistics import fmean, pstdev

from tradinglab_agents.models import Bar, Evidence, EvidencePack


def _safe_return(new: float, old: float) -> float:
    return new / old - 1.0 if old else 0.0


class FeatureEngine:
    def build(self, bars: list[Bar], decision_time) -> EvidencePack:
        if len(bars) < 21:
            raise ValueError("at least 21 visible bars are required")
        symbol = bars[-1].symbol
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]
        daily_returns = [_safe_return(closes[i], closes[i - 1]) for i in range(1, len(closes))]

        momentum_5 = _safe_return(closes[-1], closes[-6])
        momentum_20 = _safe_return(closes[-1], closes[-21])
        sma_5 = fmean(closes[-5:])
        sma_20 = fmean(closes[-20:])
        trend = sma_5 / sma_20 - 1.0
        volatility_20 = pstdev(daily_returns[-20:]) * math.sqrt(252)
        volume_ratio = volumes[-1] / fmean(volumes[-20:]) if fmean(volumes[-20:]) else 1.0

        pack = EvidencePack(symbol=symbol, decision_time=decision_time)
        source = "local_csv+feature_engine"
        values = {
            "price.close": closes[-1],
            "momentum.5d": momentum_5,
            "momentum.20d": momentum_20,
            "trend.sma5_vs_sma20": trend,
            "volatility.20d_annualized": volatility_20,
            "volume.ratio20": volume_ratio,
        }
        for key, value in values.items():
            pack.add(
                Evidence(
                    evidence_id=key,
                    kind="market_feature",
                    timestamp=bars[-1].timestamp,
                    available_at=bars[-1].available_at,
                    value=value,
                    source=source,
                )
            )
        return pack
