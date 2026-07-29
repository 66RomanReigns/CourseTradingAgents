import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradinglab_agents.config import load_settings
from tradinglab_agents.paper.models import (
    ApprovalPolicy,
    OrderStatus,
    validate_order_transition,
)
from tradinglab_agents.paper.scheduler import (
    SchedulerBusyError,
    _lock_file,
    _unlock_file,
    account_lock_path,
    run_next_with_lock,
)
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.reporting.paper_dashboard import render_paper_dashboard
from tradinglab_agents.storage.paper_store import PaperTradingStore


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data/multi_sample"
SETTINGS = load_settings(ROOT / "config/default.yaml")
SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]


class PaperOrderStateMachineTest(unittest.TestCase):
    def test_invalid_terminal_transition_is_rejected(self):
        validate_order_transition(
            OrderStatus.PENDING_APPROVAL,
            OrderStatus.APPROVED,
        )
        with self.assertRaises(ValueError):
            validate_order_transition(OrderStatus.REJECTED, OrderStatus.APPROVED)


class PaperTradingLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "paper.db"
        self.store = PaperTradingStore(self.database)
        self.service = PaperTradingService(self.store, SETTINGS)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, policy: ApprovalPolicy = ApprovalPolicy.ALL):
        return self.service.initialize_account(
            "demo",
            name="Demo Paper",
            symbols=SYMBOLS,
            approval_policy=policy,
        )

    def test_account_order_execution_restart_and_idempotency(self):
        self._create()
        first = self.service.run_next_session("demo", data_dir=DATA_DIR)
        self.assertEqual(first["payload"]["orders_executed"], 0)
        pending = self.store.list_orders(
            "demo",
            statuses=[OrderStatus.PENDING_APPROVAL],
        )
        self.assertGreater(len(pending), 0)

        approved = self.service.approve_all("demo", reviewer="unit-test")
        self.assertEqual(len(approved), len(pending))
        second = self.service.run_next_session("demo", data_dir=DATA_DIR)
        self.assertEqual(second["payload"]["orders_executed"], len(pending))
        self.assertGreater(len(second["payload"]["open_fills"]), 0)

        reloaded_store = PaperTradingStore(self.database)
        reloaded = PaperTradingService(reloaded_store, SETTINGS)
        account = reloaded_store.require_account("demo")
        self.assertTrue(account.positions)
        self.assertGreater(account.cash, 0)
        fill_count = len(reloaded_store.list_fills("demo"))

        repeated = reloaded.run_session(
            "demo",
            data_dir=DATA_DIR,
            session_date=second["run"]["session_date"],
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(len(reloaded_store.list_fills("demo")), fill_count)

    def test_crash_after_open_execution_resumes_without_duplicate_fill(self):
        self._create()
        self.service.run_next_session("demo", data_dir=DATA_DIR)
        self.service.approve_all("demo", reviewer="unit-test")
        second_session = self.service.next_runnable_session(
            "demo",
            data_dir=DATA_DIR,
        )
        self.assertIsNotNone(second_session)
        with patch.object(
            self.service,
            "_create_next_orders",
            side_effect=RuntimeError("simulated post-open crash"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.run_session(
                    "demo",
                    data_dir=DATA_DIR,
                    session_date=second_session,
                )
        fills_after_crash = len(self.store.list_fills("demo"))
        self.assertGreater(fills_after_crash, 0)
        failed = self.store.get_daily_run("demo", second_session)
        self.assertEqual(failed.status.value, "FAILED")
        self.assertEqual(failed.payload["failed_after_status"], "OPEN_EXECUTED")

        resumed = PaperTradingService(PaperTradingStore(self.database), SETTINGS)
        result = resumed.run_session(
            "demo",
            data_dir=DATA_DIR,
            session_date=second_session,
        )
        self.assertFalse(result["idempotent"])
        self.assertEqual(
            len(PaperTradingStore(self.database).list_fills("demo")),
            fills_after_crash,
        )
        self.assertEqual(result["payload"]["orders_executed"], fills_after_crash)

    def test_unapproved_orders_expire_at_scheduled_open(self):
        self._create()
        first = self.service.run_next_session("demo", data_dir=DATA_DIR)
        old_orders = self.store.list_orders(
            "demo",
            statuses=[OrderStatus.PENDING_APPROVAL],
        )
        self.assertTrue(old_orders)

        second = self.service.run_next_session("demo", data_dir=DATA_DIR)
        self.assertEqual(second["payload"]["orders_executed"], 0)
        for order in old_orders:
            self.assertEqual(
                self.store.require_order(order.order_id).status,
                OrderStatus.EXPIRED,
            )
        self.assertEqual(self.store.list_fills("demo"), [])
        self.assertNotEqual(
            first["run"]["session_date"],
            second["run"]["session_date"],
        )

    def test_none_policy_auto_approves_but_never_calls_external_broker(self):
        self._create(ApprovalPolicy.NONE)
        first = self.service.run_next_session("demo", data_dir=DATA_DIR)
        approved = self.store.list_orders(
            "demo",
            statuses=[OrderStatus.APPROVED],
        )
        self.assertGreater(len(approved), 0)
        self.assertFalse(first["payload"]["external_execution"])
        second = self.service.run_next_session("demo", data_dir=DATA_DIR)
        self.assertGreater(second["payload"]["orders_executed"], 0)
        self.assertFalse(second["payload"]["external_execution"])

    def test_dashboard_renders_account_queue_and_safety_notice(self):
        self._create()
        self.service.run_next_session("demo", data_dir=DATA_DIR)
        dashboard = render_paper_dashboard(self.service.account_summary("demo"))
        self.assertIn("Demo Paper", dashboard)
        self.assertIn("Open approval queue", dashboard)
        self.assertIn("No real broker is connected", dashboard)

    def test_reject_and_double_review_are_rejected(self):
        self._create()
        self.service.run_next_session("demo", data_dir=DATA_DIR)
        order = self.store.list_orders(
            "demo",
            statuses=[OrderStatus.PENDING_APPROVAL],
        )[0]
        rejected = self.service.reject_order(
            order.order_id,
            reviewer="reviewer",
            note="not acceptable",
        )
        self.assertEqual(rejected.status, OrderStatus.REJECTED)
        with self.assertRaises(ValueError):
            self.service.approve_order(order.order_id, reviewer="reviewer")

    def test_locked_scheduler_rejects_overlapping_cycle(self):
        self._create()
        lock_path = Path(self.temp.name) / "paper.lock"
        derived_lock = account_lock_path(lock_path, "demo")
        with derived_lock.open("a+", encoding="utf-8") as handle:
            _lock_file(handle)
            with self.assertRaises(SchedulerBusyError):
                run_next_with_lock(
                    self.service,
                    "demo",
                    data_dir=DATA_DIR,
                    lock_path=lock_path,
                )
            _unlock_file(handle)

    def test_one_shot_scheduler_advances_exactly_one_session(self):
        self._create()
        lock_path = Path(self.temp.name) / "paper.lock"
        first = run_next_with_lock(
            self.service,
            "demo",
            data_dir=DATA_DIR,
            lock_path=lock_path,
        )
        account = self.store.require_account("demo")
        self.assertEqual(account.last_session, first["run"]["session_date"])
        second = run_next_with_lock(
            self.service,
            "demo",
            data_dir=DATA_DIR,
            lock_path=lock_path,
        )
        self.assertGreater(second["run"]["session_date"], first["run"]["session_date"])


if __name__ == "__main__":
    unittest.main()
