import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import (
    LocalPointInTimeEvidenceProvider,
    PointInTimeRecord,
)
from tradinglab_agents.data.news_provider import LocalNewsProvider, NewsEvent
from tradinglab_agents.data.writers import (
    merge_bars_csv,
    merge_evidence_jsonl,
    merge_news_jsonl,
)
from tradinglab_agents.models import Bar


class IncrementalWriterTest(unittest.TestCase):
    def test_market_news_and_evidence_history_is_merged_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = datetime(2025, 1, 2, 16)
            second = datetime(2025, 1, 3, 16)
            bars_path = root / "X.csv"
            bar1 = Bar("X", first, first.replace(hour=9, minute=30), 10, 11, 9, 10.5, 100, first)
            bar2 = Bar("X", second, second.replace(hour=9, minute=30), 10.5, 12, 10, 11.5, 120, second)
            merge_bars_csv([bar1], bars_path)
            merge_bars_csv([bar2], bars_path)
            self.assertEqual(len(LocalCsvProvider(bars_path, "X").bars), 2)

            news_path = root / "X_news.jsonl"
            event1 = NewsEvent("n1", "X", first, first, "one", "summary", "fixture")
            event2 = NewsEvent("n2", "X", second, second, "two", "summary", "fixture")
            merge_news_jsonl([event1], news_path)
            merge_news_jsonl([event2], news_path)
            self.assertEqual(len(LocalNewsProvider(news_path).events), 2)

            evidence_path = root / "macro.jsonl"
            record1 = PointInTimeRecord(
                "MACRO", "fred.X", "e1", "macro", first, first, 1.0, "fixture"
            )
            record2 = PointInTimeRecord(
                "MACRO", "fred.Y", "e2", "macro", second, second, 2.0, "fixture"
            )
            merge_evidence_jsonl([record1], evidence_path)
            merge_evidence_jsonl([record2], evidence_path)
            self.assertEqual(
                len(LocalPointInTimeEvidenceProvider(evidence_path).records),
                2,
            )


if __name__ == "__main__":
    unittest.main()
