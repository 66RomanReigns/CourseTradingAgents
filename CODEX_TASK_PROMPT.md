# Prompt for Local Codex

You are taking over the TradeLab-Agent repository at v0.18.0. Read `CODEX_HANDOFF.md`, `README.md`, `CHANGELOG.md`, `config/default.yaml`, `config/api_budget.yaml`, and `docs/16_*.md` through `docs/24_*.md` before changing code.

Your immediate goal is to validate the real external API paths on this local computer while preserving all existing safety and reproducibility guarantees.

## Required sequence

1. Inspect the repository and report the current branch, commit, Python version and operating environment.
2. Use WSL 2 Ubuntu on Windows unless the repository is already running in a compatible Linux environment.
3. Create the locked environment with:

   ```bash
   make sync PYPI_INDEX=https://pypi.org/simple
   ```

4. Run the complete offline baseline before using any API key:

   ```bash
   make check
   make doctor
   make api-budget
   ```

5. Stop and diagnose any offline failure before attempting live calls. Do not weaken tests or remove safety checks just to make them pass.
6. Confirm secrets are stored only in `~/.config/tradinglab/tradinglab.keys`, permissions are `600`, and no value is printed or committed.
7. Confirm the credential-free network preflight reports a verified online HTTPS path. Never disable TLS verification or bypass captive-portal detection.
8. Run exactly one smoke request at a time for:

   ```text
   twelve_data
   alpha_vantage
   fred
   sec_edgar
   zhipu
   ```

   Use the existing `make provider-smoke` command with `CONFIRM_LIVE=1`. Do not loop aggressively after 401, 403, 429, timeout or schema errors.
9. After every provider call inspect the usage and health ledgers. Verify the expected quota units, canonical schema, point-in-time timestamps, no secret leakage and `real_broker_connected=false`.
10. Add or update deterministic regression tests for every bug found during real API validation. Fake/injected provider tests must exist before relying on repeated real calls.
11. After individual providers pass, run one confirmed live Workflow so that the `<run-id>:DATA_PROVIDER` graph routes and persists all enabled resources.
12. Verify that:

   ```text
   NORMAL quality may permit position expansion
   DEGRADED quality forces every BUY overlay to HOLD
   BLOCKED quality is not persisted and cannot enter Research
   protective SELL behavior is never weakened
   no real broker is connected
   ```

13. Start the local FastAPI service and verify `/health` is public while all management routes require `X-API-Key`.
14. Run `make check`, `make doctor`, `git diff --check`, and a secret scan again after any code change.
15. Do not commit or push until all checks pass. Use small, descriptive commits.

## Invariants you must not break

- Offline tests and backtests make zero external requests.
- Provider credentials are lazy-loaded and never logged.
- Provider Graph fallback can only reduce risk.
- Data conflict resolution is deterministic; LLMs cannot override a major conflict.
- Raw prices are used for execution and valuation; adjusted total-return history is used for Quant and Regime features.
- Corporate-action posting is idempotent.
- Research and Decision LangGraphs do not mutate accounts, persist orders or call brokers.
- Default LLM budget remains 29 requests per Workflow.
- Resume rejects changed input and policy fingerprints.
- Real broker integration remains disabled.

## Deliverable

Return a structured report containing:

```text
commit hash tested
offline tests passed / total
Doctor result
network preflight result
provider-by-provider smoke result
quota units consumed
health-state changes
fallbacks and conflicts observed
live DATA_PROVIDER thread id and checkpoint count
API authentication results
secret scan result
code changes and tests added
remaining unverified assumptions
```

Do not claim a real API path works unless you executed it successfully and can cite the corresponding local command output or ledger entry.
