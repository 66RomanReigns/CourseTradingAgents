from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tradinglab_agents.broker.paper import PaperBroker
from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.corporate_actions import LocalCorporateActionProvider
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.decision_graph import (
    LangGraphDeterministicDecisionRuntime,
)
from tradinglab_agents.engine.market import AlignedMarketData
from tradinglab_agents.engine.trading_calendar import ExchangeTradingCalendar
from tradinglab_agents.engine.portfolio_planner import PortfolioPlan, PortfolioPlanner
from tradinglab_agents.models import Fill, Portfolio
from tradinglab_agents.paper.models import (
    ApprovalPolicy,
    DailyRun,
    DailyRunStatus,
    OrderSide,
    OrderStatus,
    PaperAccount,
    PaperOrder,
)
from tradinglab_agents.storage.paper_store import PaperTradingStore


class PaperTradingService:
    """Restart-safe internal paper-trading daily cycle.

    A session performs three deterministic phases:
    1. execute already approved target-weight orders at the synchronized open;
    2. mark the account at the synchronized close;
    3. create approval-gated orders for the next synchronized open.

    It never calls a real broker and never submits external orders.
    """

    def __init__(
        self,
        store: PaperTradingStore,
        settings: BacktestSettings | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or BacktestSettings()
        self.broker = PaperBroker(
            commission_bps=self.settings.commission_bps,
            slippage_bps=self.settings.slippage_bps,
        )
        self.planner = PortfolioPlanner(self.settings)

    def _runtime_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        parts = path.parts
        if parts and parts[0] == "artifacts":
            return self.store.path.parent.joinpath(*parts[1:])
        return self.store.path.parent / path

    def load_market(
        self,
        data_dir: str | Path,
        symbols: Sequence[str],
    ) -> tuple[
        AlignedMarketData,
        dict[str, LocalNewsProvider],
    ]:
        root = Path(data_dir)
        normalized = tuple(
            dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip())
        )
        if len(normalized) < 2:
            raise ValueError("paper multi-asset account requires at least two symbols")
        providers: dict[str, LocalCsvProvider] = {}
        news: dict[str, LocalNewsProvider] = {}
        calendar = ExchangeTradingCalendar(self.settings.market_calendar_name)
        corporate_actions: dict[str, LocalCorporateActionProvider] = {}
        for symbol in normalized:
            price_path = root / f"{symbol}.csv"
            if not price_path.is_file():
                raise ValueError(f"missing paper market file for {symbol}: {price_path}")
            providers[symbol] = LocalCsvProvider(price_path, symbol)
            news_path = root / f"{symbol}_news.jsonl"
            if news_path.is_file():
                news[symbol] = LocalNewsProvider(news_path)
            action_path = root / f"{symbol}{self.settings.market_corporate_action_suffix}"
            if self.settings.market_corporate_actions_enabled and action_path.is_file():
                corporate_actions[symbol] = LocalCorporateActionProvider(
                    action_path,
                    symbol,
                    calendar=calendar,
                )
        return (
            AlignedMarketData(
                providers,
                calendar=(calendar if self.settings.market_strict_sessions else None),
                strict_session_times=self.settings.market_strict_session_times,
                require_complete_alignment=(
                    self.settings.market_require_complete_alignment
                ),
                corporate_actions=corporate_actions,
                adjust_history_for_splits=(
                    self.settings.market_adjust_history_for_splits
                ),
                adjust_history_for_dividends=(
                    self.settings.market_adjust_history_for_dividends
                ),
            ),
            news,
        )

    def initialize_account(
        self,
        account_id: str,
        *,
        name: str,
        symbols: Sequence[str],
        initial_cash: float | None = None,
        approval_policy: ApprovalPolicy = ApprovalPolicy.ALL,
    ) -> PaperAccount:
        return self.store.create_account(
            account_id,
            name=name,
            initial_cash=(
                self.settings.initial_cash if initial_cash is None else initial_cash
            ),
            symbols=symbols,
            approval_policy=approval_policy,
        )

    @staticmethod
    def _portfolio(account: PaperAccount) -> Portfolio:
        return Portfolio(
            cash=account.cash,
            positions=dict(account.positions),
            peak_equity=account.peak_equity,
        )

    @staticmethod
    def _run_id(account_id: str, session_date: str) -> str:
        digest = hashlib.sha256(f"{account_id}|{session_date}".encode()).hexdigest()[:16]
        return f"paper-{session_date.replace('-', '')}-{digest}"

    @staticmethod
    def _order_id(
        account_id: str,
        run_id: str,
        symbol: str,
        scheduled_for: datetime,
        target_weight: float,
    ) -> str:
        value = (
            f"{account_id}|{run_id}|{symbol}|{scheduled_for.isoformat()}|"
            f"{target_weight:.12f}"
        )
        return "ord-" + hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def serialize_account(account: PaperAccount) -> dict[str, Any]:
        payload = asdict(account)
        payload["status"] = account.status.value
        payload["approval_policy"] = account.approval_policy.value
        payload["created_at"] = account.created_at.isoformat() if account.created_at else None
        payload["updated_at"] = account.updated_at.isoformat() if account.updated_at else None
        return payload

    @staticmethod
    def serialize_order(order: PaperOrder) -> dict[str, Any]:
        payload = asdict(order)
        payload["side"] = order.side.value
        payload["status"] = order.status.value
        for field in ("decision_time", "scheduled_for", "created_at", "reviewed_at", "executed_at"):
            value = getattr(order, field)
            payload[field] = value.isoformat() if value else None
        return payload

    @staticmethod
    def serialize_run(run: DailyRun) -> dict[str, Any]:
        payload = asdict(run)
        payload["status"] = run.status.value
        for field in ("open_at", "close_at", "created_at", "updated_at"):
            value = getattr(run, field)
            payload[field] = value.isoformat() if value else None
        return payload

    def account_summary(self, account_id: str) -> dict[str, Any]:
        account = self.store.require_account(account_id)
        return {
            "account": self.serialize_account(account),
            "open_orders": [
                self.serialize_order(order)
                for order in self.store.list_orders(
                    account_id,
                    statuses=[OrderStatus.PENDING_APPROVAL, OrderStatus.APPROVED],
                )
            ],
            "recent_fills": [asdict(fill) for fill in self.store.list_fills(account_id, 20)],
            "equity_history": self.store.equity_history(account_id, 30),
            "recent_decision_memories": self.store.list_decision_memories(
                account_id,
                limit=20,
            ),
            "recent_corporate_actions": self.store.list_corporate_action_events(
                account_id,
                limit=20,
            ),
        }

    def approve_order(
        self,
        order_id: str,
        *,
        reviewer: str,
        note: str = "",
    ) -> PaperOrder:
        return self.store.transition_order(
            order_id,
            OrderStatus.APPROVED,
            actor=reviewer,
            note=note or "manually approved",
        )

    def reject_order(
        self,
        order_id: str,
        *,
        reviewer: str,
        note: str = "",
    ) -> PaperOrder:
        return self.store.transition_order(
            order_id,
            OrderStatus.REJECTED,
            actor=reviewer,
            note=note or "manually rejected",
        )

    def cancel_order(
        self,
        order_id: str,
        *,
        actor: str,
        note: str = "",
    ) -> PaperOrder:
        return self.store.transition_order(
            order_id,
            OrderStatus.CANCELLED,
            actor=actor,
            note=note or "cancelled",
        )

    def approve_all(self, account_id: str, *, reviewer: str) -> list[PaperOrder]:
        pending = self.store.list_orders(
            account_id,
            statuses=[OrderStatus.PENDING_APPROVAL],
            limit=5000,
        )
        return [
            self.approve_order(order.order_id, reviewer=reviewer)
            for order in reversed(pending)
        ]

    def _execution_targets(
        self,
        portfolio: Portfolio,
        market,
        orders: Sequence[PaperOrder],
    ) -> dict[str, float]:
        symbols = set(market.symbols)
        current = portfolio.weights(market, symbols)
        targets = dict(current)
        approved_symbols = {order.symbol for order in orders}
        for order in orders:
            targets[order.symbol] = min(
                self.settings.max_position_weight,
                max(0.0, order.target_weight),
            )

        increases = {
            symbol
            for symbol in approved_symbols
            if targets[symbol] > current.get(symbol, 0.0) + 1e-12
        }
        fixed_gross = sum(
            weight for symbol, weight in targets.items() if symbol not in increases
        )
        available = max(0.0, self.settings.max_gross_exposure - fixed_gross)
        requested = sum(targets[symbol] for symbol in increases)
        if requested > available + 1e-12:
            scale = available / requested if requested else 0.0
            for symbol in increases:
                targets[symbol] *= scale
        gross = sum(targets.values())
        if gross > 1.0 + 1e-9:
            raise RuntimeError(f"execution target gross exceeds 100%: {gross:.2%}")
        return targets

    def _execute_open(
        self,
        *,
        account: PaperAccount,
        portfolio: Portfolio,
        run: DailyRun,
        open_snapshot,
    ) -> tuple[list[dict[str, Any]], int]:
        if run.status == DailyRunStatus.OPEN_EXECUTED:
            durable = self.store.fills_for_run(run.run_id)
            return [asdict(fill) for fill in durable], run.orders_executed

        self.store.expire_due_pending_orders(account.account_id, open_snapshot.timestamp)
        due = self.store.due_approved_orders(account.account_id, open_snapshot.timestamp)

        latest_by_symbol: dict[str, PaperOrder] = {}
        for order in due:
            previous = latest_by_symbol.get(order.symbol)
            if previous is None or order.scheduled_for > previous.scheduled_for:
                latest_by_symbol[order.symbol] = order
        selected = list(latest_by_symbol.values())
        selected_ids = {order.order_id for order in selected}
        for order in due:
            if order.order_id not in selected_ids:
                self.store.transition_order(
                    order.order_id,
                    OrderStatus.EXPIRED,
                    actor="system",
                    note="superseded by a newer approved target for the same symbol",
                )

        if selected:
            targets = self._execution_targets(portfolio, open_snapshot, selected)
            fills = self.broker.rebalance_many(portfolio, targets, open_snapshot)
        else:
            fills = []
        fills_by_symbol: dict[str, Fill] = {fill.symbol: fill for fill in fills}
        fills_by_order = {
            order.order_id: fills_by_symbol.get(order.symbol) for order in selected
        }
        durable = self.store.persist_execution(
            account=account,
            portfolio=portfolio,
            snapshot=open_snapshot,
            orders=selected,
            fills_by_order=fills_by_order,
            strategy_state=account.strategy_state,
            risk_state=account.risk_state,
            run_id=run.run_id,
            orders_created=run.orders_created,
        )
        self.store.save_equity_snapshot(account.account_id, portfolio, open_snapshot)
        return [asdict(fill) for fill in durable], len(selected)

    def _cooldown_ok(
        self,
        account_id: str,
        symbol: str,
        market: AlignedMarketData,
        timestamp: datetime,
    ) -> bool:
        latest = self.store.latest_fill_timestamp(account_id, symbol)
        if latest is None:
            return True
        historical_sessions = [
            item for item in market.timestamps if latest.date() <= item.date() <= timestamp.date()
        ]
        bars_since = max(0, len(historical_sessions) - 1)
        return bars_since >= self.settings.cooldown_bars

    def _mature_decision_memories(
        self,
        account_id: str,
        market: AlignedMarketData,
        current_timestamp: datetime,
    ) -> int:
        current_index = market.index_of(current_timestamp)
        pending = self.store.list_decision_memories(
            account_id,
            outcome_status="PENDING",
            limit=5000,
        )
        matured = 0
        for memory in pending:
            symbol = str(memory["symbol"]).upper()
            benchmark = str(memory["benchmark_symbol"]).upper()
            if symbol not in market.symbols or benchmark not in market.symbols:
                continue
            try:
                decision_timestamp = datetime.fromisoformat(
                    str(memory["market_timestamp"])
                )
                decision_index = market.index_of(decision_timestamp)
            except (TypeError, ValueError):
                continue
            outcome_index = decision_index + int(memory["horizon_sessions"])
            if outcome_index > current_index or outcome_index >= len(market.timestamps):
                continue
            outcome_timestamp = market.timestamps[outcome_index]
            start = market.close_snapshot(decision_timestamp)
            outcome = market.close_snapshot(outcome_timestamp)
            raw_return = outcome.price(symbol) / start.price(symbol) - 1.0
            benchmark_return = (
                outcome.price(benchmark) / start.price(benchmark) - 1.0
            )
            alpha_return = raw_return - benchmark_return
            action = str(memory["action"]).upper()
            failure_type: str | None = None
            if action == "BUY" and alpha_return <= 0.0:
                failure_type = "UNDERPERFORMED_BENCHMARK"
            elif action == "SELL" and raw_return > 0.0:
                failure_type = "MISSED_UPSIDE"
            elif action == "HOLD" and abs(alpha_return) >= 0.05:
                failure_type = "MISSED_MATERIAL_MOVE"
            directional_score = (
                alpha_return
                if action == "BUY"
                else (-raw_return if action == "SELL" else -abs(alpha_return))
            )
            rationale = dict(memory.get("rationale") or {})
            trace = dict(rationale.get("research_trace") or {})
            preliminary = dict(trace.get("preliminary_trader") or {})
            portfolio_manager = dict(trace.get("portfolio_manager") or {})
            reviews = [
                dict(item)
                for item in trace.get("risk_reviews", [])
                if isinstance(item, Mapping)
            ]
            preliminary_action = str(preliminary.get("action") or "UNKNOWN")
            portfolio_action = str(portfolio_manager.get("action") or "UNKNOWN")
            verdicts = [str(item.get("verdict") or "UNKNOWN") for item in reviews]
            research_attribution = {
                "present": bool(trace),
                "graph_version": trace.get("graph_version"),
                "debate_rounds": len(trace.get("debate_rounds", [])),
                "preliminary_action": preliminary_action,
                "portfolio_manager_action": portfolio_action,
                "decision_changed_by_committee": (
                    bool(trace)
                    and preliminary_action != "UNKNOWN"
                    and portfolio_action != preliminary_action
                ),
                "risk_verdicts": verdicts,
                "veto_count": verdicts.count("VETO"),
                "reduce_count": verdicts.count("REDUCE"),
                "model_clients": dict(trace.get("clients") or {}),
            }
            reflection = {
                "generator": "deterministic_outcome_attribution_v2",
                "llm_generated": False,
                "action": action,
                "directional_score": directional_score,
                "lesson": (
                    "decision aligned with the observed horizon outcome"
                    if failure_type is None
                    else f"review decision pattern: {failure_type}"
                ),
                "research_attribution": research_attribution,
                "source_memory_immutable": True,
            }
            matured += int(
                self.store.mature_decision_memory(
                    str(memory["memory_id"]),
                    outcome_timestamp=outcome_timestamp.isoformat(),
                    raw_return=raw_return,
                    benchmark_return=benchmark_return,
                    alpha_return=alpha_return,
                    failure_type=failure_type,
                    reflection=reflection,
                )
            )
        return matured

    def _create_next_orders(
        self,
        *,
        account: PaperAccount,
        portfolio: Portfolio,
        market: AlignedMarketData,
        timestamp: datetime,
        run: DailyRun,
        news_providers: Mapping[str, LocalNewsProvider],
        evidence_providers: Sequence[LocalPointInTimeEvidenceProvider],
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[PortfolioPlan | None, list[PaperOrder]]:
        next_timestamp = market.next_timestamp(timestamp)
        if next_timestamp is None:
            return None, []
        if self.settings.workflow_decision_graph_enabled:
            context = self.planner.prepare_context(
                market,
                timestamp,
                portfolio,
                news_providers=news_providers,
                evidence_providers=evidence_providers,
                strategy_state=account.strategy_state,
                research_overlays=research_overlays,
            )
            runtime = LangGraphDeterministicDecisionRuntime(
                self.planner,
                event_path=self._runtime_path(
                    self.settings.workflow_langgraph_event_path
                ),
            )
            execution = runtime.run(
                thread_id=f"{run.run_id}:DECISION",
                context=context,
                audit_runner=lambda _name, function: function(),
                checkpointer_path=self._runtime_path(
                    self.settings.workflow_decision_checkpoint_database
                ),
                resume=True,
            )
            plan = execution.plan
        else:
            plan = self.planner.plan(
                market,
                timestamp,
                portfolio,
                news_providers=news_providers,
                evidence_providers=evidence_providers,
                strategy_state=account.strategy_state,
                research_overlays=research_overlays,
            )
        close_snapshot = market.close_snapshot(timestamp)
        current = portfolio.weights(close_snapshot, set(market.symbols))
        scheduled_for = market.open_snapshot(next_timestamp).timestamp
        self.store.record_decision_memories(
            account_id=account.account_id,
            run_id=run.run_id,
            decision_time=plan.decision_time,
            market_timestamp=plan.market_timestamp,
            symbol_decisions={
                symbol: {
                    **dict(decision),
                    "decision_graph": dict(plan.graph_runtime),
                }
                for symbol, decision in plan.symbol_decisions.items()
            },
            approved_targets=plan.target_weights,
            benchmark_symbol=("SPY" if "SPY" in market.symbols else market.symbols[0]),
        )
        orders: list[PaperOrder] = []

        for symbol in market.symbols:
            target = float(plan.target_weights.get(symbol, 0.0))
            existing = current[symbol]
            change = abs(target - existing)
            protective_reduction = target < existing - 1e-12
            directional = (
                plan.symbol_decisions[symbol]["final_action"] != "HOLD"
            )
            should_order = (
                plan.risk_decision.force_execution
                or protective_reduction
                or (
                    directional
                    and change >= self.settings.rebalance_threshold
                    and self._cooldown_ok(
                        account.account_id,
                        symbol,
                        market,
                        timestamp,
                    )
                )
            )
            if not should_order:
                continue
            approval_required = (
                account.approval_policy == ApprovalPolicy.ALL
                or (
                    account.approval_policy == ApprovalPolicy.RISK_AUTO
                    and not plan.risk_decision.force_execution
                )
            )
            status = (
                OrderStatus.PENDING_APPROVAL
                if approval_required
                else OrderStatus.APPROVED
            )
            side = OrderSide.BUY if target > existing else OrderSide.SELL
            orders.append(
                PaperOrder(
                    order_id=self._order_id(
                        account.account_id,
                        run.run_id,
                        symbol,
                        scheduled_for,
                        target,
                    ),
                    account_id=account.account_id,
                    run_id=run.run_id,
                    symbol=symbol,
                    side=side,
                    target_weight=target,
                    decision_time=datetime.fromisoformat(plan.decision_time),
                    scheduled_for=scheduled_for,
                    status=status,
                    approval_required=approval_required,
                    force_execution=plan.risk_decision.force_execution,
                    reason=(
                        f"{plan.risk_decision.reason}; "
                        f"signal={plan.symbol_decisions[symbol]['final_action']}"
                    ),
                    evidence_ids=tuple(
                        plan.symbol_decisions[symbol]["evidence_ids"]
                    ),
                )
            )
        return plan, self.store.create_orders(orders)

    def run_session(
        self,
        account_id: str,
        *,
        data_dir: str | Path,
        session_date: date | str,
        evidence_paths: Sequence[str | Path] = (),
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        account = self.store.require_account(account_id)
        market, news = self.load_market(data_dir, account.symbols)
        timestamp = market.timestamp_for_date(session_date)
        index = market.index_of(timestamp)
        if index < max(20, self.settings.warmup_bars):
            raise ValueError(
                f"session {timestamp.date()} is before required warmup index "
                f"{max(20, self.settings.warmup_bars)}"
            )
        session_key = timestamp.date().isoformat()
        existing = self.store.get_daily_run(account_id, session_key)
        if existing and existing.status == DailyRunStatus.COMPLETE:
            return {
                "idempotent": True,
                "run": self.serialize_run(existing),
                "account": self.serialize_account(self.store.require_account(account_id)),
            }

        open_snapshot = market.open_snapshot(timestamp)
        close_snapshot = market.close_snapshot(timestamp)
        run = self.store.begin_daily_run(
            run_id=self._run_id(account_id, session_key),
            account_id=account_id,
            session_date=session_key,
            open_at=open_snapshot.timestamp,
            close_at=close_snapshot.timestamp,
        )
        if run.status == DailyRunStatus.FAILED:
            prior = run.payload.get("failed_after_status")
            resume = (
                DailyRunStatus.OPEN_EXECUTED
                if prior == DailyRunStatus.OPEN_EXECUTED.value
                else DailyRunStatus.STARTED
            )
            self.store.update_daily_run(run.run_id, resume, error=None)
            run = self.store.get_daily_run(account_id, session_key) or run

        try:
            session_actions = market.actions_for_session(
                session_key,
                as_of=open_snapshot.timestamp,
            )
            corporate_action_events = self.store.apply_corporate_action_events(
                account_id=account_id,
                run_id=run.run_id,
                actions=session_actions,
                reference_prices=open_snapshot.prices,
            )
            account = self.store.require_account(account_id)
            portfolio = self._portfolio(account)
            open_fills, orders_executed = self._execute_open(
                account=account,
                portfolio=portfolio,
                run=run,
                open_snapshot=open_snapshot,
            )

            account = self.store.require_account(account_id)
            portfolio = self._portfolio(account)
            close_equity = portfolio.equity(close_snapshot)
            portfolio.peak_equity = max(portfolio.peak_equity, close_equity)
            close_mark = self.store.save_equity_snapshot(
                account_id,
                portfolio,
                close_snapshot,
            )
            memories_matured = self._mature_decision_memories(
                account_id,
                market,
                timestamp,
            )

            evidence = [
                LocalPointInTimeEvidenceProvider(path) for path in evidence_paths
            ]
            plan, created_orders = self._create_next_orders(
                account=account,
                portfolio=portfolio,
                market=market,
                timestamp=timestamp,
                run=run,
                news_providers=news,
                evidence_providers=evidence,
                research_overlays=research_overlays,
            )
            strategy_state = (
                plan.strategy_state if plan is not None else account.strategy_state
            )
            risk_state = (
                plan.risk_decision.state if plan is not None else account.risk_state
            )
            payload = {
                "session_date": session_key,
                "open_at": open_snapshot.timestamp.isoformat(),
                "close_at": close_snapshot.timestamp.isoformat(),
                "market_session": {
                    "calendar": self.settings.market_calendar_name,
                    "session_date": session_key,
                    "open_at": open_snapshot.timestamp.isoformat(),
                    "close_at": close_snapshot.timestamp.isoformat(),
                    "is_early_close": (
                        close_snapshot.timestamp.hour < 16
                    ),
                },
                "corporate_action_events": corporate_action_events,
                "open_fills": open_fills,
                "orders_executed": orders_executed,
                "orders_created": len(created_orders),
                "created_orders": [
                    self.serialize_order(order) for order in created_orders
                ],
                "close_mark": close_mark,
                "memories_matured": memories_matured,
                "plan": (
                    {
                        "decision_time": plan.decision_time,
                        "market_timestamp": plan.market_timestamp,
                        "proposed_targets": plan.proposed_targets,
                        "target_weights": plan.target_weights,
                        "risk_reason": plan.risk_decision.reason,
                        "risk_state": plan.risk_decision.state,
                        "force_execution": plan.risk_decision.force_execution,
                        "symbol_decisions": plan.symbol_decisions,
                        "graph_runtime": plan.graph_runtime,
                    }
                    if plan is not None
                    else None
                ),
                "external_execution": False,
            }
            completed = self.store.complete_daily_run(
                run_id=run.run_id,
                account_id=account_id,
                session_date=session_key,
                portfolio=portfolio,
                strategy_state=strategy_state,
                risk_state=risk_state,
                orders_created=len(created_orders),
                orders_executed=orders_executed,
                payload=payload,
            )
            return {
                "idempotent": False,
                "run": self.serialize_run(completed),
                "account": self.serialize_account(self.store.require_account(account_id)),
                "payload": payload,
            }
        except Exception as exc:
            self.store.fail_daily_run(run.run_id, f"{type(exc).__name__}: {exc}")
            raise

    def next_runnable_session(
        self,
        account_id: str,
        *,
        data_dir: str | Path,
    ) -> str | None:
        account = self.store.require_account(account_id)
        market, _ = self.load_market(data_dir, account.symbols)
        warmup = max(20, self.settings.warmup_bars)
        candidates = market.timestamps[warmup:]
        if account.last_session is None:
            return candidates[0].date().isoformat() if candidates else None
        return next(
            (
                timestamp.date().isoformat()
                for timestamp in candidates
                if timestamp.date().isoformat() > account.last_session
            ),
            None,
        )

    def run_next_session(
        self,
        account_id: str,
        *,
        data_dir: str | Path,
        evidence_paths: Sequence[str | Path] = (),
        research_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self.next_runnable_session(account_id, data_dir=data_dir)
        if session is None:
            return {
                "status": "NO_SESSION",
                "account_id": account_id,
                "external_execution": False,
            }
        return self.run_session(
            account_id,
            data_dir=data_dir,
            session_date=session,
            evidence_paths=evidence_paths,
            research_overlays=research_overlays,
        )

    def export_state(self, account_id: str) -> str:
        return json.dumps(
            self.account_summary(account_id),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
