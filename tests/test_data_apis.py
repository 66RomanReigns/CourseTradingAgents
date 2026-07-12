import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.http_client import CachedHttpJsonClient, DataApiError, _redact_url
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.sec_edgar import SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import write_bars_csv, write_evidence_jsonl, write_news_jsonl
from tradinglab_agents.engine.backtest import BacktestEngine


ROOT = Path(__file__).resolve().parents[1]


class RouteHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(
        self,
        url,
        params=None,
        headers=None,
        *,
        cache_ttl_seconds=None,
        force_refresh=False,
    ):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "cache_ttl_seconds": cache_ttl_seconds,
                "force_refresh": force_refresh,
            }
        )
        response = self.routes[url]
        return json.loads(json.dumps(response))


class TwelveDataTest(unittest.TestCase):
    def test_daily_bars_are_exported_in_project_format(self):
        http = RouteHttp(
            {
                TwelveDataClient.BASE_URL: {
                    "meta": {"symbol": "SPY", "interval": "1day"},
                    "values": [
                        {
                            "datetime": "2024-01-02",
                            "open": "470.0",
                            "high": "472.0",
                            "low": "468.0",
                            "close": "471.5",
                            "volume": "123456",
                        },
                        {
                            "datetime": "2024-01-03",
                            "open": "471.0",
                            "high": "473.0",
                            "low": "469.0",
                            "close": "470.5",
                            "volume": "234567",
                        },
                    ],
                    "status": "ok",
                }
            }
        )
        bars = TwelveDataClient(api_key="x", http=http).fetch_daily_bars(
            "SPY", start_date="2024-01-01", end_date="2024-01-04"
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].open_at.hour, 9)
        self.assertEqual(bars[0].timestamp.hour, 16)
        self.assertEqual(bars[0].available_at, bars[0].timestamp)
        self.assertEqual(http.calls[0]["params"]["order"], "ASC")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spy.csv"
            self.assertEqual(write_bars_csv(bars, path), 2)
            provider = LocalCsvProvider(path, "SPY")
            self.assertEqual(provider.bars[-1].close, 470.5)


class AlphaVantageTest(unittest.TestCase):
    def test_news_is_converted_to_point_in_time_jsonl(self):
        http = RouteHttp(
            {
                AlphaVantageNewsClient.BASE_URL: {
                    "items": "1",
                    "feed": [
                        {
                            "title": "Company reports strong growth and profit beat",
                            "url": "https://example.test/a",
                            "time_published": "20240103T143000",
                            "summary": "Quarterly demand remained strong.",
                            "source": "Example Wire",
                            "ticker_sentiment": [
                                {
                                    "ticker": "AAPL",
                                    "relevance_score": "0.95",
                                    "ticker_sentiment_score": "0.42",
                                    "ticker_sentiment_label": "Bullish",
                                }
                            ],
                        }
                    ],
                }
            }
        )
        events = AlphaVantageNewsClient(api_key="x", http=http).fetch_news("AAPL")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].available_at, datetime(2024, 1, 3, 9, 30))
        self.assertIn("ticker sentiment=Bullish", events[0].summary)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "news.jsonl"
            write_news_jsonl(events, path)
            provider = LocalNewsProvider(path)
            self.assertEqual(
                len(provider.visible_events("AAPL", datetime(2024, 1, 3, 9, 29))), 0
            )
            self.assertEqual(
                len(provider.visible_events("AAPL", datetime(2024, 1, 3, 9, 30))), 1
            )


class FredTest(unittest.TestCase):
    def test_initial_release_date_is_used_as_availability(self):
        http = RouteHttp(
            {
                FredClient.BASE_URL: {
                    "observations": [
                        {
                            "realtime_start": "2024-02-13",
                            "realtime_end": "2024-02-13",
                            "date": "2024-01-01",
                            "value": "3.1",
                        },
                        {
                            "realtime_start": "2024-03-01",
                            "realtime_end": "2024-03-01",
                            "date": "2024-01-01",
                            "value": "3.2",
                        },
                    ]
                }
            }
        )
        records = FredClient(api_key="x", http=http).fetch_initial_release_records(
            "CPIAUCSL"
        )
        self.assertEqual(records[0].kind, "macro")
        self.assertEqual(records[0].available_at, datetime(2024, 2, 13, 23, 59, 59))
        self.assertEqual(http.calls[0]["params"]["output_type"], 4)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "macro.jsonl"
            write_evidence_jsonl(records, path)
            provider = LocalPointInTimeEvidenceProvider(path)
            self.assertEqual(
                provider.visible_records("SPY", datetime(2024, 2, 13, 20, 0)), []
            )
            self.assertEqual(
                len(provider.visible_records("SPY", datetime(2024, 2, 14, 9, 0))), 1
            )


class SecEdgarTest(unittest.TestCase):
    def test_company_facts_are_mapped_to_filing_time(self):
        facts_url = SecEdgarClient.COMPANY_FACTS_URL.format(cik="0000320193")
        http = RouteHttp(
            {
                SecEdgarClient.TICKERS_URL: {
                    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
                },
                facts_url: {
                    "facts": {
                        "us-gaap": {
                            "NetIncomeLoss": {
                                "label": "Net Income (Loss)",
                                "units": {
                                    "USD": [
                                        {
                                            "end": "2023-09-30",
                                            "val": 96995000000,
                                            "accn": "0000320193-23-000106",
                                            "fy": 2023,
                                            "fp": "FY",
                                            "form": "10-K",
                                            "filed": "2023-11-03",
                                        }
                                    ]
                                },
                            }
                        }
                    }
                },
            }
        )
        client = SecEdgarClient(user_agent="TradeLab test@example.com", http=http)
        records = client.fetch_fundamental_records("AAPL", concepts=("NetIncomeLoss",))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].kind, "fundamental")
        self.assertEqual(records[0].available_at, datetime(2023, 11, 3, 23, 59, 59))
        self.assertEqual(records[0].value, 96995000000.0)
        self.assertIn("10-K", records[0].detail)
        self.assertEqual(http.calls[1]["headers"]["User-Agent"], "TradeLab test@example.com")

    def test_sec_requires_declared_contact(self):
        with self.assertRaises(ValueError):
            SecEdgarClient(user_agent="anonymous-client", http=RouteHttp({}))


class ExternalEvidenceBacktestTest(unittest.TestCase):
    def test_external_evidence_is_audited_and_never_visible_early(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "macro.jsonl"
            row = {
                "symbol": "MACRO",
                "series_id": "fred.TEST",
                "evidence_id": "fred.TEST.2024-04-01.2024-04-15",
                "kind": "macro",
                "timestamp": "2024-04-01T00:00:00",
                "available_at": "2024-04-15T23:59:59",
                "value": 4.25,
                "source": "fred:TEST",
                "detail": "test macro initial release",
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            prices = LocalCsvProvider(ROOT / "data/sample/demo.csv", "DEMO")
            evidence = LocalPointInTimeEvidenceProvider(path)
            result = BacktestEngine(BacktestSettings()).run(
                prices,
                None,
                evidence_providers=[evidence],
            )
            evidence_id = row["evidence_id"]
            before = [
                item
                for item in result["decisions"]
                if item["available_at"] < row["available_at"]
            ]
            after = [
                item
                for item in result["decisions"]
                if item["available_at"] >= row["available_at"]
            ]
            self.assertTrue(before)
            self.assertTrue(after)
            self.assertTrue(
                all(evidence_id not in item["available_evidence_ids"] for item in before)
            )
            self.assertTrue(
                any(evidence_id in item["available_evidence_ids"] for item in after)
            )


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


class HttpClientSafetyTest(unittest.TestCase):
    def test_sensitive_query_values_are_redacted(self):
        redacted = _redact_url("https://example.test/data?apikey=secret&symbol=SPY")
        self.assertNotIn("secret", redacted)
        self.assertIn("apikey=%2A%2A%2A", redacted)
        self.assertIn("symbol=SPY", redacted)

    def test_response_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            client = CachedHttpJsonClient(
                cache_dir=Path(temp),
                max_retries=0,
                max_response_bytes=8,
            )
            with patch(
                "tradinglab_agents.data.http_client.urlopen",
                return_value=_FakeResponse(b'{"value":123456789}'),
            ):
                with self.assertRaises(DataApiError):
                    client.get_json("https://example.test/data")


if __name__ == "__main__":
    unittest.main()
