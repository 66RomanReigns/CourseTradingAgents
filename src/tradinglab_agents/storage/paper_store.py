from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradinglab_agents.data.corporate_actions import (
    CorporateAction,
    apply_corporate_actions,
)
from tradinglab_agents.models import Fill, MarketSnapshot, Portfolio
from tradinglab_agents.paper.models import (
    AccountStatus,
    ApprovalPolicy,
    DailyRun,
    DailyRunStatus,
    OrderSide,
    OrderStatus,
    PaperAccount,
    PaperFill,
    PaperOrder,
    validate_order_transition,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


class PaperTradingStore:
    """Transactional SQLite ledger for an internal-only paper account.

    The store owns all durable account, order, fill and daily-run state. Broker
    calculations happen in memory, but account/fill/order updates are committed
    atomically so a process restart cannot create a partial execution ledger.
    """

    CURRENT_SCHEMA_VERSION = 3

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_currency TEXT NOT NULL,
                    initial_cash REAL NOT NULL,
                    cash REAL NOT NULL,
                    peak_equity REAL NOT NULL,
                    status TEXT NOT NULL,
                    risk_state TEXT NOT NULL,
                    approval_policy TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    strategy_state_json TEXT NOT NULL,
                    last_session TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_positions (
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (account_id, symbol),
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_daily_runs (
                    run_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    open_at TEXT NOT NULL,
                    close_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    orders_created INTEGER NOT NULL DEFAULT 0,
                    orders_executed INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    UNIQUE (account_id, session_date),
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    target_weight REAL NOT NULL,
                    decision_time TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_required INTEGER NOT NULL,
                    force_execution INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    reviewed_at_utc TEXT,
                    reviewer TEXT,
                    review_note TEXT,
                    executed_at_utc TEXT,
                    UNIQUE (run_id, symbol),
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (run_id) REFERENCES paper_daily_runs(run_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_order_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES paper_orders(order_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    FOREIGN KEY (order_id) REFERENCES paper_orders(order_id),
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    field TEXT NOT NULL,
                    cash REAL NOT NULL,
                    equity REAL NOT NULL,
                    gross_exposure REAL NOT NULL,
                    prices_json TEXT NOT NULL,
                    positions_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE (account_id, timestamp, field),
                    FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_paper_orders_account_status
                    ON paper_orders(account_id, status, scheduled_for);
                CREATE INDEX IF NOT EXISTS idx_paper_fills_account_timestamp
                    ON paper_fills(account_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_paper_runs_account_session
                    ON paper_daily_runs(account_id, session_date DESC);
                CREATE INDEX IF NOT EXISTS idx_paper_snapshots_account_timestamp
                    ON paper_equity_snapshots(account_id, timestamp DESC);
                """
            )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > self.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"paper database schema {version} is newer than supported "
                    f"{self.CURRENT_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.execute("PRAGMA user_version = 1")
                version = 1
            if version < 2:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS paper_decision_memories (
                        memory_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        decision_time TEXT NOT NULL,
                        market_timestamp TEXT NOT NULL,
                        action TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        current_weight REAL NOT NULL,
                        proposed_target_weight REAL NOT NULL,
                        approved_target_weight REAL NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        rationale_json TEXT NOT NULL,
                        benchmark_symbol TEXT NOT NULL DEFAULT 'SPY',
                        horizon_sessions INTEGER NOT NULL DEFAULT 5,
                        outcome_status TEXT NOT NULL DEFAULT 'PENDING',
                        outcome_timestamp TEXT,
                        raw_return REAL,
                        benchmark_return REAL,
                        alpha_return REAL,
                        failure_type TEXT,
                        reflection_json TEXT NOT NULL DEFAULT '{}',
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL,
                        UNIQUE (account_id, run_id, symbol),
                        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (run_id) REFERENCES paper_daily_runs(run_id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_paper_memory_account_symbol_time
                        ON paper_decision_memories(
                            account_id, symbol, decision_time DESC
                        );
                    CREATE INDEX IF NOT EXISTS idx_paper_memory_outcome_status
                        ON paper_decision_memories(
                            account_id, outcome_status, decision_time
                        );
                    """
                )
                connection.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS paper_corporate_action_events (
                        event_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        action_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        action_type TEXT NOT NULL,
                        effective_at TEXT NOT NULL,
                        available_at TEXT NOT NULL,
                        quantity_before INTEGER NOT NULL,
                        quantity_after INTEGER NOT NULL,
                        cash_delta REAL NOT NULL,
                        fractional_shares REAL NOT NULL,
                        reference_price REAL NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at_utc TEXT NOT NULL,
                        UNIQUE (account_id, action_id),
                        FOREIGN KEY (account_id) REFERENCES paper_accounts(account_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (run_id) REFERENCES paper_daily_runs(run_id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_paper_corporate_actions_account_time
                        ON paper_corporate_action_events(
                            account_id, effective_at DESC
                        );
                    """
                )
                connection.execute("PRAGMA user_version = 3")

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def record_decision_memories(
        self,
        *,
        account_id: str,
        run_id: str,
        decision_time: str,
        market_timestamp: str,
        symbol_decisions: Mapping[str, Mapping[str, Any]],
        approved_targets: Mapping[str, float],
        benchmark_symbol: str = "SPY",
        horizon_sessions: int = 5,
    ) -> int:
        """Persist immutable decision facts for later outcome attribution."""

        if horizon_sessions < 1:
            raise ValueError("memory horizon_sessions must be positive")
        now = _utc_now().isoformat()
        inserted = 0
        with self._connection() as connection:
            for symbol, decision in sorted(symbol_decisions.items()):
                normalized = symbol.upper()
                memory_id = f"memory-{run_id}-{normalized}"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_decision_memories (
                        memory_id, account_id, run_id, symbol, decision_time,
                        market_timestamp, action, confidence, current_weight,
                        proposed_target_weight, approved_target_weight,
                        evidence_ids_json, rationale_json, benchmark_symbol,
                        horizon_sessions, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        account_id,
                        run_id,
                        normalized,
                        decision_time,
                        market_timestamp,
                        str(decision.get("final_action", "HOLD")).upper(),
                        float(decision.get("confidence", 0.0)),
                        float(decision.get("current_weight_at_decision", 0.0)),
                        float(decision.get("proposed_target_weight", 0.0)),
                        float(approved_targets.get(normalized, 0.0)),
                        _dump(tuple(decision.get("evidence_ids", ()))),
                        _dump(
                            {
                                "quant_action": decision.get("quant_action"),
                                "fused_action": decision.get("fused_action"),
                                "critic_rationale": decision.get("critic_rationale"),
                                "regime": decision.get("regime"),
                                "regime_rationale": decision.get("regime_rationale"),
                                "research_overlay_policy": decision.get(
                                    "research_overlay_policy"
                                ),
                                "research_trace": (
                                    dict(decision.get("research_overlay") or {}).get(
                                        "trace"
                                    )
                                ),
                                "decision_graph": dict(
                                    decision.get("decision_graph") or {}
                                ),
                            }
                        ),
                        benchmark_symbol.upper(),
                        int(horizon_sessions),
                        now,
                        now,
                    ),
                )
                inserted += int(
                    connection.execute("SELECT changes() AS n").fetchone()["n"]
                )
        return inserted

    def list_decision_memories(
        self,
        account_id: str,
        *,
        symbol: str | None = None,
        outcome_status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 5000:
            raise ValueError("memory limit must be in [1, 5000]")
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if outcome_status:
            clauses.append("outcome_status = ?")
            params.append(outcome_status.upper())
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM paper_decision_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY decision_time DESC, symbol
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "account_id": row["account_id"],
                "run_id": row["run_id"],
                "symbol": row["symbol"],
                "decision_time": row["decision_time"],
                "market_timestamp": row["market_timestamp"],
                "action": row["action"],
                "confidence": float(row["confidence"]),
                "current_weight": float(row["current_weight"]),
                "proposed_target_weight": float(row["proposed_target_weight"]),
                "approved_target_weight": float(row["approved_target_weight"]),
                "evidence_ids": _load(row["evidence_ids_json"], []),
                "rationale": _load(row["rationale_json"], {}),
                "benchmark_symbol": row["benchmark_symbol"],
                "horizon_sessions": int(row["horizon_sessions"]),
                "outcome_status": row["outcome_status"],
                "outcome_timestamp": row["outcome_timestamp"],
                "raw_return": row["raw_return"],
                "benchmark_return": row["benchmark_return"],
                "alpha_return": row["alpha_return"],
                "failure_type": row["failure_type"],
                "reflection": _load(row["reflection_json"], {}),
            }
            for row in rows
        ]

    def mature_decision_memory(
        self,
        memory_id: str,
        *,
        outcome_timestamp: str,
        raw_return: float,
        benchmark_return: float,
        alpha_return: float,
        failure_type: str | None,
        reflection: Mapping[str, Any],
    ) -> bool:
        now = _utc_now().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT outcome_status FROM paper_decision_memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"decision memory not found: {memory_id}")
            if row["outcome_status"] != "PENDING":
                return False
            connection.execute(
                """
                UPDATE paper_decision_memories
                SET outcome_status = 'MATURED', outcome_timestamp = ?,
                    raw_return = ?, benchmark_return = ?, alpha_return = ?,
                    failure_type = ?, reflection_json = ?, updated_at_utc = ?
                WHERE memory_id = ? AND outcome_status = 'PENDING'
                """,
                (
                    outcome_timestamp,
                    float(raw_return),
                    float(benchmark_return),
                    float(alpha_return),
                    failure_type,
                    _dump(dict(reflection)),
                    now,
                    memory_id,
                ),
            )
            return bool(
                connection.execute("SELECT changes() AS n").fetchone()["n"]
            )

    def create_account(
        self,
        account_id: str,
        *,
        name: str,
        initial_cash: float,
        symbols: Sequence[str],
        approval_policy: ApprovalPolicy = ApprovalPolicy.ALL,
        base_currency: str = "USD",
    ) -> PaperAccount:
        normalized_id = account_id.strip()
        normalized_symbols = tuple(
            dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
        )
        if not normalized_id:
            raise ValueError("account_id cannot be empty")
        if not name.strip():
            raise ValueError("account name cannot be empty")
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not normalized_symbols:
            raise ValueError("paper account requires at least one symbol")
        now = _utc_now().isoformat()
        with self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO paper_accounts (
                        account_id, name, base_currency, initial_cash, cash,
                        peak_equity, status, risk_state, approval_policy,
                        symbols_json, strategy_state_json, last_session,
                        created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        normalized_id,
                        name.strip(),
                        base_currency.strip().upper(),
                        float(initial_cash),
                        float(initial_cash),
                        float(initial_cash),
                        AccountStatus.ACTIVE.value,
                        "ACTIVE",
                        approval_policy.value,
                        _dump(normalized_symbols),
                        _dump({}),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"paper account already exists: {normalized_id}") from exc
        account = self.get_account(normalized_id)
        if account is None:
            raise RuntimeError("account creation was not persisted")
        return account

    @staticmethod
    def _account_from_row(row: sqlite3.Row, positions: Mapping[str, int]) -> PaperAccount:
        return PaperAccount(
            account_id=row["account_id"],
            name=row["name"],
            base_currency=row["base_currency"],
            initial_cash=float(row["initial_cash"]),
            cash=float(row["cash"]),
            peak_equity=float(row["peak_equity"]),
            status=AccountStatus(row["status"]),
            risk_state=row["risk_state"],
            approval_policy=ApprovalPolicy(row["approval_policy"]),
            symbols=tuple(_load(row["symbols_json"], [])),
            positions={symbol: int(quantity) for symbol, quantity in positions.items()},
            strategy_state=dict(_load(row["strategy_state_json"], {})),
            created_at=datetime.fromisoformat(row["created_at_utc"]),
            updated_at=datetime.fromisoformat(row["updated_at_utc"]),
            last_session=row["last_session"],
        )

    def get_account(self, account_id: str) -> PaperAccount | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            position_rows = connection.execute(
                """
                SELECT symbol, quantity FROM paper_positions
                WHERE account_id = ? AND quantity != 0 ORDER BY symbol
                """,
                (account_id,),
            ).fetchall()
        positions = {item["symbol"]: int(item["quantity"]) for item in position_rows}
        return self._account_from_row(row, positions)

    def require_account(self, account_id: str) -> PaperAccount:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"paper account not found: {account_id}")
        return account

    def list_accounts(self) -> list[PaperAccount]:
        with self._connection() as connection:
            ids = [
                row["account_id"]
                for row in connection.execute(
                    "SELECT account_id FROM paper_accounts ORDER BY created_at_utc"
                ).fetchall()
            ]
        return [self.require_account(account_id) for account_id in ids]

    def update_account_state(
        self,
        account_id: str,
        *,
        cash: float,
        positions: Mapping[str, int],
        peak_equity: float,
        risk_state: str,
        strategy_state: Mapping[str, Any],
        status: AccountStatus | None = None,
        last_session: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if cash < -1e-7:
            raise ValueError("paper account cash cannot be negative")
        owns_connection = connection is None
        context = self._connection() if owns_connection else None
        conn = context.__enter__() if context else connection
        assert conn is not None
        try:
            now = _utc_now().isoformat()
            current = conn.execute(
                "SELECT status, last_session FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"paper account not found: {account_id}")
            resolved_status = status.value if status else current["status"]
            resolved_session = last_session if last_session is not None else current["last_session"]
            conn.execute(
                """
                UPDATE paper_accounts
                SET cash = ?, peak_equity = ?, risk_state = ?, status = ?,
                    strategy_state_json = ?, last_session = ?, updated_at_utc = ?
                WHERE account_id = ?
                """,
                (
                    float(cash),
                    float(peak_equity),
                    risk_state,
                    resolved_status,
                    _dump(dict(strategy_state)),
                    resolved_session,
                    now,
                    account_id,
                ),
            )
            conn.execute("DELETE FROM paper_positions WHERE account_id = ?", (account_id,))
            conn.executemany(
                """
                INSERT INTO paper_positions (account_id, symbol, quantity, updated_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (account_id, symbol.upper(), int(quantity), now)
                    for symbol, quantity in sorted(positions.items())
                    if int(quantity) != 0
                ],
            )
        except Exception:
            if context:
                context.__exit__(*__import__("sys").exc_info())
            raise
        else:
            if context:
                context.__exit__(None, None, None)

    def apply_corporate_action_events(
        self,
        *,
        account_id: str,
        run_id: str,
        actions: Sequence[CorporateAction],
        reference_prices: Mapping[str, float],
    ) -> list[dict[str, Any]]:
        """Atomically apply previously unseen actions to one paper account."""

        if not actions:
            return []
        with self._connection() as connection:
            account_row = connection.execute(
                "SELECT * FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if account_row is None:
                raise ValueError(f"paper account not found: {account_id}")
            position_rows = connection.execute(
                """
                SELECT symbol, quantity FROM paper_positions
                WHERE account_id = ? AND quantity != 0 ORDER BY symbol
                """,
                (account_id,),
            ).fetchall()
            positions = {
                item["symbol"]: int(item["quantity"])
                for item in position_rows
            }
            account = self._account_from_row(account_row, positions)
            existing_ids = {
                row["action_id"]
                for row in connection.execute(
                    """
                    SELECT action_id FROM paper_corporate_action_events
                    WHERE account_id = ?
                    """,
                    (account_id,),
                ).fetchall()
            }
            pending = [
                action for action in actions if action.action_id not in existing_ids
            ]
            if not pending:
                return []
            portfolio = Portfolio(
                cash=account.cash,
                positions=dict(account.positions),
                peak_equity=account.peak_equity,
            )
            effects = apply_corporate_actions(
                portfolio,
                pending,
                reference_prices,
            )
            action_lookup = {action.action_id: action for action in pending}
            now = _utc_now().isoformat()
            rows: list[dict[str, Any]] = []
            for effect in effects:
                action = action_lookup[effect.action_id]
                payload = {
                    "action": action.as_dict(),
                    "effect": effect.as_dict(),
                    "paper_only": True,
                    "external_broker": False,
                }
                event_id = "cae-" + hashlib.sha256(
                    f"{account_id}|{action.action_id}".encode()
                ).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT INTO paper_corporate_action_events (
                        event_id, account_id, run_id, action_id, symbol,
                        action_type, effective_at, available_at,
                        quantity_before, quantity_after, cash_delta,
                        fractional_shares, reference_price, payload_json,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        account_id,
                        run_id,
                        action.action_id,
                        action.symbol,
                        action.action_type.value,
                        action.effective_at.isoformat(),
                        action.available_at.isoformat(),
                        effect.quantity_before,
                        effect.quantity_after,
                        effect.cash_delta,
                        effect.fractional_shares,
                        effect.reference_price,
                        _dump(payload),
                        now,
                    ),
                )
                rows.append({"event_id": event_id, **payload})
            self.update_account_state(
                account_id,
                cash=portfolio.cash,
                positions=portfolio.positions,
                peak_equity=portfolio.peak_equity,
                risk_state=account.risk_state,
                strategy_state=account.strategy_state,
                status=account.status,
                last_session=account.last_session,
                connection=connection,
            )
        return rows

    def list_corporate_action_events(
        self,
        account_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 5000:
            raise ValueError("corporate action event limit must be in [1, 5000]")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_corporate_action_events
                WHERE account_id = ?
                ORDER BY effective_at DESC, action_id DESC
                LIMIT ?
                """,
                (account_id, int(limit)),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "account_id": row["account_id"],
                "run_id": row["run_id"],
                "action_id": row["action_id"],
                "symbol": row["symbol"],
                "action_type": row["action_type"],
                "effective_at": row["effective_at"],
                "available_at": row["available_at"],
                "quantity_before": int(row["quantity_before"]),
                "quantity_after": int(row["quantity_after"]),
                "cash_delta": float(row["cash_delta"]),
                "fractional_shares": float(row["fractional_shares"]),
                "reference_price": float(row["reference_price"]),
                "payload": dict(_load(row["payload_json"], {})),
                "created_at_utc": row["created_at_utc"],
            }
            for row in rows
        ]

    def begin_daily_run(
        self,
        *,
        run_id: str,
        account_id: str,
        session_date: str,
        open_at: datetime,
        close_at: datetime,
    ) -> DailyRun:
        now = _utc_now().isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM paper_daily_runs
                WHERE account_id = ? AND session_date = ?
                """,
                (account_id, session_date),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO paper_daily_runs (
                        run_id, account_id, session_date, open_at, close_at,
                        status, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        account_id,
                        session_date,
                        open_at.isoformat(),
                        close_at.isoformat(),
                        DailyRunStatus.STARTED.value,
                        now,
                        now,
                    ),
                )
        result = self.get_daily_run(account_id, session_date)
        if result is None:
            raise RuntimeError("daily run was not persisted")
        return result

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> DailyRun:
        return DailyRun(
            run_id=row["run_id"],
            account_id=row["account_id"],
            session_date=row["session_date"],
            open_at=datetime.fromisoformat(row["open_at"]),
            close_at=datetime.fromisoformat(row["close_at"]),
            status=DailyRunStatus(row["status"]),
            orders_created=int(row["orders_created"]),
            orders_executed=int(row["orders_executed"]),
            payload=dict(_load(row["payload_json"], {})),
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at_utc"]),
            updated_at=datetime.fromisoformat(row["updated_at_utc"]),
        )

    def get_daily_run(self, account_id: str, session_date: str) -> DailyRun | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM paper_daily_runs
                WHERE account_id = ? AND session_date = ?
                """,
                (account_id, session_date),
            ).fetchone()
        return self._run_from_row(row) if row else None

    def update_daily_run(
        self,
        run_id: str,
        status: DailyRunStatus,
        *,
        orders_created: int | None = None,
        orders_executed: int | None = None,
        payload: Mapping[str, Any] | None = None,
        error: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        owns_connection = connection is None
        context = self._connection() if owns_connection else None
        conn = context.__enter__() if context else connection
        assert conn is not None
        try:
            row = conn.execute(
                "SELECT * FROM paper_daily_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"daily run not found: {run_id}")
            conn.execute(
                """
                UPDATE paper_daily_runs
                SET status = ?, orders_created = ?, orders_executed = ?,
                    payload_json = ?, error = ?, updated_at_utc = ?
                WHERE run_id = ?
                """,
                (
                    status.value,
                    int(orders_created if orders_created is not None else row["orders_created"]),
                    int(orders_executed if orders_executed is not None else row["orders_executed"]),
                    _dump(dict(payload)) if payload is not None else row["payload_json"],
                    error,
                    _utc_now().isoformat(),
                    run_id,
                ),
            )
        except Exception:
            if context:
                context.__exit__(*__import__("sys").exc_info())
            raise
        else:
            if context:
                context.__exit__(None, None, None)

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> PaperOrder:
        return PaperOrder(
            order_id=row["order_id"],
            account_id=row["account_id"],
            run_id=row["run_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            target_weight=float(row["target_weight"]),
            decision_time=datetime.fromisoformat(row["decision_time"]),
            scheduled_for=datetime.fromisoformat(row["scheduled_for"]),
            status=OrderStatus(row["status"]),
            approval_required=bool(row["approval_required"]),
            force_execution=bool(row["force_execution"]),
            reason=row["reason"],
            evidence_ids=tuple(_load(row["evidence_ids_json"], [])),
            created_at=datetime.fromisoformat(row["created_at_utc"]),
            reviewed_at=(
                datetime.fromisoformat(row["reviewed_at_utc"])
                if row["reviewed_at_utc"]
                else None
            ),
            reviewer=row["reviewer"],
            review_note=row["review_note"],
            executed_at=(
                datetime.fromisoformat(row["executed_at_utc"])
                if row["executed_at_utc"]
                else None
            ),
        )

    def create_orders(self, orders: Sequence[PaperOrder]) -> list[PaperOrder]:
        if not orders:
            return []
        now = _utc_now().isoformat()
        with self._connection() as connection:
            for order in orders:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_orders (
                        order_id, account_id, run_id, symbol, side,
                        target_weight, decision_time, scheduled_for, status,
                        approval_required, force_execution, reason,
                        evidence_ids_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id,
                        order.account_id,
                        order.run_id,
                        order.symbol.upper(),
                        order.side.value,
                        float(order.target_weight),
                        order.decision_time.isoformat(),
                        order.scheduled_for.isoformat(),
                        order.status.value,
                        int(order.approval_required),
                        int(order.force_execution),
                        order.reason,
                        _dump(order.evidence_ids),
                        now,
                    ),
                )
                inserted = connection.execute("SELECT changes() AS n").fetchone()["n"]
                if inserted:
                    connection.execute(
                        """
                        INSERT INTO paper_order_events (
                            order_id, from_status, to_status, actor, note, created_at_utc
                        ) VALUES (?, NULL, ?, ?, ?, ?)
                        """,
                        (
                            order.order_id,
                            order.status.value,
                            "system",
                            "order created",
                            now,
                        ),
                    )
        return [self.require_order(order.order_id) for order in orders]

    def get_order(self, order_id: str) -> PaperOrder | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM paper_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._order_from_row(row) if row else None

    def require_order(self, order_id: str) -> PaperOrder:
        order = self.get_order(order_id)
        if order is None:
            raise ValueError(f"paper order not found: {order_id}")
        return order

    def list_orders(
        self,
        account_id: str,
        *,
        statuses: Sequence[OrderStatus] | None = None,
        limit: int = 200,
    ) -> list[PaperOrder]:
        if limit <= 0 or limit > 5000:
            raise ValueError("order limit must be in [1, 5000]")
        params: list[Any] = [account_id]
        where = "account_id = ?"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where += f" AND status IN ({placeholders})"
            params.extend(status.value for status in statuses)
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM paper_orders WHERE {where}
                ORDER BY scheduled_for DESC, symbol LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def transition_order(
        self,
        order_id: str,
        target: OrderStatus,
        *,
        actor: str,
        note: str = "",
    ) -> PaperOrder:
        if not actor.strip():
            raise ValueError("order transition actor cannot be empty")
        now = _utc_now().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM paper_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"paper order not found: {order_id}")
            current = OrderStatus(row["status"])
            validate_order_transition(current, target)
            reviewed_at = now if target in {OrderStatus.APPROVED, OrderStatus.REJECTED} else row["reviewed_at_utc"]
            reviewer = actor if target in {OrderStatus.APPROVED, OrderStatus.REJECTED} else row["reviewer"]
            executed_at = now if target == OrderStatus.EXECUTED else row["executed_at_utc"]
            connection.execute(
                """
                UPDATE paper_orders
                SET status = ?, reviewed_at_utc = ?, reviewer = ?,
                    review_note = ?, executed_at_utc = ?
                WHERE order_id = ?
                """,
                (
                    target.value,
                    reviewed_at,
                    reviewer,
                    note or row["review_note"],
                    executed_at,
                    order_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_order_events (
                    order_id, from_status, to_status, actor, note, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (order_id, current.value, target.value, actor, note, now),
            )
        return self.require_order(order_id)

    def expire_due_pending_orders(
        self,
        account_id: str,
        execution_time: datetime,
    ) -> list[PaperOrder]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT order_id FROM paper_orders
                WHERE account_id = ? AND status = ? AND scheduled_for <= ?
                ORDER BY scheduled_for, symbol
                """,
                (
                    account_id,
                    OrderStatus.PENDING_APPROVAL.value,
                    execution_time.isoformat(),
                ),
            ).fetchall()
        return [
            self.transition_order(
                row["order_id"],
                OrderStatus.EXPIRED,
                actor="system",
                note="approval deadline missed before scheduled open",
            )
            for row in rows
        ]

    def due_approved_orders(
        self,
        account_id: str,
        execution_time: datetime,
    ) -> list[PaperOrder]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_orders
                WHERE account_id = ? AND status = ? AND scheduled_for <= ?
                ORDER BY scheduled_for, symbol
                """,
                (
                    account_id,
                    OrderStatus.APPROVED.value,
                    execution_time.isoformat(),
                ),
            ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def persist_execution(
        self,
        *,
        account: PaperAccount,
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
        orders: Sequence[PaperOrder],
        fills_by_order: Mapping[str, Fill | None],
        strategy_state: Mapping[str, Any],
        risk_state: str,
        run_id: str,
        orders_created: int,
    ) -> list[PaperFill]:
        now = _utc_now().isoformat()
        durable_fills: list[PaperFill] = []
        with self._connection() as connection:
            self.update_account_state(
                account.account_id,
                cash=portfolio.cash,
                positions=portfolio.positions,
                peak_equity=portfolio.peak_equity,
                risk_state=risk_state,
                strategy_state=strategy_state,
                status=(
                    AccountStatus.HALTED
                    if risk_state == "HALTED"
                    else AccountStatus.ACTIVE
                ),
                connection=connection,
            )
            for order in orders:
                row = connection.execute(
                    "SELECT status FROM paper_orders WHERE order_id = ?",
                    (order.order_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"paper order not found: {order.order_id}")
                current = OrderStatus(row["status"])
                validate_order_transition(current, OrderStatus.EXECUTED)
                connection.execute(
                    """
                    UPDATE paper_orders
                    SET status = ?, executed_at_utc = ? WHERE order_id = ?
                    """,
                    (OrderStatus.EXECUTED.value, snapshot.timestamp.isoformat(), order.order_id),
                )
                connection.execute(
                    """
                    INSERT INTO paper_order_events (
                        order_id, from_status, to_status, actor, note, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order.order_id,
                        current.value,
                        OrderStatus.EXECUTED.value,
                        "paper-broker",
                        "target-weight order processed at scheduled open",
                        now,
                    ),
                )
                fill = fills_by_order.get(order.order_id)
                if fill is None:
                    continue
                fill_id = f"fill-{order.order_id}"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_fills (
                        fill_id, order_id, account_id, symbol, quantity,
                        price, fee, timestamp, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill_id,
                        order.order_id,
                        account.account_id,
                        fill.symbol,
                        fill.quantity,
                        fill.price,
                        fill.fee,
                        fill.timestamp.isoformat(),
                        now,
                    ),
                )
                durable_fills.append(
                    PaperFill(
                        fill_id=fill_id,
                        order_id=order.order_id,
                        account_id=account.account_id,
                        symbol=fill.symbol,
                        quantity=fill.quantity,
                        price=fill.price,
                        fee=fill.fee,
                        timestamp=fill.timestamp,
                    )
                )
            self.update_daily_run(
                run_id,
                DailyRunStatus.OPEN_EXECUTED,
                orders_created=orders_created,
                orders_executed=len(orders),
                payload={"open_executed": True},
                connection=connection,
            )
        return durable_fills

    def save_equity_snapshot(
        self,
        account_id: str,
        portfolio: Portfolio,
        snapshot: MarketSnapshot,
    ) -> dict[str, Any]:
        equity = portfolio.equity(snapshot)
        gross = portfolio.gross_exposure(snapshot)
        payload = {
            "account_id": account_id,
            "timestamp": snapshot.timestamp.isoformat(),
            "field": snapshot.field,
            "cash": portfolio.cash,
            "equity": equity,
            "gross_exposure": gross,
            "prices": dict(snapshot.prices),
            "positions": dict(sorted(portfolio.positions.items())),
        }
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO paper_equity_snapshots (
                    account_id, timestamp, field, cash, equity, gross_exposure,
                    prices_json, positions_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    snapshot.timestamp.isoformat(),
                    snapshot.field,
                    portfolio.cash,
                    equity,
                    gross,
                    _dump(dict(snapshot.prices)),
                    _dump(dict(sorted(portfolio.positions.items()))),
                    _utc_now().isoformat(),
                ),
            )
        return payload

    def complete_daily_run(
        self,
        *,
        run_id: str,
        account_id: str,
        session_date: str,
        portfolio: Portfolio,
        strategy_state: Mapping[str, Any],
        risk_state: str,
        orders_created: int,
        orders_executed: int,
        payload: Mapping[str, Any],
    ) -> DailyRun:
        with self._connection() as connection:
            self.update_account_state(
                account_id,
                cash=portfolio.cash,
                positions=portfolio.positions,
                peak_equity=portfolio.peak_equity,
                risk_state=risk_state,
                strategy_state=strategy_state,
                status=(
                    AccountStatus.HALTED
                    if risk_state == "HALTED"
                    else AccountStatus.ACTIVE
                ),
                last_session=session_date,
                connection=connection,
            )
            self.update_daily_run(
                run_id,
                DailyRunStatus.COMPLETE,
                orders_created=orders_created,
                orders_executed=orders_executed,
                payload=payload,
                connection=connection,
            )
        result = self.get_daily_run(account_id, session_date)
        if result is None:
            raise RuntimeError("completed daily run not found")
        return result

    def fail_daily_run(self, run_id: str, error: str) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, payload_json FROM paper_daily_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"daily run not found: {run_id}")
            payload = dict(_load(row["payload_json"], {}))
            payload["failed_after_status"] = row["status"]
            self.update_daily_run(
                run_id,
                DailyRunStatus.FAILED,
                payload=payload,
                error=error,
                connection=connection,
            )

    def fills_for_run(self, run_id: str) -> list[PaperFill]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT fills.* FROM paper_fills AS fills
                JOIN paper_orders AS orders ON orders.order_id = fills.order_id
                WHERE orders.run_id = ? ORDER BY fills.timestamp, fills.symbol
                """,
                (run_id,),
            ).fetchall()
        return [
            PaperFill(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                fee=float(row["fee"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
            )
            for row in rows
        ]

    def list_fills(self, account_id: str, limit: int = 200) -> list[PaperFill]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_fills WHERE account_id = ?
                ORDER BY timestamp DESC LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return [
            PaperFill(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                account_id=row["account_id"],
                symbol=row["symbol"],
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                fee=float(row["fee"]),
                timestamp=datetime.fromisoformat(row["timestamp"]),
            )
            for row in rows
        ]

    def latest_fill_timestamp(self, account_id: str, symbol: str) -> datetime | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT timestamp FROM paper_fills
                WHERE account_id = ? AND symbol = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (account_id, symbol.upper()),
            ).fetchone()
        return datetime.fromisoformat(row["timestamp"]) if row else None

    def equity_history(self, account_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, field, cash, equity, gross_exposure,
                       prices_json, positions_json
                FROM paper_equity_snapshots
                WHERE account_id = ? ORDER BY timestamp DESC LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return [
            {
                "timestamp": row["timestamp"],
                "field": row["field"],
                "cash": float(row["cash"]),
                "equity": float(row["equity"]),
                "gross_exposure": float(row["gross_exposure"]),
                "prices": _load(row["prices_json"], {}),
                "positions": _load(row["positions_json"], {}),
            }
            for row in rows
        ]
