# Changelog

## v0.18.0 — checkpointed Provider Graph, fallback and data-quality gate

- Added a strict Provider Capability Registry for market, news, macro and fundamental sources, including authority, freshness, timeout, retry, quota, pacing, concurrency, point-in-time and cache capabilities.
- Added a persistent SQLite Provider Health state machine with `HEALTHY`, latency/staleness degradation, `RATE_LIMITED`, `AUTH_FAILED`, `SCHEMA_CHANGED`, `DATA_CONFLICT` and `OFFLINE` states, cooldowns and bounded event history.
- Added a deterministic Fallback Router: Twelve Data daily bars can fall back to Alpha Vantage and then local cache; news, FRED macro and SEC fundamentals can fall back to their point-in-time local caches.
- Added Alpha Vantage `TIME_SERIES_DAILY` support with the same XNYS holiday and early-close validation as Twelve Data.
- Added canonical Provider candidates, cross-source market reconciliation and a weighted quality score over freshness, authority, agreement, schema validity, completeness, point-in-time integrity and fallback depth.
- Added a three-state Data Quality Gate. Major conflicts and invalid schema/point-in-time data fail before persistence; degraded or fallback data may support explanation but cannot expand risk.
- Added deterministic Overlay enforcement: under degraded quality, every research `BUY` is converted to approval-gated `HOLD`, while protective `SELL` decisions remain available.
- Added a durable `<run-id>:DATA_PROVIDER` LangGraph with dynamic resource fan-out, SQLite checkpoints, input/policy SHA-256 and pending-write recovery that reruns only failed resources.
- Added per-provider pacing and shared quota groups. Alpha Vantage market and news share one 15-second serialized group; FRED and SEC are paced independently.
- Added proactive application daily-quota checks against the persistent usage ledger. Exhausted quotas skip credentialed fetchers with a zero-unit event and route to cache before any key is sent.
- Added actual-record freshness for local caches so copying or rewriting an old file cannot make stale data appear fresh.
- Integrated the Provider Graph before Workflow Core while preserving the v0.17 provider refresh path behind a conservative no-expansion rollback switch.
- Added bounded CLI/API operations for capabilities, health, events, conflicts, data quality, graph Mermaid and DATA_PROVIDER thread inspection; none of these read operations trigger external requests.
- Extended Doctor with offline fault injection for rate-limit fallback, non-expansion and major-conflict no-persistence behavior.
- Added thirteen Provider Graph, Alpha Vantage fallback, pacing, quota and full-workflow tests; the complete offline suite now contains 135 passing tests.
- Real provider credentials and endpoints remain unverified by v0.18.0 release tests; live calls still require explicit confirmation and credential-free network preflight.

## v0.17.0 — exchange calendar and point-in-time corporate actions

- Added `exchange-calendars` as a required locked dependency and introduced a project-owned `ExchangeTradingCalendar` adapter for XNYS holidays, DST, session opens/closes and early closes.
- Rebuilt all deterministic fixtures from official XNYS sessions: the 2018–2025 multi-asset baseline now contains 2011 true sessions and 18 early closes instead of 2087 weekday-only rows.
- Added strict validation for every source bar and optional fail-closed complete alignment so unmatched symbol dates cannot be silently removed by a common-timestamp intersection.
- Added local point-in-time corporate-action JSONL models for splits and cash dividends, requiring the action to be known no later than its effective exchange open.
- Added raw-price split and ex-dividend discontinuities to deterministic fixtures, plus split- and dividend-adjusted feature history so Quant/Regime signals use total-return semantics while execution always uses raw prices.
- Added pre-open split quantity changes, fractional-share cash-in-lieu and dividend cash credits for single backtests, multi-asset backtests and Paper sessions.
- Upgraded the Paper ledger to schema v3 with an idempotent `paper_corporate_action_events` table and `UNIQUE(account_id, action_id)` protection across crashes and restarts.
- Added corporate-action file hashes and calendar/adjustment modes to Decision Graph input fingerprints so semantic changes invalidate checkpoint resume.
- Upgraded Workflow data validation from file-existence checks to parsed synchronized-market validation with calendar, early-close, alignment and corporate-action summaries.
- Added cached exchange-session lookup, reducing strict Paper lifecycle regression time from roughly 48 seconds to roughly 6 seconds without relaxing validation.
- Added `market-calendar`, `market-validate`, `paper-actions`, `make market-calendar`, `make market-validate`, protected `/market/calendar`, `/market/semantics` and Paper corporate-action endpoints.
- Extended API health, workflow plans, Doctor and the Paper dashboard with market-semantic and corporate-action provenance.
- Added nine dedicated market-semantic tests covering holidays, early closes, complete alignment, point-in-time split/dividend adjustment, fingerprint invalidation and idempotent Paper posting.

## v0.16.0 — deterministic decision graph and checkpointed hard-risk planning

- Refactored `PortfolioPlanner` into pure, reusable stages for Evidence construction, Quant analysis, Context/Fusion/Critic review, Regime assessment, Overlay target policy and portfolio hard-risk finalization while retaining the sequential `plan()` fallback.
- Added a native deterministic LangGraph with dynamic per-symbol subgraphs. Each symbol runs Evidence first, then Quant/Fusion/Critic and Regime branches, followed by target/Overlay fan-in; all symbols fan-in to `PortfolioRiskGovernor` and canonical plan finalization.
- Added durable `<daily-run-id>:DECISION` threads and `artifacts/langgraph/decision_checkpoints.db` with nested subgraph recovery.
- Verified exact field-for-field parity between the decision graph and the sequential planner.
- Added deterministic input SHA-256 over market/news/evidence files, portfolio state, strategy state and research overlays; resume fails closed when any input differs.
- Added canonical plan SHA-256 and persisted DECISION thread, checkpoint and plan-hash provenance into Paper run payloads and immutable decision memories.
- Changed hard-risk planning to use a Portfolio snapshot copy, guaranteeing that the graph does not mutate the caller's Portfolio object, paper account, order ledger or external systems.
- Integrated the graph immediately before decision-memory and order persistence; Paper approval, order creation, account locks and next-open execution remain outside the graph.
- Added relative runtime path isolation so temporary paper databases and production ledgers do not share DECISION threads accidentally.
- Added `decision-graph`, `make decision-graph`, protected `/workflow/decision-graph`, DECISION-aware thread inspection and workflow/API health metadata.
- Extended Doctor with a real no-network deterministic decision probe and added five dedicated tests for parity, purity, nested recovery, input-hash mismatch rejection and Paper provenance.

## v0.15.0 — persistent workflow core graph and paper-input contract

- Added a new top-level `WORKFLOW_CORE` LangGraph above the research-parent graph with four recoverable stages: local data validation, research-parent execution, overlay assembly and deterministic decision preparation.
- Kept external provider refresh and paper-account execution outside the core graph as explicit staged-migration safety boundaries.
- Added strict overlay assembly checks so the candidate symbol set must exactly match the final research overlay set.
- Added deterministic paper-input validation covering action/order compatibility, protective SELL targets, maximum position limits, human-approval requirements and non-expansion traces.
- Added a canonical SHA-256 hash of the complete overlay payload for reproducible handoff into the paper service.
- Added an isolated `<workflow-run-id>:WORKFLOW_CORE` SQLite thread and `artifacts/langgraph/workflow_core_checkpoints.db` database.
- Added native checkpoint recovery so an overlay-stage failure does not rerun completed data-validation or research-parent stages.
- Added a shared concurrency-safe LangGraph SQLite saver that serializes schema/WAL initialization per database path, applies a 30-second busy timeout and preserves parallel symbol execution.
- Added a research-disabled branch that still validates data and emits an explicit safe empty paper input; network-only workflows may mark validation as `not_required` when no stage consumes market data.
- Added `workflow-core-graph`, `make workflow-core-graph`, protected `/workflow/core-graph`, core-aware thread inspection and API health metadata.
- Extended Doctor with a real no-network workflow-core probe and added five regression tests for stage recovery, stale-thread replacement, disabled research, fail-closed safety and complete DailyWorkflow integration.

## v0.14.0 — dynamic top-level research parent graph

- Added a durable top-level LangGraph that owns candidate screening, native `Send` fan-out to an arbitrary Top-K set of symbol research branches, portfolio fan-in and final research aggregation.
- Preserved the verified v0.12 symbol graph and v0.13 Portfolio Supervisor graph as independently checkpointed staged sub-workflows rather than rewriting their internal logic.
- Added a dedicated `<workflow-run-id>:RESEARCH_PARENT` thread and separate SQLite checkpoint database, while retaining `<run-id>:<symbol>` and `<run-id>:PORTFOLIO` child threads.
- Verified parent-level pending-write recovery: when one dynamic candidate fails, successful candidate branches are not rerun and the Portfolio Supervisor executes once after recovery.
- Added strict candidate normalization, duplicate rejection, fan-in completeness validation and fail-closed behavior when screening returns no candidates.
- Added `research-parent-graph`, `make research-parent-graph`, protected `/research/parent-graph`, parent-aware thread inspection and parent-graph metadata in workflow plans/results.
- Kept the v0.13 sequential orchestration path behind `workflow.research_parent_graph_enabled: false` for controlled ablations and rollback.
- Added Doctor coverage and four dedicated regression tests for dynamic Top-K routing, parent recovery, fresh-thread isolation and full DailyWorkflow integration.

## v0.13.0 — cross-asset portfolio supervisor graph

- Added a second native LangGraph above the per-symbol research threads: parallel Correlation and Concentration reviewers, a deep Portfolio Supervisor, and a deterministic non-expansion guard.
- Added strict `PortfolioCommitteeReview`, `PortfolioSupervisorDecision`, `PortfolioAllocationItem` and `GuardedPortfolioAllocation` Pydantic schemas.
- Added point-in-time cross-asset context from synchronized closes, including annualized volatility, trailing return, pairwise correlation and high-correlation pair detection.
- Added three LangChain structured calls per standard workflow—two quick reviewers plus one deep supervisor—raising the Top-2 budget from 26 to 29 calls.
- Enforced deterministic portfolio safety after all model output: no symbol target may increase, HOLD cannot become BUY, protective SELL cannot be weakened, review caps are binding, high-correlation clusters are scaled, max positions and gross exposure are bounded.
- Persisted the portfolio graph as a dedicated `<workflow-run-id>:PORTFOLIO` SQLite thread while preserving immutable JSON artifacts for the market context, reviewers, proposal and guarded allocation.
- Verified native parallel pending-write recovery: when one portfolio reviewer fails, the successful reviewer is not rerun on resume.
- Applied the guarded allocation to research overlays before the existing Quant/Fusion/Regime and `PortfolioRiskGovernor` layers; the supervisor cannot replace or bypass hard risk.
- Added `portfolio-graph`, `make portfolio-graph`, protected `/research/portfolio-graph`, and portfolio-aware thread status summaries.
- Extended Doctor with a no-network portfolio graph probe and added five dedicated regression tests for non-expansion, correlation caps, protective SELL preservation, recovery and workflow integration.

## v0.12.0 — native LangChain and LangGraph research runtime

- Promoted `langchain`, `langgraph` and `langgraph-checkpoint-sqlite` from absent/optional concepts to required, fully locked production dependencies.
- Migrated the 13-call research workflow to a native `StateGraph` with parallel analyst branches, a cyclic Bull/Bear debate loop, conditional manager routing, parallel risk reviewers and a final human-review gate.
- Wrapped every structured research role in LangChain `PromptTemplate` and `RunnableLambda` composition while preserving provider-neutral clients and Pydantic validation.
- Added secret-free, thread-safe LangChain role traces containing task, tier, symbol, thread and latency without prompts, payloads, outputs or credentials.
- Switched graph execution to native `stream_mode="updates"` and added a separate secret-free graph event log containing only lifecycle events, thread IDs and node names.
- Added durable LangGraph SQLite threads, strict msgpack serialization, native super-step checkpoints and pending-write recovery; successful parallel siblings are not rerun after one node fails.
- Added optional graph-level human interrupts using `interrupt()` and `Command(resume=...)` for approve, reject or exposure-reduction decisions; the default remains the existing paper approval queue.
- Added optional cost-aware conditional routing that can finalize low-confidence HOLD decisions before Trader and risk-committee calls; it is disabled by default to preserve v0.11 behavior.
- Upgraded the JSON workflow audit store to schema v2 with thread-safe multi-node updates, while retaining immutable per-role artifacts alongside LangGraph executable checkpoints.
- Added Mermaid graph export, durable thread inspection, CLI/API HITL controls and protected `/research/graph` plus `/research/threads/{thread_id}` endpoints.
- Added Doctor probes for actual no-network LangGraph execution, SQLite checkpoint creation, LangChain traces, Mermaid generation and installed package versions.
- Added five dedicated LangGraph regression tests covering native graph execution, SQLite recovery, human interrupts, cost-aware routing and concurrent audit writes.

## v0.11.0 — tiered research graph, risk committee and memory feedback

- Replaced the fixed seven-role chain with an original configurable DAG containing quick-tier analysts, two rounds of deep-tier Bull/Bear debate, Research Manager, preliminary Trader, three risk personas and final Portfolio Manager.
- Added strict `RiskReview` and `DebateRound` schemas; risk reviewers cannot increase exposure, a BUY veto forces HOLD, and protective SELL decisions cannot be weakened.
- Added separate quick/deep model selection and cache namespaces while preserving the existing OpenAI-compatible Zhipu integration and structured-output validation.
- Increased the standard Top-2 research budget to 26 structured calls: 13 checkpointed nodes per candidate.
- Exposed the complete node dependency graph, model tier, risk personas, memory policy and exact call count in workflow plans.
- Persisted full research traces—analysts, debate rounds, manager, preliminary trader, risk reviews, final manager and model identities—inside immutable paper decision memories.
- Upgraded deterministic outcome attribution to v2 with committee-change, veto/reduction and model-tier metadata.
- Fed only matured, account-scoped and point-in-time-visible memories back into future debates, management and risk reviews, closing the cross-run learning loop without future leakage.
- Unified standalone CLI, FastAPI research and the daily workflow on the same tiered graph.

## v0.10.0 — captive-portal-safe live providers and unified observability

- Added a credential-free external-network preflight that detects captive portals, JavaScript redirects, TLS interception and offline states before any provider key is sent.
- Added fail-closed live execution with explicit `ONLINE`, `CAPTIVE_PORTAL`, `TLS_INTERCEPTED` and `OFFLINE` states; TLS verification cannot be disabled by configuration.
- Added a WAL-backed provider usage ledger covering data APIs and every structured GLM research role, with status, quota units, latency, cache hits and secret-free errors.
- Added provider runtime states including `LIVE_OK`, `CACHE_HIT`, `DEGRADED_STALE`, `NETWORK_BLOCKED`, `RATE_LIMITED`, `AUTH_FAILED` and `FAILED`.
- Added `provider-smoke` for minimal provider-by-provider validation and `provider-usage` plus `/providers/usage` for operations inspection.
- Added explicit `fail_closed` and bounded `last_known_good` policies; stale data is never silently presented as a successful live refresh.
- Changed Twelve Data authentication from query-string keys to its documented `Authorization: apikey ...` header and partitioned HTTP caches by an authorization hash rather than the secret.
- Added gzip/deflate HTTP response support with post-decompression size limits for SEC and other compressed JSON APIs.
- Corrected live refresh behavior: news now starts with the latest articles, while market and news updates use overlap-based incremental windows instead of repeatedly requesting full history.
- Added safe GLM response telemetry for response model, finish reason and token usage without logging prompts, outputs or credentials.
- Diagnosed the first real provider attempt as an unauthenticated NJU captive portal; the workflow now stops at `network_gate` and can resume after network authentication without replaying stable nodes.
- Expanded the complete suite to 80 passing tests covering network interception, volatile resume, provider accounting, compressed responses and minimal live smoke contracts.

## v0.9.0 — resumable workflow, decision memory and authenticated operations

- Added atomic workflow state with one checkpoint per data provider and per-symbol research role; failed runs resume by `run_id` without re-executing completed nodes.
- Added strict resume guards using the complete settings SHA-256 fingerprint and checkpoint schema version.
- Added SQLite `PRAGMA user_version` migrations and explicit rejection of databases newer than the supported schema.
- Added immutable structured decision memories linked to paper runs, including action, approved target, evidence IDs and deterministic rationale fields.
- Added local outcome maturation after a five-session horizon with raw return, benchmark return, Alpha, failure classification and non-LLM reflection.
- Added `paper-memories`, `workflow-status`, and `workflow-run --run-id --resume` CLI operations plus matching read-only API endpoints.
- Added fail-closed API authentication for every non-public endpoint using `TRADINGLAB_API_TOKEN`; the token is generated and stored only in the external chmod-600 key file.
- Unified API, CLI and scheduler paper mutations under one sanitized per-account cross-process lock.
- Extended Doctor with external-secret metadata, API-token presence and SQLite schema-version checks without exposing values.
- Upgraded Compose and source deployment metadata to v0.9.0 while preserving local-only binding and paper-trading-only execution.
- Expanded the complete offline suite to 70 passing tests, including injected agent failure/resume, migration compatibility, API authentication and decision-memory maturation.

## v0.8.0 — GLM dry-run orchestration and deployment framework

- Selected the official free `glm-4.7-flash` model and Zhipu OpenAI-compatible endpoint as the planned remote research provider.
- Separated provider selection from execution mode: `dry_run` is the default, while `live` requires explicit confirmation and a repository-external key.
- Added a no-network GLM client that exercises all seven structured research calls, schema validation, caching and audit logging without consuming quota.
- Added the end-to-end `DailyWorkflow`: data refresh, quant candidate screening, two-candidate structured research, conservative research overlay, portfolio risk and internal paper execution.
- Added application-level model call budgeting at 14 calls per workflow and official provider request planning.
- Added incremental merge writers for market, news, FRED vintages and SEC filings instead of overwriting prior history.
- Added Alpha Vantage and FRED live-mode throttling, explicit live confirmation and local-only management API binding.
- Added `workflow-plan`, `workflow-run`, Make targets, no-network FastAPI endpoints and Docker Compose dry-run/live profiles.
- Extended the external credential installer for `ZHIPU_API_KEY`; no real key is stored in Git or generated artifacts.
- Expanded the complete offline suite to 64 tests covering GLM request contracts, dry-run safety, call budgets, conservative overlays and incremental data merging.

## v0.7.0 — P3 persistent internal paper trading

- Added a WAL-backed SQLite ledger for paper accounts, positions, daily runs, target-weight orders, approval events, fills and equity snapshots.
- Added explicit account, approval-policy, order and daily-run state machines with terminal transition validation.
- Added restart-safe `PaperTradingService`: approved orders execute at the synchronized next open, close marks are persisted and the next approval queue is generated after close.
- Added order expiry when approval misses the scheduled open, plus `ALL`, `RISK_AUTO` and `NONE` approval policies.
- Added deterministic daily-run and order IDs, same-session idempotency and phase-aware crash recovery after committed open fills.
- Persisted Regime Guard hysteresis and portfolio circuit-breaker state across process restarts.
- Added a non-blocking one-shot scheduler lock suitable for cron, systemd timers or future containers.
- Added CLI commands for account initialization, inspection, session runs, queue listing, approval, rejection, cancellation and dashboard rendering.
- Added local FastAPI endpoints for paper accounts, sessions, orders and an HTML dashboard; all responses remain simulation-only and no real broker connector exists.
- Expanded the complete suite to 55 passing tests, including API lifecycle, scheduler overlap, stale-order expiry and post-open crash recovery.

## v0.6.0 — P2 synchronized multi-asset portfolios

- Added strict `MarketSnapshot` validation and removed zero-price fallback valuation for missing holdings.
- Added synchronized `AlignedMarketData` over common timestamps with complete open and close snapshots.
- Extended `Portfolio` with strict multi-asset equity, weights and gross-exposure calculations.
- Added multi-asset `PaperBroker.rebalance_many()` with reductions-before-increases execution and negative-cash protection.
- Added `PortfolioRiskGovernor` with per-position caps, maximum position count, gross-exposure scaling and full-portfolio drawdown liquidation.
- Added `MultiAssetBacktestEngine` with per-symbol analysis, portfolio-wide allocation, next-common-open execution and execution-time exposure audit.
- Added deterministic 2018-2025 synthetic fixtures for SPY, QQQ, AAPL, MSFT and NVDA plus `multi-backtest` CLI/Make targets.
- Added P2 regression coverage; the complete suite now contains 45 passing tests.

## v0.5.0 — P0 reliability and P1 structured LLM agents

- Fixed the maximum-drawdown circuit breaker with an explicit `ACTIVE → LIQUIDATING → HALTED` state machine and mandatory liquidation path.
- Added regression coverage proving that a rejected risk decision can still force a protective close.
- Replaced silently ignored YAML options with a strict, path-aware configuration schema and reduced the default config to implemented fields.
- Standardized the project on Python 3.11 with `.python-version`, `requirements.lock`, unified Make targets and Docker lock installation.
- Added Pydantic structured-output schemas with forbidden extra fields and cross-field order/weight validation.
- Added deterministic `MockLLM`, DeepSeek/OpenAI-compatible client, content-addressed file cache and key-safe JSONL call logging.
- Split news, macro and fundamental analysis into separate agents and input contracts.
- Added Bull Researcher, Bear Researcher, Research Manager and Trader roles with Evidence ID validation and a 20% P1 exposure ceiling.
- Added the `research` CLI command; all directional paper intents require human approval and no real broker call is made.
- Expanded the suite to 35 passing tests and added Ruff static checks.

## v0.4.0 — free real-data integrations

- Added a dependency-free HTTPS JSON client with caching, retries, response-size limits and API-key redaction.
- Added Twelve Data daily OHLCV integration and export to the project CSV schema.
- Added Alpha Vantage news/sentiment integration and point-in-time JSONL export.
- Added FRED vintage-aware macro observations using earliest realtime availability.
- Added SEC EDGAR ticker resolution and Company Facts extraction using filing dates.
- Added generic macro/fundamental evidence providers to the backtest and FastAPI request model.
- Added CLI fetch commands and environment-variable configuration.
- Expanded the test suite to 27 unit/integration checks with mocked official API responses.

## v0.3.0 — course-ready research build

- Added Regime Guard Agent with trailing-only classification and hysteresis.
- Added `without_regime_guard` ablation.
- Added deterministic bull, bear, sideways and volatile stress scenarios.
- Added regime-specific metrics and multi-scenario benchmark aggregation.
- Added automated temporal, structural and evidence-citation audit.
- Added SHA-256 input provenance, configuration hash and Git/runtime manifest.
- Added versioned run bundles with JSON, Markdown, HTML, manifest and audit files.
- Added SQLite experiment registry and CLI run browsing.
- Added dependency-free HTML report with inline SVG equity curves.
- Expanded FastAPI with persisted experiments, run history and latest-report endpoint.
- Added strict settings validation and environment doctor.
- Expanded the test suite to 19 unit/integration checks.

## v0.2.0 — auditable multi-agent experiment

- Added Context Agent, structured MockLLM and Critic Agent.
- Added news point-in-time provider and evidence validation.
- Added baseline and ablation experiments.
- Added complete risk/return metrics.
- Added FastAPI, Docker assets and deployment documentation.
- Added next-open execution timestamp semantics.

## v0.1.0 — offline MVP

- Added local OHLCV provider, feature engine and Quant Agent.
- Added Risk Governor, Paper Broker and Backtest Engine.
- Added sample data generator and basic tests.

## Initial design

- Documented reference-project analysis, architecture decisions and implementation roadmap.
