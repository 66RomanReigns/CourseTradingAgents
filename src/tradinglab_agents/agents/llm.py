from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class StructuredReasoner(Protocol):
    def analyze_news(self, items: list[dict[str, str]]) -> dict:
        """Return score [-1, 1], confidence [0, 1], rationale and evidence_ids."""


@dataclass
class MockLLM:
    """Offline deterministic stand-in for an LLM with structured output.

    It deliberately exposes the same contract as a remote model so experiments
    remain reproducible and the model can later be swapped without changing the
    orchestration code.
    """

    positive_words: tuple[str, ...] = (
        "beat",
        "growth",
        "upgrade",
        "record",
        "profit",
        "launch",
        "approval",
        "partnership",
        "strong",
        "surge",
    )
    negative_words: tuple[str, ...] = (
        "miss",
        "downgrade",
        "loss",
        "fraud",
        "lawsuit",
        "recall",
        "decline",
        "weak",
        "warning",
        "cut",
    )

    def analyze_news(self, items: list[dict[str, str]]) -> dict:
        if not items:
            return {
                "score": 0.0,
                "confidence": 0.20,
                "rationale": "no point-in-time news evidence",
                "evidence_ids": [],
            }
        raw = 0
        cited: list[str] = []
        matched: list[str] = []
        for item in items:
            text = item["text"].lower()
            positive = sum(word in text for word in self.positive_words)
            negative = sum(word in text for word in self.negative_words)
            delta = positive - negative
            if delta:
                raw += delta
                cited.append(item["evidence_id"])
                matched.append(f"{item['evidence_id']}:{delta:+d}")
        score = max(-1.0, min(1.0, raw / max(2.0, len(items))))
        confidence = min(0.85, 0.30 + 0.12 * abs(raw) + 0.03 * len(items))
        return {
            "score": score,
            "confidence": confidence,
            "rationale": "lexical structured reasoning " + (", ".join(matched) or "neutral"),
            "evidence_ids": cited,
        }
