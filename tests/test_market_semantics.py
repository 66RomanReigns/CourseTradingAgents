import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from tradinglab_agents.config import load_settings
from tradinglab_agents.data.corporate_actions import LocalCorporateActionProvider
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.engine.decision_graph import planning_input_sha256
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.portfolio_planner import PortfolioPlanner
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.models import Bar, Portfolio
from tradinglab_agents.paper.models import ApprovalPolicy
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.storage.paper_store import PaperTradingStore


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "multi_sample"
SETTINGS = load_settings(ROOT / "config" / "default.yaml")


class ExchangeCalendarTest(unittest.TestCase):
    def test_holiday_and_early_close_are_exchange_defined(self):
        calendar = ExchangeTradingCalendar("XNYS")
        self.assertFalse(calendar.is_session("2025-12-25"))
        self.assertTrue(calendar.is_session("2025-12-26"))
        early = calendar.session("2025-11-28")
        self.assertTrue(early.is_early_close)
        self.assertEqual(early.open_at, datetime(2025, 11, 28, 9, 30))
        self.assertEqual(early.close_at, datetime(2025, 11, 28, 13, 0))
        self.assertEqual(calendar.next_session("2025-12-25").session_date.isoformat(), "2025-12-26")

    def test_holiday_bar_and_wrong_early_close_are_rejected(self):
        calendar = ExchangeTradingCalendar("XNYS")
        holiday = Bar(
            symbol="X",
            timestamp=datetime(2025, 12, 25, 16),
            open_at=datetime(2025, 12, 25, 9, 30),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1000.0,
            available_at=datetime(2025, 12, 25, 16),
        )
        with self.assertRaisesRegex(ValueError, "not a XNYS trading session"):
            calendar.validate_bar(holiday)

        wrong_close = replace(
            holiday,
            timestamp=datetime(2025, 11, 28, 16),
            open_at=datetime(2025, 11, 28, 9, 30),
            available_at=datetime(2025, 11, 28, 16),
        )
        with self.assertRaisesRegex(ValueError, "close does not match"):
            calendar.validate_bar(wrong_close)

    def test_complete_alignment_rejects_silent_symbol_date_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (DATA_DIR / "SPY.csv").open(newline="", encoding="utf-8") as source:
                source_rows = list(csv.DictReader(source))[:3]
            for symbol, count in (("AAA", 3), ("BBB", 2)):
                path = root / f"{symbol}.csv"
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(source_rows[0]))
                    writer.writeheader()
                    writer.writerows(source_rows[:count])
            providers = {
                symbol: LocalCsvProvider(root / f"{symbol}.csv", symbol)
                for symbol in ("AAA", "BBB")
            }
            with self.assertRaisesRegex(ValueError, "unmatched synchronized sessions"):
                AlignedMarketData(
                    providers,
                    calendar=ExchangeTradingCalendar("XNYS"),
                    require_complete_alignment=True,
                )


class CorporateActionHistoryTest(unittest.TestCase):
    def _market(self) -> AlignedMarketData:
        calendar = ExchangeTradingCalendar("XNYS")
        providers = {
            symbol: LocalCsvProvider(DATA_DIR / f"{symbol}.csv", symbol)
            for symbol in ("AAPL", "SPY")
        }
        actions = {
            symbol: LocalCorporateActionProvider(
                DATA_DIR / f"{symbol}_actions.jsonl",
                symbol,
                calendar=calendar,
            )
            for symbol in providers
        }
        return AlignedMarketData(
            providers,
            calendar=calendar,
            strict_session_times=True,
            require_complete_alignment=True,
            corporate_actions=actions,
            adjust_history_for_splits=True,
        )

    def test_split_is_point_in_time_and_pre_split_history_is_adjusted_after_effective_open(self):
        market = self._market()
        split_timestamp = market.timestamp_for_date("2021-08-30")
        index = market.index_of(split_timestamp)
        prior_timestamp = market.timestamps[index - 1]

        prior_visible = market.history("AAPL", prior_timestamp)
        prior_raw = market.raw_history("AAPL", prior_timestamp)
        self.assertEqual(prior_visible[-1].close, prior_raw[-1].close)

        raw = market.raw_history("AAPL", split_timestamp)
        adjusted = market.history("AAPL", split_timestamp)
        raw_gap = abs(raw[-1].open / raw[-2].close - 1.0)
        adjusted_gap = abs(adjusted[-1].open / adjusted[-2].close - 1.0)
        self.assertGreater(raw_gap, 0.60)
        self.assertLess(adjusted_gap, 0.10)
        self.assertAlmostEqual(adjusted[-2].close, raw[-2].close / 4.0)
        self.assertAlmostEqual(adjusted[-2].volume, raw[-2].volume * 4.0)
        self.assertEqual(adjusted[-1].close, raw[-1].close)

    def test_cash_dividend_history_uses_total_return_adjustment(self):
        market = self._market()
        dividend_timestamp = market.timestamp_for_date("2024-03-15")
        raw = market.raw_history("SPY", dividend_timestamp)
        adjusted = market.history("SPY", dividend_timestamp)
        self.assertAlmostEqual(raw[-2].close - adjusted[-2].close, 0.75)
        self.assertEqual(adjusted[-1].close, raw[-1].close)
        raw_gap = abs(raw[-1].open / raw[-2].close - 1.0)
        adjusted_gap = abs(adjusted[-1].open / adjusted[-2].close - 1.0)
        self.assertLess(adjusted_gap, raw_gap)

    def test_action_unavailable_at_effective_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AAPL_actions.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "action_id": "late",
                        "symbol": "AAPL",
                        "action_type": "SPLIT",
                        "effective_date": "2024-06-10",
                        "available_at": "2024-06-10T10:00:00",
                        "split_ratio": 2.0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                LocalCorporateActionProvider(path, "AAPL")

    def test_decision_fingerprint_includes_corporate_action_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = ExchangeTradingCalendar("XNYS")
            providers = {}
            actions = {}
            for symbol in ("AAPL", "SPY"):
                price_path = root / f"{symbol}.csv"
                price_path.write_bytes((DATA_DIR / f"{symbol}.csv").read_bytes())
                action_path = root / f"{symbol}_actions.jsonl"
                action_path.write_bytes((DATA_DIR / f"{symbol}_actions.jsonl").read_bytes())
                providers[symbol] = LocalCsvProvider(price_path, symbol)
                actions[symbol] = LocalCorporateActionProvider(
                    action_path,
                    symbol,
                    calendar=calendar,
                )
            market = AlignedMarketData(
                providers,
                calendar=calendar,
                require_complete_alignment=True,
                corporate_actions=actions,
            )
            timestamp = market.timestamps[max(20, SETTINGS.warmup_bars)]
            context = PortfolioPlanner(SETTINGS).prepare_context(
                market,
                timestamp,
                Portfolio(cash=100_000.0, peak_equity=100_000.0),
            )
            before = planning_input_sha256(context)
            with (root / "AAPL_actions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("\n")
            after = planning_input_sha256(context)
            self.assertNotEqual(before, after)


class PaperCorporateActionTest(unittest.TestCase):
    def _service(self, directory: str) -> PaperTradingService:
        return PaperTradingService(
            PaperTradingStore(Path(directory) / "paper.db"),
            SETTINGS,
        )

    def test_split_is_applied_once_before_open_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            account_id = "split-demo"
            service.initialize_account(
                account_id,
                name="Split Demo",
                symbols=("AAPL", "SPY"),
                initial_cash=1_000.0,
                approval_policy=ApprovalPolicy.ALL,
            )
            market, _ = service.load_market(DATA_DIR, ("AAPL", "SPY"))
            split_timestamp = market.timestamp_for_date("2021-08-30")
            prior = market.timestamps[market.index_of(split_timestamp) - 1]
            peak = 1_000.0 + 3 * market.close_snapshot(prior).price("AAPL")
            service.store.update_account_state(
                account_id,
                cash=1_000.0,
                positions={"AAPL": 3},
                peak_equity=peak,
                risk_state="ACTIVE",
                strategy_state={},
            )

            first = service.run_session(
                account_id,
                data_dir=DATA_DIR,
                session_date="2021-08-30",
            )
            account = service.store.require_account(account_id)
            self.assertEqual(account.positions["AAPL"], 12)
            self.assertEqual(len(first["payload"]["corporate_action_events"]), 1)
            event = first["payload"]["corporate_action_events"][0]["effect"]
            self.assertEqual(event["quantity_before"], 3)
            self.assertEqual(event["quantity_after"], 12)

            repeated = service.run_session(
                account_id,
                data_dir=DATA_DIR,
                session_date="2021-08-30",
            )
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(service.store.require_account(account_id).positions["AAPL"], 12)
            self.assertEqual(
                len(service.store.list_corporate_action_events(account_id)),
                1,
            )

    def test_cash_dividend_is_credited_once(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(directory)
            account_id = "dividend-demo"
            service.initialize_account(
                account_id,
                name="Dividend Demo",
                symbols=("SPY", "QQQ"),
                initial_cash=1_000.0,
                approval_policy=ApprovalPolicy.ALL,
            )
            service.store.update_account_state(
                account_id,
                cash=1_000.0,
                positions={"SPY": 10},
                peak_equity=10_000.0,
                risk_state="ACTIVE",
                strategy_state={},
            )
            result = service.run_session(
                account_id,
                data_dir=DATA_DIR,
                session_date="2024-03-15",
            )
            event = result["payload"]["corporate_action_events"][0]["effect"]
            self.assertAlmostEqual(event["cash_delta"], 7.5)
            self.assertAlmostEqual(service.store.require_account(account_id).cash, 1_007.5)
            service.run_session(
                account_id,
                data_dir=DATA_DIR,
                session_date="2024-03-15",
            )
            self.assertAlmostEqual(service.store.require_account(account_id).cash, 1_007.5)
            self.assertEqual(
                len(service.store.list_corporate_action_events(account_id)),
                1,
            )


if __name__ == "__main__":
    unittest.main()
