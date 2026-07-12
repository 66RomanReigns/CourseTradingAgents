from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class RunStore:
    """Small SQLite store for reproducible course experiments."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at_utc TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    git_revision TEXT,
                    git_dirty INTEGER NOT NULL,
                    audit_passed INTEGER NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS variant_metrics (
                    run_id TEXT NOT NULL,
                    variant_name TEXT NOT NULL,
                    total_return REAL NOT NULL,
                    annualized_return REAL NOT NULL,
                    sharpe REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    turnover REAL NOT NULL,
                    trade_count INTEGER NOT NULL,
                    fees REAL NOT NULL,
                    PRIMARY KEY (run_id, variant_name),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at_utc DESC);
                """
            )

    def save_experiment(self, result: dict) -> str:
        manifest = result.get("manifest")
        if not manifest:
            raise ValueError("experiment result must include a manifest")
        run_id = manifest["run_id"]
        git = manifest.get("git", {})
        audit = result.get("audit", {})
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, created_at_utc, symbol, content_hash, git_revision,
                    git_dirty, audit_passed, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    manifest["created_at_utc"],
                    result["symbol"],
                    manifest["content_hash"],
                    git.get("revision"),
                    int(bool(git.get("dirty"))),
                    int(bool(audit.get("passed"))),
                    payload,
                ),
            )
            connection.execute("DELETE FROM variant_metrics WHERE run_id = ?", (run_id,))
            for row in result.get("summary", []):
                connection.execute(
                    """
                    INSERT INTO variant_metrics (
                        run_id, variant_name, total_return, annualized_return, sharpe,
                        max_drawdown, turnover, trade_count, fees
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        row["name"],
                        row["total_return"],
                        row["annualized_return"],
                        row["sharpe"],
                        row["max_drawdown"],
                        row["turnover"],
                        row["trade_count"],
                        row["fees"],
                    ),
                )
        return run_id

    def list_runs(self, limit: int = 20) -> list[dict]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be in [1, 1000]")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, created_at_utc, symbol, content_hash, git_revision,
                       git_dirty, audit_passed
                FROM runs ORDER BY created_at_utc DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["result_json"]) if row else None
