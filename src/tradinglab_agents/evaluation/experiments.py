from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tradinglab_agents.config import BacktestSettings
from tradinglab_agents.data.csv_provider import LocalCsvProvider
from tradinglab_agents.data.news_provider import LocalNewsProvider
from tradinglab_agents.engine.backtest import BacktestEngine
from tradinglab_agents.evaluation.baselines import run_buy_and_hold, run_sma_cross


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
) -> dict:
    variants = [
        BacktestEngine(settings).run(provider, news_provider, "full_agent"),
        BacktestEngine(replace(settings, enable_context=False)).run(
            provider, None, "quant_plus_critic"
        ),
        BacktestEngine(replace(settings, enable_context=False, enable_critic=False)).run(
            provider, None, "quant_only"
        ),
        BacktestEngine(replace(settings, enable_risk=False)).run(
            provider, news_provider, "without_risk_governor"
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
    summary = []
    for result in variants:
        row = {"name": result["name"]}
        row.update({key: result["metrics"][key] for key in VARIANT_KEYS})
        summary.append(row)
    return {
        "symbol": provider.symbol,
        "settings": settings.__dict__,
        "summary": summary,
        "variants": variants,
    }


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
    lines.extend(
        [
            "",
            "## 解释原则",
            "",
            "- 所有智能体变体使用相同数据、起始资金、暖启动窗口和交易成本。",
            "- 信号在当前收盘后形成，只允许在下一根 K 线开盘成交。",
            "- `without_risk_governor` 是风险模块消融，不代表推荐交易方式。",
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
