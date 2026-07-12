from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.evaluation.experiments import run_experiment_suite


def run_scenario_benchmark(
    scenario_dir: str | Path,
    settings: BacktestSettings,
) -> dict:
    root = Path(scenario_dir)
    csv_files = sorted(path for path in root.glob("*.csv") if not path.name.endswith("_news.csv"))
    if not csv_files:
        raise ValueError(f"no scenario CSV files found in {root}")

    scenarios = []
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for csv_path in csv_files:
        scenario = csv_path.stem
        news_path = root / f"{scenario}_news.jsonl"
        provider = LocalCsvProvider(csv_path, scenario.upper())
        news = LocalNewsProvider(news_path) if news_path.exists() else None
        experiment = run_experiment_suite(provider, settings, news)
        scenarios.append(
            {
                "scenario": scenario,
                "csv": str(csv_path),
                "news": str(news_path) if news_path.exists() else None,
                "audit_passed": experiment["audit"]["passed"],
                "summary": experiment["summary"],
            }
        )
        for row in experiment["summary"]:
            by_variant[row["name"]].append({"scenario": scenario, **row})

    aggregate = []
    for variant, rows in sorted(by_variant.items()):
        aggregate.append(
            {
                "name": variant,
                "scenario_count": len(rows),
                "average_return": fmean(row["total_return"] for row in rows),
                "median_return": sorted(row["total_return"] for row in rows)[len(rows) // 2],
                "worst_return": min(row["total_return"] for row in rows),
                "average_sharpe": fmean(row["sharpe"] for row in rows),
                "worst_drawdown": max(row["max_drawdown"] for row in rows),
                "average_turnover": fmean(row["turnover"] for row in rows),
                "positive_scenarios": sum(row["total_return"] > 0 for row in rows),
            }
        )

    full_rows = by_variant.get("full_agent", [])
    return {
        "schema_version": 1,
        "scenario_dir": str(root),
        "settings": settings.__dict__,
        "scenario_count": len(scenarios),
        "all_audits_passed": all(item["audit_passed"] for item in scenarios),
        "full_agent_positive_scenarios": sum(row["total_return"] > 0 for row in full_rows),
        "scenarios": scenarios,
        "aggregate": aggregate,
    }


def render_benchmark_markdown(result: dict) -> str:
    lines = [
        "# TradeLab-Agent 多场景压力测试",
        "",
        f"场景数量：{result['scenario_count']}；全部审计通过：`{result['all_audits_passed']}`。",
        "",
        "| 方案 | 平均收益 | 中位收益 | 最差收益 | 平均 Sharpe | 最差回撤 | 平均换手 | 正收益场景 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["aggregate"]:
        lines.append(
            f"| {row['name']} | {row['average_return']:.2%} | {row['median_return']:.2%} | "
            f"{row['worst_return']:.2%} | {row['average_sharpe']:.3f} | "
            f"{row['worst_drawdown']:.2%} | {row['average_turnover']:.2f} | "
            f"{row['positive_scenarios']}/{row['scenario_count']} |"
        )
    lines.extend(["", "## 分场景结果", ""])
    for scenario in result["scenarios"]:
        lines.extend(
            [
                f"### {scenario['scenario']}",
                "",
                "| 方案 | 收益 | Sharpe | 最大回撤 | 成交数 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in scenario["summary"]:
            lines.append(
                f"| {row['name']} | {row['total_return']:.2%} | {row['sharpe']:.3f} | "
                f"{row['max_drawdown']:.2%} | {row['trade_count']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 解释边界",
            "",
            "- 四个场景均由固定随机种子生成，用于压力测试而非盈利证明。",
            "- 每个方案在同一场景中使用相同成本、暖启动窗口和输入数据。",
            "- 真实课程结论仍需使用真实历史行情复验。",
            "",
        ]
    )
    return "\n".join(lines)


def save_benchmark(result: dict, output_json: str | Path, output_md: str | Path) -> None:
    json_path = Path(output_json)
    markdown_path = Path(output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_benchmark_markdown(result), encoding="utf-8")
