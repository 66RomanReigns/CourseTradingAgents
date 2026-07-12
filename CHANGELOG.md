# Changelog

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
