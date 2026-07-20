from __future__ import annotations

import fcntl
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tradinglab_agents.paper.service import PaperTradingService


class SchedulerBusyError(RuntimeError):
    pass


def account_lock_path(lock_path: str | Path, account_id: str) -> Path:
    base = Path(lock_path)
    safe_account = re.sub(r"[^A-Za-z0-9_.-]+", "_", account_id.strip())
    if not safe_account:
        raise ValueError("account_id cannot be empty")
    suffix = base.suffix or ".lock"
    stem = base.stem if base.suffix else base.name
    return base.with_name(f"{stem}.{safe_account}{suffix}")


@contextmanager
def account_process_lock(
    lock_path: str | Path,
    account_id: str,
) -> Iterator[Path]:
    """Acquire one non-blocking cross-process lock for an account mutation."""

    path = account_lock_path(lock_path, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SchedulerBusyError(
                f"paper account is already being processed: {account_id} ({path})"
            ) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"account_id={account_id}\n")
            handle.flush()
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_session_with_lock(
    service: PaperTradingService,
    account_id: str,
    *,
    data_dir: str | Path,
    session_date: str,
    lock_path: str | Path,
    evidence_paths: Sequence[str | Path] = (),
    research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    with account_process_lock(lock_path, account_id):
        return service.run_session(
            account_id,
            data_dir=data_dir,
            session_date=session_date,
            evidence_paths=evidence_paths,
            research_overlays=research_overlays,
        )


def run_next_with_lock(
    service: PaperTradingService,
    account_id: str,
    *,
    data_dir: str | Path,
    lock_path: str | Path,
    evidence_paths: Sequence[str | Path] = (),
    research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Advance exactly one session under the account's shared process lock."""

    with account_process_lock(lock_path, account_id):
        return service.run_next_session(
            account_id,
            data_dir=data_dir,
            evidence_paths=evidence_paths,
            research_overlays=research_overlays,
        )
