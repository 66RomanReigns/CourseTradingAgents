from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tradinglab_agents.config import BacktestSettings


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _git_output(project_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def build_manifest(
    project_root: str | Path,
    settings: BacktestSettings,
    input_files: Iterable[str | Path],
    run_type: str,
) -> dict:
    root = Path(project_root).resolve()
    settings_dict = asdict(settings)
    inputs = []
    for value in input_files:
        path = Path(value).resolve()
        inputs.append(
            {
                "path": str(path),
                "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else None,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    created_at = datetime.now(timezone.utc).isoformat()
    revision = _git_output(root, "rev-parse", "HEAD")
    dirty = bool(_git_output(root, "status", "--porcelain"))
    identity_payload = {
        "run_type": run_type,
        "settings": settings_dict,
        "inputs": [{"sha256": item["sha256"], "relative_path": item["relative_path"]} for item in inputs],
        "git_revision": revision,
    }
    content_hash = canonical_hash(identity_payload)
    return {
        "schema_version": 1,
        "run_id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{content_hash[:10]}",
        "created_at_utc": created_at,
        "run_type": run_type,
        "content_hash": content_hash,
        "project_root": str(root),
        "git": {
            "revision": revision,
            "branch": _git_output(root, "branch", "--show-current"),
            "dirty": dirty,
        },
        "runtime": {
            "python": sys.version.replace("\n", " "),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "settings": settings_dict,
        "settings_sha256": canonical_hash(settings_dict),
        "inputs": inputs,
    }
