import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tradinglab_agents.agents.context import ContextAnalystAgent
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.models import Action, Evidence, EvidencePack


class InvalidReasoner:
    def analyze_news(self, items):
        return {
            "score": 1.0,
            "confidence": 0.9,
            "rationale": "unsupported claim",
            "evidence_ids": ["news.not-present"],
        }


class EvidencePackTest(unittest.TestCase):
    def test_future_evidence_is_rejected(self):
        now = datetime(2025, 1, 1, 16)
        pack = EvidencePack(symbol="X", decision_time=now)
        with self.assertRaises(ValueError):
            pack.add(Evidence("future", "news", now, now + timedelta(days=1), "x", "fixture"))

    def test_duplicate_evidence_is_rejected(self):
        now = datetime(2025, 1, 1, 16)
        pack = EvidencePack(symbol="X", decision_time=now)
        item = Evidence("same", "news", now, now, "x", "fixture")
        pack.add(item)
        with self.assertRaises(ValueError):
            pack.add(item)

    def test_context_agent_vetoes_unknown_evidence(self):
        now = datetime(2025, 1, 1, 16)
        pack = EvidencePack(symbol="X", decision_time=now)
        opinion = ContextAnalystAgent(InvalidReasoner()).analyze(pack)
        self.assertEqual(opinion.action, Action.HOLD)
        self.assertEqual(opinion.confidence, 0.0)


class NewsProviderTest(unittest.TestCase):
    def test_future_news_is_not_visible(self):
        now = datetime(2025, 1, 2, 16)
        rows = [
            {
                "event_id": "visible",
                "symbol": "X",
                "published_at": "2025-01-02T12:00:00",
                "available_at": "2025-01-02T12:01:00",
                "headline": "strong growth",
            },
            {
                "event_id": "future",
                "symbol": "X",
                "published_at": "2025-01-02T12:00:00",
                "available_at": "2025-01-03T09:00:00",
                "headline": "future report",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            provider = LocalNewsProvider(path)
            events = provider.visible_events("X", now)
        self.assertEqual([event.event_id for event in events], ["visible"])


if __name__ == "__main__":
    unittest.main()
