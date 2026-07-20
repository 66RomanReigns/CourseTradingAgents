from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradinglab_agents.agents.llm import build_llm_client
from tradinglab_agents.agents.quant import QuantSignalAgent
from tradinglab_agents.agents.research import MultiAgentResearchPipeline
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.alpha_vantage import AlphaVantageNewsClient
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.fred import FredClient
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.data.sec_edgar import DEFAULT_US_GAAP_CONCEPTS, SecEdgarClient
from tradinglab_agents.data.twelve_data import TwelveDataClient
from tradinglab_agents.data.writers import (
    merge_bars_csv,
    merge_evidence_jsonl,
    merge_news_jsonl,
)
from tradinglab_agents.engine.features import FeatureEngine
from tradinglab_agents.paper.scheduler import run_next_with_lock
from tradinglab_agents.paper.service import PaperTradingService
from tradinglab_agents.storage.paper_store import PaperTradingStore
from tradinglab_agents.workflows.state import WorkflowStateStore


class DailyWorkflow:
    """Data -> research -> internal paper-trading orchestration.

    Modes:
    - dry_run: no external requests and no paper-account mutation;
    - offline: local files, deterministic LLM execution and optional paper run;
    - live: explicit external refresh and live GLM execution, requiring confirmation.

    Every provider refresh and every research role is persisted as an atomic
    node artifact. A failed run can be resumed only when its mode and complete
    settings fingerprint are unchanged.
    """

    ETF_SYMBOLS = frozenset({"SPY", "QQQ"})
    RESEARCH_CALLS_PER_SYMBOL = 7

    def __init__(
        self,
        settings: BacktestSettings,
        project_root: str | Path,
    ) -> None:
        self.settings = settings
        self.root = Path(project_root).resolve()

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _mode(self, mode: str | None) -> str:
        selected = (mode or self.settings.workflow_mode).lower()
        if selected not in {"dry_run", "offline", "live"}:
            raise ValueError("workflow mode must be dry_run, offline or live")
        return selected

    def _settings_hash(self) -> str:
        payload = json.dumps(
            asdict(self.settings),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def plan(
        self,
        *,
        mode: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        selected = self._mode(mode)
        symbols = list(self.settings.workflow_symbols)
        equities = [symbol for symbol in symbols if symbol not in self.ETF_SYMBOLS]
        candidate_count = min(
            self.settings.workflow_llm_candidate_limit,
            len(symbols),
        )
        llm_calls = (
            self.RESEARCH_CALLS_PER_SYMBOL * candidate_count
            if self.settings.workflow_run_research
            else 0
        )
        if llm_calls > self.settings.llm_max_calls_per_run:
            raise ValueError(
                f"planned LLM calls {llm_calls} exceed configured limit "
                f"{self.settings.llm_max_calls_per_run}"
            )
        credential_names = {
            "market": "TWELVE_DATA_API_KEY",
            "news": "ALPHA_VANTAGE_API_KEY",
            "macro": "FRED_API_KEY",
            "fundamentals": "SEC_USER_AGENT",
            "llm": self.settings.llm_api_key_env,
        }
        steps = [
            {
                "name": "preflight",
                "external_calls": 0,
                "enabled": True,
                "description": "validate configuration, budgets, paths and local fixtures",
            },
            {
                "name": "refresh_market",
                "external_calls": len(symbols),
                "enabled": self.settings.workflow_refresh_market,
                "provider": "twelve_data",
            },
            {
                "name": "refresh_news",
                "external_calls": len(symbols),
                "enabled": self.settings.workflow_refresh_news,
                "provider": "alpha_vantage",
            },
            {
                "name": "refresh_macro",
                "external_calls": len(self.settings.workflow_macro_series),
                "enabled": self.settings.workflow_refresh_macro,
                "provider": "fred",
            },
            {
                "name": "refresh_fundamentals",
                "external_calls": len(equities) * 2,
                "enabled": self.settings.workflow_refresh_fundamentals,
                "provider": "sec_edgar",
                "note": "approximate: ticker map plus company facts; ticker map is cacheable",
            },
            {
                "name": "candidate_screen",
                "external_calls": 0,
                "enabled": self.settings.workflow_run_research,
                "candidate_limit": candidate_count,
            },
            {
                "name": "structured_research",
                "external_calls": llm_calls,
                "enabled": self.settings.workflow_run_research,
                "provider": self.settings.llm_provider,
                "model": self.settings.llm_model,
                "checkpoint_granularity": "one node per research role and symbol",
            },
            {
                "name": "paper_session",
                "external_calls": 0,
                "enabled": self.settings.workflow_run_paper,
                "account_id": account_id or self.settings.paper_default_account_id,
                "external_broker": False,
            },
        ]
        planned_external = sum(
            int(step.get("external_calls", 0))
            for step in steps
            if bool(step.get("enabled"))
        )
        return {
            "mode": selected,
            "symbols": symbols,
            "remote_llm": {
                "provider": self.settings.llm_provider,
                "model": self.settings.llm_model,
                "execution_mode": self.settings.llm_execution_mode,
                "planned_calls": llm_calls,
                "max_calls_per_run": self.settings.llm_max_calls_per_run,
            },
            "external_requests_enabled": selected == "live",
            "planned_external_requests": planned_external,
            "credentials": {
                name: {
                    "env": env_name,
                    "present": bool(os.environ.get(env_name)),
                }
                for name, env_name in credential_names.items()
            },
            "steps": steps,
            "safety": {
                "real_broker_connected": False,
                "dry_run_mutates_account": False,
                "live_requires_explicit_confirmation": True,
                "checkpoint_resume_requires_identical_settings": True,
            },
        }

    def _market_paths(self, data_dir: Path) -> dict[str, Path]:
        return {
            symbol: data_dir / f"{symbol}.csv"
            for symbol in self.settings.workflow_symbols
        }

    def _validate_local_data(self, data_dir: Path) -> dict[str, Any]:
        missing = [
            str(path)
            for path in self._market_paths(data_dir).values()
            if not path.is_file()
        ]
        if missing:
            raise ValueError("missing workflow market files: " + ", ".join(missing))
        return {
            "status": "completed",
            "data_dir": str(data_dir),
            "market_files": len(self.settings.workflow_symbols),
        }

    def _refresh_market(self, data_dir: Path) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = TwelveDataClient()
        result: dict[str, Any] = {}
        for symbol in self.settings.workflow_symbols:
            bars = client.fetch_daily_bars(symbol, outputsize=5000)
            path = data_dir / f"{symbol}.csv"
            result[symbol] = {
                "rows": merge_bars_csv(bars, path),
                "path": str(path),
            }
        return result

    def _refresh_news(self, data_dir: Path) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = AlphaVantageNewsClient()
        result: dict[str, Any] = {}
        for index, symbol in enumerate(self.settings.workflow_symbols):
            if index:
                time.sleep(15.0)
            events = client.fetch_news(symbol, limit=200, sort="EARLIEST")
            path = data_dir / f"{symbol}_news.jsonl"
            result[symbol] = {
                "rows": merge_news_jsonl(events, path),
                "path": str(path),
            }
        return result

    def _refresh_macro(self, data_dir: Path) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        records = []
        client = FredClient()
        for index, series_id in enumerate(self.settings.workflow_macro_series):
            if index:
                time.sleep(1.0)
            records.extend(
                client.fetch_initial_release_records(series_id, symbol="MACRO")
            )
        records.sort(key=lambda item: (item.available_at, item.evidence_id))
        path = data_dir / "macro.jsonl"
        return {
            "rows": merge_evidence_jsonl(records, path),
            "path": str(path),
        }

    def _refresh_fundamentals(self, data_dir: Path) -> dict[str, Any]:
        data_dir.mkdir(parents=True, exist_ok=True)
        client = SecEdgarClient()
        result: dict[str, Any] = {}
        for symbol in self.settings.workflow_symbols:
            if symbol in self.ETF_SYMBOLS:
                continue
            records = client.fetch_fundamental_records(
                symbol,
                concepts=DEFAULT_US_GAAP_CONCEPTS,
            )
            path = data_dir / f"{symbol}_fundamentals.jsonl"
            result[symbol] = {
                "rows": merge_evidence_jsonl(records, path),
                "path": str(path),
            }
        return result

    def _refresh_live_data(self, data_dir: Path) -> dict[str, Any]:
        """Compatibility wrapper; production execution checkpoints providers separately."""

        return {
            "market": (
                self._refresh_market(data_dir)
                if self.settings.workflow_refresh_market
                else {}
            ),
            "news": (
                self._refresh_news(data_dir)
                if self.settings.workflow_refresh_news
                else {}
            ),
            "macro": (
                self._refresh_macro(data_dir)
                if self.settings.workflow_refresh_macro
                else None
            ),
            "fundamentals": (
                self._refresh_fundamentals(data_dir)
                if self.settings.workflow_refresh_fundamentals
                else {}
            ),
        }

    def _evidence_paths(self, data_dir: Path) -> list[Path]:
        paths: list[Path] = []
        macro = data_dir / "macro.jsonl"
        if macro.is_file():
            paths.append(macro)
        for symbol in self.settings.workflow_symbols:
            path = data_dir / f"{symbol}_fundamentals.jsonl"
            if path.is_file():
                paths.append(path)
        return paths

    def _select_candidates(self, data_dir: Path) -> list[dict[str, Any]]:
        engine = FeatureEngine()
        quant = QuantSignalAgent()
        scored: list[tuple[str, float]] = []
        for symbol in self.settings.workflow_symbols:
            provider = LocalCsvProvider(data_dir / f"{symbol}.csv", symbol)
            decision_bar = provider.bars[-1]
            pack = engine.build(
                provider.history(decision_bar.available_at),
                decision_bar.available_at,
            )
            opinion = quant.analyze(pack)
            priority = abs(opinion.score) * max(0.1, opinion.confidence)
            scored.append((symbol, priority))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [
            {"symbol": symbol, "priority": priority}
            for symbol, priority in scored[: self.settings.workflow_llm_candidate_limit]
        ]

    def _research_pack(
        self,
        data_dir: Path,
        symbol: str,
        evidence: list[LocalPointInTimeEvidenceProvider],
    ):
        provider = LocalCsvProvider(data_dir / f"{symbol}.csv", symbol)
        decision_bar = provider.bars[-1]
        pack = FeatureEngine().build(
            provider.history(decision_bar.available_at),
            decision_bar.available_at,
        )
        news_path = data_dir / f"{symbol}_news.jsonl"
        if news_path.is_file():
            LocalNewsProvider(news_path).add_to_pack(pack)
        for external in evidence:
            external.add_to_pack(pack)
        return pack

    def _run_research(
        self,
        data_dir: Path,
        *,
        mode: str,
        state: WorkflowStateStore,
    ) -> dict[str, Any]:
        candidates = state.run_json_node(
            "candidate_screen",
            lambda: self._select_candidates(data_dir),
        )
        expected_calls = len(candidates) * self.RESEARCH_CALLS_PER_SYMBOL
        if expected_calls > self.settings.llm_max_calls_per_run:
            raise ValueError("candidate selection exceeds the LLM call budget")
        execution_mode = (
            "live"
            if mode == "live"
            else ("mock" if mode == "offline" else "dry_run")
        )
        client_settings = replace(
            self.settings,
            llm_execution_mode=execution_mode,
        )
        client = build_llm_client(client_settings, self.root)
        evidence = [
            LocalPointInTimeEvidenceProvider(path)
            for path in self._evidence_paths(data_dir)
        ]
        outputs: dict[str, Any] = {}
        for candidate in candidates:
            symbol = str(candidate["symbol"]).upper()
            priority = float(candidate["priority"])
            pack = self._research_pack(data_dir, symbol, evidence)
            pipeline = MultiAgentResearchPipeline(client)
            result = pipeline.run_resumable(
                pack,
                node_runner=lambda node, model, function, symbol=symbol: state.run_model_node(
                    f"research.{symbol}.{node}",
                    model,
                    function,
                ),
            )
            outputs[symbol] = {
                "screen_priority": priority,
                "client": client.identity,
                "result": result.model_dump(mode="json"),
            }
        return state.run_json_node(
            "research_summary",
            lambda: {
                "candidate_count": len(candidates),
                "planned_calls": expected_calls,
                "candidates": outputs,
            },
        )

    @staticmethod
    def _write_result(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)

    def execute(
        self,
        *,
        mode: str | None = None,
        account_id: str | None = None,
        confirm_live: bool = False,
        run_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        selected = self._mode(mode)
        if selected == "live" and not confirm_live:
            raise ValueError("live workflow requires confirm_live=True")
        if selected == "live" and self.settings.llm_provider == "zhipu":
            if not os.environ.get(self.settings.llm_api_key_env):
                raise ValueError(
                    f"live workflow requires {self.settings.llm_api_key_env}"
                )
        if resume and not run_id:
            raise ValueError("workflow resume requires an explicit run_id")

        resolved_run_id = run_id or datetime.now(timezone.utc).strftime(
            "workflow-%Y%m%dT%H%M%S%fZ"
        )
        run_dir = self._resolve(self.settings.workflow_artifact_dir) / resolved_run_id
        state = WorkflowStateStore(
            run_dir,
            run_id=resolved_run_id,
            mode=selected,
            settings_hash=self._settings_hash(),
            resume=resume,
        )
        existing_state = state.state
        if resume and existing_state.get("status") == "COMPLETE":
            result_path = Path(str(existing_state.get("result_path", run_dir / "result.json")))
            if result_path.is_file():
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                payload["artifact"] = str(result_path)
                payload["workflow_state"] = str(state.state_path)
                return payload

        plan = state.run_json_node(
            "preflight",
            lambda: self.plan(mode=selected, account_id=account_id),
        )
        self._write_result(run_dir / "plan.json", plan)

        data_dir = (
            self._resolve(self.settings.workflow_live_data_dir)
            if selected == "live"
            else self._resolve(self.settings.paper_data_dir)
        )
        payload: dict[str, Any] = {
            "run_id": resolved_run_id,
            "mode": selected,
            "resumed": resume,
            "plan": plan,
            "data_refresh": {},
            "research": {"status": "disabled"},
            "paper": {"status": "disabled"},
        }

        if selected == "live":
            if self.settings.workflow_refresh_market:
                payload["data_refresh"]["market"] = {
                    "status": "completed",
                    "result": state.run_json_node(
                        "refresh_market",
                        lambda: self._refresh_market(data_dir),
                    ),
                }
            if self.settings.workflow_refresh_news:
                payload["data_refresh"]["news"] = {
                    "status": "completed",
                    "result": state.run_json_node(
                        "refresh_news",
                        lambda: self._refresh_news(data_dir),
                    ),
                }
            if self.settings.workflow_refresh_macro:
                payload["data_refresh"]["macro"] = {
                    "status": "completed",
                    "result": state.run_json_node(
                        "refresh_macro",
                        lambda: self._refresh_macro(data_dir),
                    ),
                }
            if self.settings.workflow_refresh_fundamentals:
                payload["data_refresh"]["fundamentals"] = {
                    "status": "completed",
                    "result": state.run_json_node(
                        "refresh_fundamentals",
                        lambda: self._refresh_fundamentals(data_dir),
                    ),
                }
        else:
            payload["data_refresh"] = state.run_json_node(
                "validate_local_data",
                lambda: self._validate_local_data(data_dir),
            )

        research_overlays: dict[str, dict[str, Any]] = {}
        if self.settings.workflow_run_research:
            research_result = self._run_research(
                data_dir,
                mode=selected,
                state=state,
            )
            payload["research"] = {
                "status": "completed",
                "result": research_result,
            }
            research_overlays = {
                symbol: dict(row["result"]["trader"])
                for symbol, row in research_result["candidates"].items()
            }

        if self.settings.workflow_run_paper:
            if selected == "dry_run":
                payload["paper"] = state.run_json_node(
                    "paper_session",
                    lambda: {
                        "status": "planned",
                        "mutated": False,
                        "external_broker": False,
                    },
                )
            else:
                account = account_id or self.settings.paper_default_account_id
                store = PaperTradingStore(
                    self._resolve(self.settings.paper_database_path)
                )
                if store.get_account(account) is None:
                    payload["paper"] = state.run_json_node(
                        "paper_session",
                        lambda: {
                            "status": "skipped",
                            "reason": f"paper account does not exist: {account}",
                            "external_broker": False,
                        },
                    )
                else:
                    service = PaperTradingService(store, self.settings)
                    result = state.run_json_node(
                        "paper_session",
                        lambda: run_next_with_lock(
                            service,
                            account,
                            data_dir=data_dir,
                            lock_path=self._resolve(self.settings.paper_lock_path),
                            evidence_paths=self._evidence_paths(data_dir),
                            research_overlays=research_overlays,
                        ),
                    )
                    payload["paper"] = {
                        "status": "completed",
                        "external_broker": False,
                        "result": result,
                    }

        payload["settings"] = asdict(self.settings)
        output = run_dir / "result.json"
        self._write_result(output, payload)
        state.complete(output)
        payload["artifact"] = str(output)
        payload["workflow_state"] = str(state.state_path)
        return payload
