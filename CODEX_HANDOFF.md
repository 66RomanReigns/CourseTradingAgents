# Local Codex Handoff — TradeLab-Agent v0.18.0

This document is the source of truth for continuing the project on a local machine with Codex.

## 1. Current release

```text
Project: tradinglab-agents
Version: 0.18.0
Python: >=3.11,<3.13
Default branch: main
Current release commit: created from the accumulated v0.10–v0.18 work
Real broker: disabled
Default LLM execution: dry_run
```

The latest architecture contains these persistent LangGraph boundaries:

```text
<run-id>:DATA_PROVIDER
<run-id>:WORKFLOW_CORE
<run-id>:RESEARCH_PARENT
<run-id>:<SYMBOL>
<run-id>:PORTFOLIO
<paper-run-id>:DECISION
```

Do not merge real broker execution, approval persistence or account mutation into these graphs without a separate transaction/idempotency design review.

## 2. Recommended local environment

For Windows, use **WSL 2 Ubuntu**. The repository uses Bash, Make, Unix file permissions and SQLite paths. Native PowerShell can work, but WSL is the supported handoff path.

Install prerequisites inside WSL:

```bash
sudo apt update
sudo apt install -y git make curl ca-certificates python3.11 python3.11-venv docker-compose-plugin
curl -LsSf https://astral.sh/uv/install.sh | sh
exec "$SHELL"
```

Clone and create the locked environment:

```bash
git clone <GITHUB_REPOSITORY_URL>
cd CourseTradingAgents
make sync PYPI_INDEX=https://pypi.org/simple
```

## 3. Mandatory offline baseline

Run these before touching any live provider code:

```bash
make check
make doctor
make api-budget
TRADINGLAB_KEYS_FILE=/tmp/tradinglab.keys docker compose config --quiet
```

Expected baseline:

```text
135 tests pass
Ruff passes
compileall passes
Doctor ok = true
Backtests/tests external requests = 0
Real broker = false
```

If the test count changes, explain why in the commit message and update this document.

## 4. Secret configuration

Secrets must remain outside the repository.

Run:

```bash
chmod +x scripts/configure_api_keys.sh scripts/with_api_keys.sh
./scripts/configure_api_keys.sh
./scripts/with_api_keys.sh
```

Default secret file:

```text
~/.config/tradinglab/tradinglab.keys
```

Required variables:

```text
TWELVE_DATA_API_KEY
ALPHA_VANTAGE_API_KEY
FRED_API_KEY
SEC_USER_AGENT
GOOGLE_API_KEY
ZHIPU_API_KEY
ZHIPU_BASE_URL
ZHIPU_MODEL
TRADINGLAB_API_TOKEN
```

`SEC_USER_AGENT` must identify the application and include a contact email, for example:

```text
TradeLab-Agent/0.18 local-test your-email@example.com
```

Never paste secret values into Codex chat, source files, test snapshots, terminal transcripts committed to Git, SQLite rows or GitHub Actions variables unless intentionally using GitHub encrypted secrets later.

## 5. Network preflight

Before any credentialed call:

```bash
make doctor
```

Proceed only when the connectivity probe reports an online verified HTTPS path. Required safety condition:

```text
network state = ONLINE
credentials_sent = false during preflight
```

Do not disable TLS verification, ignore certificate errors or bypass captive-portal detection.

## 6. Quota-conscious real API validation

Test one provider at a time. Each command requires explicit confirmation and should consume only the documented smoke-test request.

```bash
CONFIRM_LIVE=1 PROVIDER=twelve_data SYMBOL=AAPL make provider-smoke
CONFIRM_LIVE=1 PROVIDER=alpha_vantage SYMBOL=AAPL make provider-smoke
CONFIRM_LIVE=1 PROVIDER=fred SYMBOL=AAPL make provider-smoke
CONFIRM_LIVE=1 PROVIDER=sec_edgar SYMBOL=AAPL make provider-smoke
CONFIRM_LIVE=1 PROVIDER=zhipu SYMBOL=AAPL make provider-smoke
```

After every call inspect:

```bash
make provider-usage
make provider-health
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-events --config config/default.yaml
```

Acceptance criteria for each provider:

```text
HTTP/TLS request succeeds
canonical schema validation succeeds
point-in-time timestamps are valid
usage ledger records exactly the expected units
no key appears in URL, output, logs or databases
real_broker_connected = false
```

Do not repeatedly retry 401, 403 or 429 responses. Investigate configuration or persisted cooldown state first.

## 7. Provider Graph live integration

Only after all individual smoke tests pass, run the full live data refresh:

```bash
./scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli workflow-run \
  --mode live --confirm-live --config config/default.yaml
```

Then inspect:

```bash
make provider-usage
make provider-health
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-conflicts \
  --config config/default.yaml
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli market-validate \
  --data-dir artifacts/live_data --config config/default.yaml
```

The run must create a `<run-id>:DATA_PROVIDER` thread. Confirm:

```text
all enabled resources have route results
selected providers and fallback depths are visible
quality summary is NORMAL or explicitly DEGRADED
BLOCKED data is not persisted
DEGRADED data cannot expand positions
no external broker call exists
```

If Provider Graph is DEGRADED, Research may continue for explanation, but every BUY overlay must become HOLD. Protective SELL decisions must remain possible.

## 8. API service validation

Start the local authenticated API:

```bash
./scripts/with_api_keys.sh env PYTHONPATH=src \
  .venv/bin/python -m uvicorn tradinglab_agents.api.app:app \
  --host 127.0.0.1 --port 8000
```

From another WSL terminal:

```bash
curl http://127.0.0.1:8000/health

./scripts/with_api_keys.sh bash -c '
  curl -H "X-API-Key: $TRADINGLAB_API_TOKEN" \
    http://127.0.0.1:8000/providers/capabilities
'
```

Validate these protected endpoints:

```text
/providers/capabilities
/providers/health
/providers/events
/providers/conflicts
/market/data-quality
/market/calendar
/market/semantics
/workflow/data-provider-graph
/workflow/core-graph
/workflow/decision-graph
/research/threads/{thread_id}
```

Expected authentication behavior:

```text
/health without token -> 200
protected endpoint without token -> 401
protected endpoint with token -> 200
```

## 9. Main invariants Codex must preserve

1. Backtests and ordinary tests never call external providers.
2. Provider keys are loaded lazily and never logged.
3. Network preflight is credential-free.
4. Fallback, stale cache and conflicts can only reduce risk.
5. `BLOCKED` data is never persisted into the formal dataset.
6. Raw prices are used for execution; total-return-adjusted history is used for Quant/Regime features.
7. Paper corporate actions are idempotent under `UNIQUE(account_id, action_id)`.
8. Research and Decision graphs do not mutate Paper accounts or persist orders.
9. All directional Paper orders require the configured approval policy.
10. There is no real broker integration.
11. Candidate research budget remains 29 LLM calls by default.
12. Resume must reject changed input or policy fingerprints.

## 10. Important files

```text
src/tradinglab_agents/data/provider_registry.py
src/tradinglab_agents/data/provider_quality.py
src/tradinglab_agents/data/fallback_router.py
src/tradinglab_agents/data/provider_adapters.py
src/tradinglab_agents/storage/provider_health.py
src/tradinglab_agents/workflows/data_provider_graph.py
src/tradinglab_agents/workflows/workflow_core.py
src/tradinglab_agents/workflows/research_parent.py
src/tradinglab_agents/engine/decision_graph.py
src/tradinglab_agents/engine/trading_calendar.py
src/tradinglab_agents/data/corporate_actions.py
src/tradinglab_agents/paper/service.py
src/tradinglab_agents/storage/paper_store.py
config/default.yaml
config/api_budget.yaml
scripts/doctor.py
scripts/api_budget_report.py
```

Architecture documents are under `docs/16_...` through `docs/24_...`.

## 11. Recommended Codex workflow

For every change:

```text
inspect relevant code and tests
state the invariant being changed
add or update a focused regression test
run the focused test
run make check
run make doctor
run secret scan or inspect git diff for credentials
show git diff --check
commit only after all checks pass
```

Do not use real provider calls as a substitute for deterministic unit tests. Implement fake or injected Provider candidates first, then run one quota-conscious smoke call.

## 12. GitHub publishing from the local machine

If the repository has not yet been created on GitHub:

```bash
gh auth login
gh repo create CourseTradingAgents --private --source=. --remote=origin --push
```

If `origin` already exists:

```bash
git push -u origin main
git push origin v0.18.0
```

Keep the repository private until the owner explicitly decides to make it public.

## 13. Completion report expected from Codex

Ask Codex to return:

```text
OS and Python versions
commit hash tested
offline test count and result
Doctor result
network preflight state
one result per real provider
usage units consumed
Provider health transitions
any conflicts or fallbacks observed
API authentication checks
secret scan result
files changed
remaining unverified assumptions
```
