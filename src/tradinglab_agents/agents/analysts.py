from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from tradinglab_agents.agents.llm import StructuredLLMClient
from tradinglab_agents.agents.schemas import (
    EvidenceAnalysis,
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
)
from tradinglab_agents.models import EvidencePack


TAnalysis = TypeVar("TAnalysis", bound=EvidenceAnalysis)


def _evidence_items(pack: EvidencePack, kind: str) -> list[dict[str, str]]:
    return [
        {
            "evidence_id": item.evidence_id,
            "kind": item.kind,
            "timestamp": item.timestamp.isoformat(),
            "available_at": item.available_at.isoformat(),
            "value": str(item.value),
            "source": item.source,
            "detail": item.detail,
            "text": f"source={item.source}; detail={item.detail}; value={item.value}",
        }
        for item in pack.evidence
        if item.kind == kind
    ]


def _validate_references(
    analysis: EvidenceAnalysis,
    pack: EvidencePack,
    allowed_kind: str,
) -> None:
    allowed_ids = {
        item.evidence_id for item in pack.evidence if item.kind == allowed_kind
    }
    unknown = set(analysis.evidence_ids).difference(allowed_ids)
    if unknown:
        raise ValueError(
            f"{allowed_kind} analyst referenced unavailable evidence: {sorted(unknown)}"
        )


@dataclass
class EvidenceAnalystAgent(Generic[TAnalysis]):
    client: StructuredLLMClient
    kind: str
    task: str
    response_model: type[TAnalysis]
    system_prompt: str

    def analyze(self, pack: EvidencePack) -> TAnalysis:
        result = self.client.complete(
            task=self.task,
            system_prompt=self.system_prompt,
            payload={
                "symbol": pack.symbol,
                "decision_time": pack.decision_time.isoformat(),
                "items": _evidence_items(pack, self.kind),
                "instructions": (
                    "Use only supplied point-in-time evidence. Cite evidence_ids for "
                    "every directional claim and return only the requested JSON object."
                ),
            },
            response_model=self.response_model,
        )
        _validate_references(result, pack, self.kind)
        return result


class NewsAnalystAgent(EvidenceAnalystAgent[NewsAnalysis]):
    def __init__(self, client: StructuredLLMClient):
        super().__init__(
            client=client,
            kind="news",
            task="news_analysis",
            response_model=NewsAnalysis,
            system_prompt=(
                "You are a point-in-time market news analyst. Distinguish event impact "
                "from narrative noise, avoid future knowledge, and cite only supplied IDs."
            ),
        )


class MacroAnalystAgent(EvidenceAnalystAgent[MacroAnalysis]):
    def __init__(self, client: StructuredLLMClient):
        super().__init__(
            client=client,
            kind="macro",
            task="macro_analysis",
            response_model=MacroAnalysis,
            system_prompt=(
                "You are a macro analyst. Interpret releases, rates, inflation and growth "
                "for the named asset without treating macro observations as news prose."
            ),
        )


class FundamentalAnalystAgent(EvidenceAnalystAgent[FundamentalAnalysis]):
    def __init__(self, client: StructuredLLMClient):
        super().__init__(
            client=client,
            kind="fundamental",
            task="fundamental_analysis",
            response_model=FundamentalAnalysis,
            system_prompt=(
                "You are a fundamental analyst. Interpret filed company facts and filing "
                "dates, avoid unsupported valuation claims, and cite only supplied IDs."
            ),
        )
