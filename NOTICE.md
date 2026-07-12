# Originality and Reference Notice

TradeLab-Agent is an independent course implementation inspired by the system-level idea of multi-agent financial analysis in TauricResearch/TradingAgents.

## Referenced ideas

The project retains only broad architectural ideas that are common in agent systems:

- separate analyst responsibilities;
- structured state passed between modules;
- tool/data isolation;
- point-in-time market and news access;
- comparison through backtesting.

## Independently designed and implemented elements

The following components were designed and written specifically for this course project:

- the four-agent topology: Quant, Context, Critic, and Regime Guard;
- deterministic EvidencePack validation and evidence citation audit;
- Regime Guard exposure multipliers and confirmation/hysteresis mechanism;
- deterministic Risk Governor and its continuous position-cap enforcement;
- close-decision/next-open execution semantics;
- offline CSV/JSONL providers and deterministic scenario generators;
- baseline and module-ablation experiment suite;
- four-regime stress benchmark;
- run manifest, SHA-256 provenance and audit score;
- SQLite experiment registry;
- dependency-free HTML report generator;
- course-oriented CLI, FastAPI surface and environment doctor.

## Code boundary

No source files from the reference repository were copied into `src/tradinglab_agents`. The unpacked reference repository is kept outside this project under `../reference/TradingAgents` solely for architecture study and comparison.

The project deliberately avoids reproducing the reference repository's LangGraph workflow, multi-provider LLM adapters, Redis/checkpoint system, role prompts, dataflow implementations and multi-round debate topology.

## Use boundary

This software is for educational research and paper-trading simulation only. It does not provide investment advice or real-broker execution.
