from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.evidence_provider import LocalPointInTimeEvidenceProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.audit import audit_experiment
from tradinglab_agents.evaluation.baselines import run_buy_and_hold, run_sma_cross
from tradinglab_agents.evaluation.regimes import attach_regime_metrics
from tradinglab_agents.reporting.html_report import render_experiment_html
from tradinglab_agents.reporting.provenance import build_manifest


VARIANT_KEYS = (
    "total_return",
    "annualized_return",
    "sharpe",
    "max_drawdown",
    "turnover",
    "trade_count",
    "fees",
)


def run_experiment_suite(
    provider: LocalCsvProvider,
    settings: BacktestSettings,
    news_provider: LocalNewsProvider | None = None,
    evidence_providers: Sequence[LocalPointInTimeEvidenceProvider] | None = None,
) -> dict:
    evidence_providers = tuple(evidence_providers or ())
    variants = [
        BacktestEngine(settings).run(
            provider, news_provider, "full_agent", evidence_providers
        ),
        BacktestEngine(replace(settings, enable_context=False)).run(
            provider, None, "quant_plus_critic"
        ),
        BacktestEngine(replace(settings, enable_context=False, enable_critic=False)).run(
            provider, None, "quant_only"
        ),
        BacktestEngine(replace(settings, enable_risk=False)).run(
            provider, news_provider, "without_risk_governor", evidence_providers
        ),
        BacktestEngine(replace(settings, enable_regime_guard=False)).run(
            provider, news_provider, "without_regime_guard", evidence_providers
        ),
        run_sma_cross(
            provider,
            initial_cash=settings.initial_cash,
            warmup_bars=settings.warmup_bars,
            commission_bps=settings.commission_bps,
            slippage_bps=settings.slippage_bps,
        ),
        run_buy_and_hold(
            provider,
            initial_cash=settings.initial_cash,
            warmup_bars=settings.warmup_bars,
            commission_bps=settings.commission_bps,
            slippage_bps=settings.slippage_bps,
        ),
    ]
    for variant in variants:
        attach_regime_metrics(variant, provider)

    summary = []
    for result in variants:
        row = {"name": result["name"]}
        row.update({key: result["metrics"][key] for key in VARIANT_KEYS})
        summary.append(row)
    experiment = {
        "schema_version": 2,
        "symbol": provider.symbol,
        "settings": settings.__dict__,
        "summary": summary,
        "variants": variants,
    }
    experiment["audit"] = audit_experiment(experiment)
    return experiment


def render_markdown(result: dict) -> str:
    lines = [
        "# TradeLab-Agent 实验结果",
        "",
        f"标的：`{result['symbol']}`",
        "",
        "| 方案 | 总收益 | 年化收益 | Sharpe | 最大回撤 | 换手率 | 成交数 | 手续费 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["summary"]:
        lines.append(
            "| {name} | {total_return:.2%} | {annualized_return:.2%} | {sharpe:.3f} | "
            "{max_drawdown:.2%} | {turnover:.2f} | {trade_count} | {fees:.2f} |".format(**row)
        )

    full = next((item for item in result["variants"] if item["name"] == "full_agent"), None)
    if full:
        lines.extend(
            [
                "",
                "## 完整智能体分市场状态表现",
                "",
                "| 状态 | 样本数 | 区间收益 | 最大回撤 | 正收益步比例 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for regime, metrics in full.get("regime_metrics", {}).items():
            lines.append(
                f"| {regime} | {metrics['observations']} | {metrics['return']:.2%} | "
                f"{metrics['max_drawdown']:.2%} | {metrics['positive_step_rate']:.2%} |"
            )

    audit = result.get("audit", {})
    lines.extend(
        [
            "",
            "## 自动审计",
            "",
            f"- 审计结果：`{'PASS' if audit.get('passed') else 'FAIL'}`",
            f"- 最低审计分数：{audit.get('minimum_audit_score', 0.0):.3f}",
            f"- 最低证据引用覆盖率：{audit.get('minimum_citation_coverage', 0.0):.2%}",
            "",
            "## 解释原则",
            "",
            "- 所有智能体变体使用相同数据、起始资金、暖启动窗口和交易成本。",
            "- 信号在当前收盘后形成，只允许在下一根 K 线开盘成交。",
            "- 市场状态标签仅使用当时及此前的行情，不使用未来数据。",
            "- `without_risk_governor` 与 `without_regime_guard` 是模块消融，不代表推荐交易方式。",
            "- 合成数据仅用于验证系统逻辑，不用于证明真实市场盈利能力。",
            "",
        ]
    )
    return "\n".join(lines)


def save_experiment(result: dict, output_json: str | Path, output_md: str | Path) -> None:
    json_path = Path(output_json)
    md_path = Path(output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")


def save_run_bundle(
    result: dict,
    project_root: str | Path,
    settings: BacktestSettings,
    input_files: list[str | Path],
    artifacts_root: str | Path = "artifacts/runs",
) -> dict:
    root = Path(project_root).resolve()
    manifest = build_manifest(root, settings, input_files, run_type="experiment_suite")
    result["manifest"] = manifest
    result["audit"] = audit_experiment(result)

    runs_root = Path(artifacts_root)
    if not runs_root.is_absolute():
        runs_root = root / runs_root
    run_dir = runs_root / manifest["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)

    json_path = run_dir / "experiment.json"
    markdown_path = run_dir / "report.md"
    html_path = run_dir / "report.html"
    manifest_path = run_dir / "manifest.json"
    audit_path = run_dir / "audit.json"

    json_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    html_path.write_text(
        render_experiment_html(result, manifest=manifest, audit=result["audit"]),
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    audit_path.write_text(json.dumps(result["audit"], indent=2), encoding="utf-8")

    latest = runs_root.parent / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for source in (json_path, markdown_path, html_path, manifest_path, audit_path):
        shutil.copy2(source, latest / source.name)
    (runs_root.parent / "LATEST_RUN.txt").write_text(manifest["run_id"] + "\n", encoding="utf-8")

    return {
        "run_id": manifest["run_id"],
        "run_dir": str(run_dir),
        "json": str(json_path),
        "markdown": str(markdown_path),
        "html": str(html_path),
        "manifest": str(manifest_path),
        "audit": str(audit_path),
    }
