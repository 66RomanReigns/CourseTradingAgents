import unittest
from datetime import datetime, timedelta

from tradinglab_agents.models import Evidence, EvidencePack


class EvidencePackTest(unittest.TestCase):
    def test_future_evidence_is_rejected(self):
        now = datetime(2025, 1, 1)
        pack = EvidencePack(symbol="X", decision_time=now)
        with self.assertRaises(ValueError):
            pack.add(Evidence("future", "news", now, now + timedelta(days=1), "x", "fixture"))


if __name__ == "__main__":
    unittest.main()
