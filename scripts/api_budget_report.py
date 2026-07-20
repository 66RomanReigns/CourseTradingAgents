from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config" / "api_budget.yaml"


def _percent(used: float, limit: float) -> str:
    return f"{used / limit:.1%}" if limit else "n/a"


def main() -> None:
    data = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
    providers = data["providers"]
    universe = data["universe"]["symbols"]

    twelve = providers["twelve_data"]
    twelve_used = twelve["application_budget"]["expected_credits_per_run"]
    twelve_daily = twelve["documented_free_limits"]["api_credits_per_day"]
    twelve_minute = twelve["documented_free_limits"]["api_credits_per_minute"]

    alpha = providers["alpha_vantage"]
    alpha_used = alpha["application_budget"]["expected_requests_per_run"]
    alpha_daily = alpha["documented_free_limits"]["requests_per_day"]

    fred = providers["fred"]
    zhipu = providers["zhipu"]
    gemini = providers["gemini"]

    print("TradeLab-Agent API budget")
    print(f"universe: {','.join(universe)} ({len(universe)} symbols)")
    print(
        "Twelve Data: "
        f"{twelve_used}/{twelve_daily} daily credits ({_percent(twelve_used, twelve_daily)}), "
        f"{twelve_used}/{twelve_minute} credits in the scheduled minute"
    )
    print(
        "Alpha Vantage: "
        f"{alpha_used}/{alpha_daily} daily requests ({_percent(alpha_used, alpha_daily)})"
    )
    print(
        "FRED: "
        f"up to {fred['application_budget']['maximum_series_per_refresh']} series, "
        f"minimum spacing {fred['application_budget']['minimum_interval_seconds']}s"
    )
    print(
        "Zhipu: "
        f"{zhipu['selected_model']} ({zhipu['official_status']}), "
        f"{zhipu['application_budget']['maximum_requests_per_workflow']} max requests/workflow, "
        f"default mode {zhipu['execution_policy']['default_mode']}"
    )
    print(
        "Gemini: disabled by default; "
        f"application cap {gemini['application_budget']['maximum_requests_per_day']} requests/day, "
        f"preferred model {gemini['application_budget']['preferred_model']}"
    )
    print("binding quota: Alpha Vantage")
    print("backtests/tests: offline, zero external requests")

    if twelve_used > twelve_minute:
        raise SystemExit("Twelve Data scheduled run exceeds per-minute credits")
    if twelve_used > twelve_daily:
        raise SystemExit("Twelve Data scheduled run exceeds daily credits")
    if alpha_used > alpha_daily:
        raise SystemExit("Alpha Vantage scheduled run exceeds daily requests")


if __name__ == "__main__":
    main()
