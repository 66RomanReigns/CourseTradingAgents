from __future__ import annotations

from typing import Any, Callable

from tradinglab_agents.agents.llm import MockLLM, StructuredReasoner
from tradinglab_agents.agents.schemas import (
    EvidenceAnalysis,
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
)
from tradinglab_agents.models import Action, AgentOpinion, EvidencePack


class ContextAnalystAgent:
    """Compatibility context agent with type-specific reasoning paths.

    The legacy backtest still consumes one AgentOpinion, but news, macro and
    fundamental evidence are no longer flattened into a single news prompt.
    """

    def __init__(self, reasoner: StructuredReasoner | None = None):
        self.reasoner = reasoner or MockLLM()

    @staticmethod
    def _items(pack: EvidencePack, kind: str) -> list[dict[str, str]]:
        return [
            {
                "evidence_id": item.evidence_id,
                "text": (
                    f"kind={item.kind}; source={item.source}; "
                    f"detail={item.detail}; value={item.value}"
                ),
            }
            for item in pack.evidence
            if item.kind == kind
        ]

    @staticmethod
    def _neutral(analysis_type: str, reason: str) -> dict[str, Any]:
        return {
            "analysis_type": analysis_type,
            "score": 0.0,
            "confidence": 0.0,
            "rationale": reason,
            "evidence_ids": [],
            "catalysts": [],
            "risks": [],
        }

    def _call(
        self,
        method_name: str,
        items: list[dict[str, str]],
        response_model: type[EvidenceAnalysis],
    ) -> EvidenceAnalysis:
        method: Callable[[list[dict[str, str]]], dict[str, Any]] | None = getattr(
            self.reasoner,
            method_name,
            None,
        )
        if method is None:
            raw = self._neutral(
                response_model.model_fields["analysis_type"].default,
                f"reasoner does not implement {method_name}",
            )
        else:
            raw = method(items)
        return response_model.model_validate(raw)

    def analyze(self, pack: EvidencePack) -> AgentOpinion:
        news = self._call(
            "analyze_news",
            self._items(pack, "news"),
            NewsAnalysis,
        )
        analyses: list[EvidenceAnalysis] = [news]

        macro_items = self._items(pack, "macro")
        if macro_items:
            analyses.append(
                self._call("analyze_macro", macro_items, MacroAnalysis)
            )
        fundamental_items = self._items(pack, "fundamental")
        if fundamental_items:
            analyses.append(
                self._call(
                    "analyze_fundamentals",
                    fundamental_items,
                    FundamentalAnalysis,
                )
            )

        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for result in analyses
                for evidence_id in result.evidence_ids
            )
        )
        unknown = set(evidence_ids).difference(pack.ids)
        if unknown:
            return AgentOpinion(
                agent="context_analyst_agent",
                action=Action.HOLD,
                confidence=0.0,
                score=0.0,
                rationale=f"invalid evidence references rejected: {sorted(unknown)}",
                evidence_ids=(),
            )

        total_weight = sum(max(0.05, item.confidence) for item in analyses)
        score = sum(
            item.score * max(0.05, item.confidence) for item in analyses
        ) / total_weight
        confidence = sum(item.confidence for item in analyses) / len(analyses)
        if score > 0.15:
            action = Action.BUY
        elif score < -0.15:
            action = Action.SELL
        else:
            action = Action.HOLD
        return AgentOpinion(
            agent="context_analyst_agent",
            action=action,
            confidence=max(0.0, min(1.0, confidence)),
            score=max(-1.0, min(1.0, score)),
            rationale=" | ".join(
                f"{item.analysis_type}: {item.rationale}" for item in analyses
            ),
            evidence_ids=evidence_ids,
        )
