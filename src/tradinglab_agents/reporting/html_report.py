from __future__ import annotations

from html import escape


def _polyline(values: list[float], width: int = 920, height: int = 260, padding: int = 20) -> str:
    if not values:
        return ""
    low = min(values)
    high = max(values)
    span = high - low or 1.0
    usable_w = width - 2 * padding
    usable_h = height - 2 * padding
    points = []
    for index, value in enumerate(values):
        x = padding + usable_w * index / max(1, len(values) - 1)
        y = padding + usable_h * (high - value) / span
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _metric(value: float, kind: str = "number") -> str:
    if kind == "percent":
        return f"{value:.2%}"
    return f"{value:.3f}"


def render_experiment_html(result: dict, manifest: dict | None = None, audit: dict | None = None) -> str:
    rows = []
    for row in result.get("summary", []):
        rows.append(
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{_metric(row['total_return'], 'percent')}</td>"
            f"<td>{_metric(row['annualized_return'], 'percent')}</td>"
            f"<td>{_metric(row['sharpe'])}</td>"
            f"<td>{_metric(row['max_drawdown'], 'percent')}</td>"
            f"<td>{row['turnover']:.2f}</td>"
            f"<td>{row['trade_count']}</td>"
            f"<td>{row['fees']:.2f}</td>"
            "</tr>"
        )

    chart_blocks = []
    for variant in result.get("variants", []):
        curve = [float(point["equity"]) for point in variant.get("equity_curve", [])]
        if not curve:
            continue
        chart_blocks.append(
            f"<section class='chart'><h3>{escape(variant['name'])}</h3>"
            "<svg viewBox='0 0 920 260' role='img' aria-label='equity curve'>"
            "<rect x='0' y='0' width='920' height='260' class='plot-bg'/>"
            f"<polyline points='{_polyline(curve)}' class='equity-line'/>"
            "</svg>"
            f"<p>起始权益 {curve[0]:,.2f}，最终权益 {curve[-1]:,.2f}</p></section>"
        )

    regime_rows = []
    full = next((item for item in result.get("variants", []) if item.get("name") == "full_agent"), None)
    if full:
        for regime, metrics in full.get("regime_metrics", {}).items():
            regime_rows.append(
                "<tr>"
                f"<td>{escape(regime)}</td>"
                f"<td>{metrics['observations']}</td>"
                f"<td>{_metric(metrics['return'], 'percent')}</td>"
                f"<td>{_metric(metrics['max_drawdown'], 'percent')}</td>"
                f"<td>{_metric(metrics['positive_step_rate'], 'percent')}</td>"
                "</tr>"
            )

    manifest_html = "<p>未生成运行清单。</p>"
    if manifest:
        inputs = "".join(
            f"<li><code>{escape(item.get('relative_path') or item['path'])}</code> — "
            f"SHA-256 <code>{item['sha256'][:16]}…</code></li>"
            for item in manifest.get("inputs", [])
        )
        manifest_html = (
            f"<p>Run ID：<code>{escape(manifest['run_id'])}</code></p>"
            f"<p>Git：<code>{escape(str(manifest.get('git', {}).get('revision')))}</code>，"
            f"dirty={manifest.get('git', {}).get('dirty')}</p><ul>{inputs}</ul>"
        )

    audit_html = "<p>未执行审计。</p>"
    if audit:
        audit_html = (
            f"<p class={'pass' if audit.get('passed') else 'fail'}>"
            f"审计结果：{'PASS' if audit.get('passed') else 'FAIL'}；"
            f"最低审计分数 {audit.get('minimum_audit_score', 0):.3f}；"
            f"最低证据覆盖率 {audit.get('minimum_citation_coverage', 0):.2%}</p>"
        )

    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>TradeLab-Agent 实验报告</title>
<style>
:root {{ font-family: Inter, system-ui, sans-serif; color: #172033; background: #f5f7fb; }}
body {{ margin: 0; }} main {{ max-width: 1080px; margin: auto; padding: 32px 20px 60px; }}
h1, h2, h3 {{ color: #102046; }} .card, section {{ background: white; border-radius: 14px; padding: 20px; margin: 18px 0; box-shadow: 0 5px 18px rgba(28,48,90,.08); }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 10px 8px; border-bottom: 1px solid #e7ebf3; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }} code {{ background: #edf1f8; padding: 2px 5px; border-radius: 5px; }}
svg {{ width: 100%; height: auto; }} .plot-bg {{ fill: #f8faff; }} .equity-line {{ fill: none; stroke: #315ddd; stroke-width: 2.5; }}
.pass {{ color: #117840; font-weight: 700; }} .fail {{ color: #b42318; font-weight: 700; }}
.note {{ color: #526077; }} @media(max-width:720px) {{ table {{ font-size: 12px; }} th,td {{ padding: 7px 4px; }} }}
</style>
</head>
<body><main>
<h1>TradeLab-Agent 实验报告</h1>
<p class='note'>标的：<strong>{escape(result.get('symbol', 'UNKNOWN'))}</strong>。本报告由离线实验自动生成，不构成投资建议。</p>
<section><h2>方案对比</h2><table><thead><tr><th>方案</th><th>总收益</th><th>年化收益</th><th>Sharpe</th><th>最大回撤</th><th>换手率</th><th>成交数</th><th>手续费</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
<section><h2>完整智能体分市场状态表现</h2><table><thead><tr><th>状态</th><th>样本数</th><th>区间收益</th><th>最大回撤</th><th>正收益步比例</th></tr></thead><tbody>{''.join(regime_rows)}</tbody></table></section>
<section><h2>审计结果</h2>{audit_html}</section>
<section><h2>运行可追溯性</h2>{manifest_html}</section>
<h2>权益曲线</h2>{''.join(chart_blocks)}
<section><h2>解释边界</h2><ul><li>当前示例数据为确定性合成数据，仅验证系统工程与实验流程。</li><li>所有信号在收盘后形成，最早于下一交易日开盘成交。</li><li>风险收益结果应结合真实历史数据与跨阶段实验重新评估。</li></ul></section>
</main></body></html>"""
