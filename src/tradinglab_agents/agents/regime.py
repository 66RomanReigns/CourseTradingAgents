from __future__ import annotations

from dataclasses import dataclass

from tradinglab_agents.models import EvidencePack


@dataclass(frozen=True)
class RegimeAssessment:
    label: str
    exposure_multiplier: float
    rationale: str
    evidence_ids: tuple[str, ...]


class RegimeGuardAgent:
    """Deterministic market-state agent that only constrains exposure.

    The raw state must persist for several consecutive bars before the active
    state changes. This hysteresis prevents unnecessary trading around regime
    thresholds while preserving a fully deterministic decision process.
    """

    MULTIPLIERS = {
        "bull": 1.00,
        "bear": 0.00,
        "sideways": 0.30,
        "volatile": 0.20,
        "transition": 0.60,
    }

    def __init__(self, confirmation_bars: int = 3):
        if confirmation_bars < 1:
            raise ValueError("confirmation_bars must be positive")
        self.confirmation_bars = confirmation_bars
        self._active_label = "transition"
        self._candidate_label: str | None = None
        self._candidate_count = 0

    @staticmethod
    def _classify(pack: EvidencePack) -> tuple[str, float, float, float]:
        momentum = pack.get_float("momentum.20d")
        trend = pack.get_float("trend.sma5_vs_sma20")
        volatility = pack.get_float("volatility.20d_annualized")
        if volatility >= 0.35:
            label = "volatile"
        elif momentum <= -0.04 and trend < 0.0:
            label = "bear"
        elif abs(momentum) <= 0.025 and abs(trend) <= 0.012:
            label = "sideways"
        elif momentum >= 0.04 and trend > 0.0:
            label = "bull"
        else:
            label = "transition"
        return label, momentum, trend, volatility

    def assess(self, pack: EvidencePack) -> RegimeAssessment:
        raw_label, momentum, trend, volatility = self._classify(pack)
        if raw_label == self._active_label:
            self._candidate_label = None
            self._candidate_count = 0
        else:
            if raw_label == self._candidate_label:
                self._candidate_count += 1
            else:
                self._candidate_label = raw_label
                self._candidate_count = 1
            if self._candidate_count >= self.confirmation_bars:
                self._active_label = raw_label
                self._candidate_label = None
                self._candidate_count = 0

        ids = (
            "momentum.20d",
            "trend.sma5_vs_sma20",
            "volatility.20d_annualized",
        )
        return RegimeAssessment(
            label=self._active_label,
            exposure_multiplier=self.MULTIPLIERS[self._active_label],
            rationale=(
                f"active={self._active_label}; raw={raw_label}; "
                f"pending={self._candidate_label}:{self._candidate_count}/{self.confirmation_bars}; "
                f"momentum20={momentum:.4f}; trend={trend:.4f}; volatility={volatility:.3f}"
            ),
            evidence_ids=ids,
        )
