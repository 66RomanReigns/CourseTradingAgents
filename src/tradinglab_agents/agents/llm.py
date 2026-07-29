from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from tradinglab_agents.agents.schemas import (
    FundamentalAnalysis,
    MacroAnalysis,
    NewsAnalysis,
    ResearchDecision,
    RiskReview,
    TradePlan,
)


TModel = TypeVar("TModel", bound=BaseModel)


class StructuredReasoner(Protocol):
    def analyze_news(self, items: list[dict[str, str]]) -> dict[str, Any]: ...

    def analyze_macro(self, items: list[dict[str, str]]) -> dict[str, Any]: ...

    def analyze_fundamentals(
        self, items: list[dict[str, str]]
    ) -> dict[str, Any]: ...


class StructuredLLMClient(Protocol):
    @property
    def identity(self) -> str: ...

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel: ...


@dataclass
class MockLLM:
    """Deterministic offline client implementing the production LLM contract."""

    model: str = "mock-research-v1"
    positive_words: tuple[str, ...] = (
        "beat",
        "growth",
        "upgrade",
        "record",
        "profit",
        "launch",
        "approval",
        "partnership",
        "strong",
        "surge",
        "expansion",
        "improve",
        "cooling inflation",
        "rate cut",
    )
    negative_words: tuple[str, ...] = (
        "miss",
        "downgrade",
        "loss",
        "fraud",
        "lawsuit",
        "recall",
        "decline",
        "weak",
        "warning",
        "cut guidance",
        "recession",
        "inflation shock",
        "default",
    )

    @property
    def identity(self) -> str:
        return f"mock:{self.model}"

    def _lexical_result(
        self,
        items: list[dict[str, str]],
        *,
        analysis_type: str,
    ) -> dict[str, Any]:
        if not items:
            return {
                "analysis_type": analysis_type,
                "score": 0.0,
                "confidence": 0.20,
                "rationale": f"no point-in-time {analysis_type} evidence",
                "evidence_ids": [],
                "catalysts": [],
                "risks": [],
            }
        raw = 0
        cited: list[str] = []
        matched: list[str] = []
        catalysts: list[str] = []
        risks: list[str] = []
        for item in items:
            text = str(item.get("text", "")).lower()
            positive = sum(word in text for word in self.positive_words)
            negative = sum(word in text for word in self.negative_words)
            delta = positive - negative
            if delta:
                evidence_id = str(item["evidence_id"])
                raw += delta
                cited.append(evidence_id)
                matched.append(f"{evidence_id}:{delta:+d}")
                if delta > 0:
                    catalysts.append(evidence_id)
                else:
                    risks.append(evidence_id)
        score = max(-1.0, min(1.0, raw / max(2.0, len(items))))
        confidence = min(0.88, 0.30 + 0.12 * abs(raw) + 0.03 * len(items))
        return {
            "analysis_type": analysis_type,
            "score": score,
            "confidence": confidence,
            "rationale": (
                f"deterministic {analysis_type} reasoning: "
                + (", ".join(matched) or "neutral evidence")
            ),
            "evidence_ids": cited,
            "catalysts": catalysts,
            "risks": risks,
        }

    def analyze_news(self, items: list[dict[str, str]]) -> dict[str, Any]:
        return self._lexical_result(items, analysis_type="news")

    def analyze_macro(self, items: list[dict[str, str]]) -> dict[str, Any]:
        return self._lexical_result(items, analysis_type="macro")

    def analyze_fundamentals(
        self, items: list[dict[str, str]]
    ) -> dict[str, Any]:
        return self._lexical_result(items, analysis_type="fundamental")

    @staticmethod
    def _all_evidence_ids(payload: dict[str, Any]) -> tuple[str, ...]:
        ids: list[str] = []
        for value in payload.values():
            if isinstance(value, dict):
                ids.extend(str(item) for item in value.get("evidence_ids", []))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        ids.extend(
                            str(evidence_id)
                            for evidence_id in item.get("evidence_ids", [])
                        )
        return tuple(dict.fromkeys(ids))

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel:
        del system_prompt
        if task == "news_analysis":
            raw = self.analyze_news(list(payload.get("items", [])))
        elif task == "macro_analysis":
            raw = self.analyze_macro(list(payload.get("items", [])))
        elif task == "fundamental_analysis":
            raw = self.analyze_fundamentals(list(payload.get("items", [])))
        elif task in {"bull_case", "bear_case"}:
            analyses = list(payload.get("analyses", []))
            scores = [float(item.get("score", 0.0)) for item in analyses]
            average = sum(scores) / max(1, len(scores))
            stance = "bull" if task == "bull_case" else "bear"
            directional = max(0.0, average) if stance == "bull" else min(0.0, average)
            if stance == "bull" and directional == 0.0:
                directional = 0.05
            if stance == "bear" and directional == 0.0:
                directional = -0.05
            confidence = min(
                0.90,
                0.40 + sum(float(item.get("confidence", 0.0)) for item in analyses)
                / max(1, len(analyses))
                * 0.45,
            )
            ids = self._all_evidence_ids({"analyses": analyses})
            raw = {
                "stance": stance,
                "score": directional,
                "confidence": confidence,
                "thesis": f"offline {stance} synthesis from {len(analyses)} analyst reports",
                "evidence_ids": ids,
                "supporting_points": tuple(
                    f"{item.get('analysis_type', 'analysis')} score={float(item.get('score', 0.0)):.3f}"
                    for item in analyses
                ),
                "counterpoints": (
                    "mock debate is deterministic and should not be treated as market truth",
                ),
            }
        elif task == "research_manager":
            bull = dict(payload.get("bull", {}))
            bear = dict(payload.get("bear", {}))
            net = (float(bull.get("score", 0.0)) + float(bear.get("score", 0.0))) / 2
            confidence = min(
                0.95,
                (float(bull.get("confidence", 0.0)) + float(bear.get("confidence", 0.0)))
                / 2,
            )
            if net > 0.12 and confidence >= 0.45:
                action = "BUY"
                target_weight = min(0.20, 0.05 + confidence * 0.15)
            elif net < -0.12 and confidence >= 0.45:
                action = "SELL"
                target_weight = 0.0
            else:
                action = "HOLD"
                target_weight = min(0.20, float(payload.get("current_weight", 0.0)))
            raw = {
                "action": action,
                "confidence": confidence,
                "target_weight": target_weight,
                "thesis": f"research manager net debate score={net:.3f}",
                "evidence_ids": tuple(
                    dict.fromkeys(
                        [
                            *[str(item) for item in bull.get("evidence_ids", [])],
                            *[str(item) for item in bear.get("evidence_ids", [])],
                        ]
                    )
                ),
                "dissent": tuple(str(item) for item in bear.get("counterpoints", [])),
            }
        elif task == "trader_plan":
            decision = dict(payload.get("research_decision", {}))
            action = str(decision.get("action", "HOLD"))
            confidence = float(decision.get("confidence", 0.0))
            if confidence < 0.45:
                action = "HOLD"
            target_weight = (
                0.0
                if action == "SELL"
                else float(decision.get("target_weight", 0.0))
            )
            raw = {
                "action": action,
                "confidence": confidence,
                "target_weight": target_weight,
                "order_type": "NO_ORDER" if action == "HOLD" else "MARKET_NEXT_OPEN",
                "rationale": "trader converted validated research into a paper-trading intent",
                "evidence_ids": tuple(
                    str(item) for item in decision.get("evidence_ids", [])
                ),
                "requires_human_approval": True,
            }
        elif task == "risk_review":
            persona = str(payload.get("persona", "balanced"))
            plan = dict(payload.get("preliminary_trade", {}))
            action = str(plan.get("action", "HOLD"))
            confidence = float(plan.get("confidence", 0.0))
            proposed = float(plan.get("target_weight", 0.0))
            if action == "SELL":
                verdict, cap = "APPROVE", 0.0
            elif action == "HOLD":
                verdict, cap = "APPROVE", proposed
            elif persona == "conservative" and confidence < 0.65:
                verdict, cap = "VETO", 0.0
            elif persona == "balanced" and (confidence < 0.55 or proposed > 0.15):
                verdict, cap = "REDUCE", min(proposed, 0.10)
            elif persona == "aggressive" and confidence >= 0.45:
                verdict, cap = "APPROVE", proposed
            else:
                verdict, cap = "REDUCE", min(proposed, 0.15)
            raw = {
                "persona": persona,
                "verdict": verdict,
                "confidence": min(0.95, max(0.35, confidence)),
                "max_target_weight": cap,
                "rationale": f"deterministic {persona} risk review of {action}",
                "evidence_ids": tuple(str(item) for item in plan.get("evidence_ids", [])),
                "conditions": ("paper trading only", "human approval required"),
            }
        elif task == "portfolio_manager":
            plan = dict(payload.get("preliminary_trade", {}))
            reviews = [dict(item) for item in payload.get("risk_reviews", [])]
            action = str(plan.get("action", "HOLD"))
            confidence = float(plan.get("confidence", 0.0))
            target = float(plan.get("target_weight", 0.0))
            caps = [float(item.get("max_target_weight", target)) for item in reviews]
            cap = min([target, *caps]) if caps else target
            veto = any(item.get("verdict") == "VETO" for item in reviews)
            if action == "BUY" and veto:
                action, target = "HOLD", 0.0
            elif action == "BUY":
                target = cap
                if target <= 0.0:
                    action = "HOLD"
            elif action == "SELL":
                target = 0.0
            raw = {
                "action": action,
                "confidence": confidence,
                "target_weight": target,
                "order_type": "NO_ORDER" if action == "HOLD" else "MARKET_NEXT_OPEN",
                "rationale": "portfolio manager reconciled trader intent with risk committee",
                "evidence_ids": tuple(str(item) for item in plan.get("evidence_ids", [])),
                "requires_human_approval": True,
            }
        elif task in {
            "portfolio_correlation_review",
            "portfolio_concentration_review",
        }:
            reviewer = (
                "correlation"
                if task == "portfolio_correlation_review"
                else "concentration"
            )
            plans = [dict(item) for item in payload.get("candidate_plans", [])]
            context = dict(payload.get("market_context", {}))
            policy = dict(payload.get("policy", {}))
            max_gross = float(policy.get("max_gross_target", 0.90))
            cluster_cap = float(policy.get("high_correlation_cluster_cap", 0.25))
            high_symbols = {
                str(symbol).upper()
                for pair in context.get("high_correlation_pairs", [])
                for symbol in dict(pair).get("symbols", [])
            }
            high_symbol_cap = (
                cluster_cap / len(high_symbols) if high_symbols else 0.20
            )
            caps = []
            warnings = []
            for plan in plans:
                symbol = str(plan.get("symbol", "")).upper()
                action = str(plan.get("action", "HOLD")).upper()
                original = max(0.0, min(0.20, float(plan.get("target_weight", 0.0))))
                if action == "SELL":
                    cap = 0.0
                elif reviewer == "correlation" and symbol in high_symbols:
                    cap = min(original, high_symbol_cap)
                    if cap < original:
                        warnings.append(
                            f"{symbol} reduced by deterministic high-correlation review"
                        )
                elif reviewer == "concentration" and original > 0.15:
                    cap = 0.15
                    warnings.append(
                        f"{symbol} reduced by deterministic concentration review"
                    )
                else:
                    cap = original
                caps.append(
                    {
                        "symbol": symbol,
                        "max_target_weight": cap,
                        "rationale": (
                            f"offline {reviewer} cap from original target {original:.4f}"
                        ),
                    }
                )
            raw = {
                "reviewer": reviewer,
                "confidence": 0.75,
                "max_gross_target": min(
                    max_gross,
                    sum(float(item["max_target_weight"]) for item in caps),
                ),
                "symbol_caps": caps,
                "rationale": f"offline deterministic {reviewer} portfolio review",
                "warnings": tuple(warnings),
            }
        elif task == "portfolio_supervisor":
            plans = [dict(item) for item in payload.get("candidate_plans", [])]
            reviews = [dict(item) for item in payload.get("committee_reviews", [])]
            policy = dict(payload.get("policy", {}))
            max_gross = float(policy.get("max_gross_target", 0.90))
            review_caps: dict[str, float] = {}
            review_gross_caps = [max_gross]
            for review in reviews:
                review_gross_caps.append(
                    float(review.get("max_gross_target", max_gross))
                )
                for cap in review.get("symbol_caps", []):
                    item = dict(cap)
                    symbol = str(item.get("symbol", "")).upper()
                    value = float(item.get("max_target_weight", 0.0))
                    review_caps[symbol] = min(
                        review_caps.get(symbol, value),
                        value,
                    )
            allocations = []
            for plan in plans:
                symbol = str(plan.get("symbol", "")).upper()
                original_action = str(plan.get("action", "HOLD")).upper()
                original_target = max(
                    0.0,
                    min(0.20, float(plan.get("target_weight", 0.0))),
                )
                target = min(
                    original_target,
                    review_caps.get(symbol, original_target),
                )
                if original_action == "SELL":
                    action, target = "SELL", 0.0
                elif original_action != "BUY" or target <= 1e-12:
                    action = "HOLD"
                else:
                    action = "BUY"
                allocations.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "target_weight": target,
                        "confidence": float(plan.get("confidence", 0.0)),
                        "rationale": (
                            "offline portfolio supervisor reconciled per-symbol research "
                            "with committee caps"
                        ),
                    }
                )
            gross = sum(float(item["target_weight"]) for item in allocations)
            gross_cap = min(review_gross_caps)
            if gross > gross_cap + 1e-12:
                scale = gross_cap / gross
                for item in allocations:
                    item["target_weight"] = float(item["target_weight"]) * scale
                    if item["action"] == "BUY" and item["target_weight"] <= 1e-12:
                        item["action"] = "HOLD"
                gross = sum(float(item["target_weight"]) for item in allocations)
            raw = {
                "allocations": allocations,
                "gross_target": gross,
                "confidence": (
                    sum(float(item.get("confidence", 0.0)) for item in allocations)
                    / max(1, len(allocations))
                ),
                "rationale": (
                    "offline cross-asset supervisor preserved or reduced every target"
                ),
                "dissent": tuple(
                    warning
                    for review in reviews
                    for warning in review.get("warnings", [])
                ),
            }
        else:
            raise ValueError(f"unsupported mock LLM task: {task}")
        return response_model.model_validate(raw)


@dataclass
class DryRunLLMClient:
    """No-network client that exercises the complete structured LLM workflow.

    It exposes the selected remote provider/model in its identity, but delegates
    output generation to the deterministic MockLLM. This makes deployment,
    caching, schema validation and call budgeting testable before any paid or
    quota-limited request is enabled.
    """

    provider: str
    model: str
    delegate: MockLLM = field(default_factory=MockLLM)

    @property
    def identity(self) -> str:
        return f"dry-run:{self.provider}:{self.model}"

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel:
        return self.delegate.complete(
            task=task,
            system_prompt=system_prompt,
            payload=payload,
            response_model=response_model,
        )


@dataclass
class OpenAICompatibleClient:
    """Structured client for OpenAI-compatible chat-completions endpoints."""

    model: str
    api_key: str
    base_url: str | None = None
    temperature: float = 0.0
    timeout_seconds: float = 60.0
    provider_name: str = "openai-compatible"
    extra_body: dict[str, Any] = field(default_factory=dict)
    _client: Any = field(default=None, init=False, repr=False)
    _last_metadata: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("LLM API key is missing")
        if not self.model:
            raise ValueError("LLM model is missing")

    @property
    def identity(self) -> str:
        endpoint = self.base_url or "openai-default"
        return f"{self.provider_name}:{endpoint}:{self.model}"

    @property
    def last_metadata(self) -> dict[str, Any]:
        return dict(self._last_metadata)

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "openai package is required for openai_compatible provider; "
                    "install the llm extra"
                ) from exc
            kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": self.timeout_seconds,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
        return self._client

    @staticmethod
    def _strip_fence(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            return "\n".join(lines).strip()
        return stripped

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel:
        schema = response_model.model_json_schema()
        user_content = json.dumps(
            {
                "task": task,
                "input": payload,
                "required_json_schema": schema,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        request: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if self.extra_body:
            request["extra_body"] = dict(self.extra_body)
        response = self._get_client().chat.completions.create(**request)
        choice = response.choices[0]
        content = choice.message.content
        usage = getattr(response, "usage", None)
        self._last_metadata = {
            "response_id": getattr(response, "id", None),
            "response_model": getattr(response, "model", None),
            "finish_reason": getattr(choice, "finish_reason", None),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
        if not content:
            raise ValueError("LLM returned empty structured output")
        return response_model.model_validate_json(self._strip_fence(content))


@dataclass
class CachedLLMClient:
    delegate: StructuredLLMClient
    cache_dir: Path
    log_path: Path
    _last_call_metadata: dict[str, Any] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _log_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    @property
    def identity(self) -> str:
        return self.delegate.identity

    @property
    def last_call_metadata(self) -> dict[str, Any]:
        return dict(self._last_call_metadata)

    def _cache_key(
        self,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[BaseModel],
    ) -> str:
        canonical = json.dumps(
            {
                "client": self.identity,
                "task": task,
                "system_prompt": system_prompt,
                "payload": payload,
                "schema": response_model.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _log(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )

    def complete(
        self,
        *,
        task: str,
        system_prompt: str,
        payload: dict[str, Any],
        response_model: type[TModel],
    ) -> TModel:
        cache_key = self._cache_key(task, system_prompt, payload, response_model)
        cache_path = self.cache_dir / f"{cache_key}.json"
        started = time.perf_counter()
        base_record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "task": task,
            "client": self.identity,
            "cache_key": cache_key,
            "response_schema": response_model.__name__,
        }
        if cache_path.is_file():
            result = response_model.model_validate_json(
                cache_path.read_text(encoding="utf-8")
            )
            self._last_call_metadata = {
                "cache_hit": True,
                "runtime_state": "CACHE_HIT",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": {},
            }
            self._log({**base_record, **self._last_call_metadata, "status": "ok"})
            return result
        try:
            result = self.delegate.complete(
                task=task,
                system_prompt=system_prompt,
                payload=payload,
                response_model=response_model,
            )
        except Exception as exc:
            self._last_call_metadata = {
                "cache_hit": False,
                "runtime_state": "FAILED",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "usage": {},
            }
            self._log(
                {
                    **base_record,
                    **self._last_call_metadata,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(cache_path)
        self._last_call_metadata = {
            "cache_hit": False,
            "runtime_state": (
                "DRY_RUN"
                if isinstance(self.delegate, DryRunLLMClient)
                else "LIVE_OK"
            ),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "usage": (
                self.delegate.last_metadata
                if isinstance(self.delegate, OpenAICompatibleClient)
                else {}
            ),
        }
        self._log({**base_record, **self._last_call_metadata, "status": "ok"})
        return result


def build_llm_client(
    settings: Any,
    project_root: str | Path = ".",
    *,
    model: str | None = None,
    cache_namespace: str | None = None,
) -> CachedLLMClient:
    root = Path(project_root)
    mode = settings.llm_execution_mode
    provider = settings.llm_provider
    explicit_model = model is not None
    selected_model = model or settings.llm_model
    if mode == "mock":
        delegate: StructuredLLMClient = MockLLM(model=selected_model or "mock-research-v1")
    elif mode == "dry_run":
        delegate = DryRunLLMClient(provider=provider, model=selected_model)
    elif mode == "live":
        api_key = os.environ.get(settings.llm_api_key_env, "")
        base_url = os.environ.get(settings.llm_base_url_env) or None
        if provider == "zhipu":
            resolved_model = (
                selected_model
                if explicit_model
                else os.environ.get("ZHIPU_MODEL", selected_model)
            )
            delegate = OpenAICompatibleClient(
                model=resolved_model,
                api_key=api_key,
                base_url=base_url or "https://open.bigmodel.cn/api/paas/v4",
                temperature=settings.llm_temperature,
                timeout_seconds=settings.llm_timeout_seconds,
                provider_name="zhipu",
                extra_body={
                    "thinking": {"type": settings.llm_thinking_mode},
                },
            )
        elif provider == "openai_compatible":
            is_deepseek = settings.llm_api_key_env == "DEEPSEEK_API_KEY"
            delegate = OpenAICompatibleClient(
                model=selected_model,
                api_key=api_key,
                base_url=base_url or ("https://api.deepseek.com" if is_deepseek else None),
                temperature=settings.llm_temperature,
                timeout_seconds=settings.llm_timeout_seconds,
                provider_name="deepseek" if is_deepseek else "openai-compatible",
                extra_body=(
                    {"thinking": {"type": settings.llm_thinking_mode}}
                    if is_deepseek
                    else {}
                ),
            )
        elif provider == "mock":
            delegate = MockLLM(model=selected_model)
        else:
            raise ValueError(f"unsupported live LLM provider: {provider}")
    else:
        raise ValueError(f"unsupported LLM execution mode: {mode}")
    cache_dir = Path(settings.llm_cache_dir)
    if cache_namespace:
        cache_dir = cache_dir / cache_namespace
    log_path = Path(settings.llm_log_path)
    if not cache_dir.is_absolute():
        cache_dir = root / cache_dir
    if not log_path.is_absolute():
        log_path = root / log_path
    return CachedLLMClient(delegate=delegate, cache_dir=cache_dir, log_path=log_path)


__all__ = [
    "CachedLLMClient",
    "DryRunLLMClient",
    "FundamentalAnalysis",
    "MacroAnalysis",
    "MockLLM",
    "NewsAnalysis",
    "OpenAICompatibleClient",
    "ResearchDecision",
    "RiskReview",
    "StructuredLLMClient",
    "StructuredReasoner",
    "TradePlan",
    "build_llm_client",
]
