# Changelog

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
