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
    alpha_budget = alpha["application_budget"]
    alpha_news = alpha_budget["expected_news_requests_per_run"]
    alpha_market_fallback = alpha_budget[
        "maximum_market_fallback_requests_per_run"
    ]
    alpha_max = alpha_budget["maximum_requests_per_run"]
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
        f"expected news={alpha_news}, reserved market fallback={alpha_market_fallback}, "
        f"worst case={alpha_max}/{alpha_daily} daily requests "
        f"({_percent(alpha_max, alpha_daily)}), "
        f"shared spacing={alpha['schedule']['shared_minimum_interval_seconds']}s"
    )
    print(
        "FRED: "
        f"up to {fred['application_budget']['maximum_series_per_refresh']} series, "
        f"minimum spacing {fred['application_budget']['minimum_interval_seconds']}s"
    )
    zhipu_budget = zhipu["application_budget"]
    zhipu_candidates = zhipu_budget["candidate_symbols_per_workflow"]
    zhipu_per_candidate = zhipu_budget["requests_per_candidate"]
    zhipu_portfolio = zhipu_budget["portfolio_supervisor_requests"]
    zhipu_max = zhipu_budget["maximum_requests_per_workflow"]
    print(
        "Zhipu: "
        f"quick={zhipu['quick_model']}, deep={zhipu['deep_model']} "
        f"({zhipu['official_status']}), "
        f"{zhipu_candidates} candidates x {zhipu_per_candidate} nodes + "
        f"{zhipu_portfolio} portfolio-supervisor calls = "
        f"{zhipu_max} max requests/workflow, "
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
    if alpha_news + alpha_market_fallback != alpha_max:
        raise SystemExit(
            "Alpha Vantage maximum does not match news plus market fallback reserve"
        )
    if alpha_max > alpha_daily:
        raise SystemExit("Alpha Vantage scheduled run exceeds daily requests")
    if zhipu_candidates * zhipu_per_candidate + zhipu_portfolio != zhipu_max:
        raise SystemExit(
            "Zhipu workflow budget does not match per-symbol plus portfolio calls"
        )


if __name__ == "__main__":
    main()
