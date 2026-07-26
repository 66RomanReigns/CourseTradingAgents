from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


_LOCKS_GUARD = threading.Lock()
_DATABASE_LOCKS: dict[str, threading.RLock] = {}


def _database_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _DATABASE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DATABASE_LOCKS[key] = lock
        return lock


@contextmanager
def open_sqlite_checkpoint_saver(
    path: str | Path,
    *,
    busy_timeout_ms: int = 30_000,
) -> Iterator[SqliteSaver]:
    """Open a LangGraph SQLite saver safe for concurrent staged subgraphs.

    Multiple symbol subgraphs may begin in the same parent super-step and share
    one checkpoint database. SQLite supports concurrent WAL readers/writers, but
    LangGraph's schema initialization includes a journal-mode change and DDL.
    Serialize only that setup per database path, then allow graph execution to
    proceed concurrently with a bounded busy timeout.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    timeout_seconds = max(1.0, busy_timeout_ms / 1000.0)
    connection = sqlite3.connect(
        str(destination),
        timeout=timeout_seconds,
        check_same_thread=False,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        lock = _database_lock(destination)
        with lock:
            saver = SqliteSaver(connection)
            saver.setup()
            connection.execute("PRAGMA synchronous=NORMAL")
        yield saver
    finally:
        connection.close()


__all__ = ["open_sqlite_checkpoint_saver"]
