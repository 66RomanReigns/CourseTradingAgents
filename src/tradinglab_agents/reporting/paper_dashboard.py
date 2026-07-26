from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _equity_svg(history: Sequence[Mapping[str, Any]]) -> str:
    points = list(reversed(history))
    if len(points) < 2:
        return '<div class="empty">Not enough equity history for a chart.</div>'
    values = [float(point["equity"]) for point in points]
    width, height, pad = 900, 260, 28
    minimum, maximum = min(values), max(values)
    span = max(maximum - minimum, 1e-9)
    coordinates = []
    for index, value in enumerate(values):
        x = pad + index * (width - 2 * pad) / max(1, len(values) - 1)
        y = height - pad - (value - minimum) * (height - 2 * pad) / span
        coordinates.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Paper account equity curve">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="3" '
        f'points="{" ".join(coordinates)}"/>'
        f'<text x="{pad}" y="18">max {_money(maximum)}</text>'
        f'<text x="{pad}" y="{height - 6}">min {_money(minimum)}</text>'
        "</svg>"
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(value))}</td>" for value in row)
        + "</tr>"
        for row in rows
    )
    if not rows:
        body = f'<tr><td colspan="{len(headers)}" class="empty">No records.</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_paper_dashboard(summary: Mapping[str, Any]) -> str:
    account = summary["account"]
    orders = summary.get("open_orders", [])
    fills = summary.get("recent_fills", [])
    history = summary.get("equity_history", [])
    corporate_actions = summary.get("recent_corporate_actions", [])
    positions = account.get("positions", {})
    latest = history[0] if history else None
    equity = float(latest["equity"]) if latest else float(account["cash"])
    gross = float(latest["gross_exposure"]) if latest else 0.0

    position_rows = [
        (symbol, quantity)
        for symbol, quantity in sorted(positions.items())
        if int(quantity) != 0
    ]
    order_rows = [
        (
            row["order_id"],
            row["symbol"],
            row["side"],
            f"{float(row['target_weight']):.2%}",
            row["status"],
            row["scheduled_for"],
        )
        for row in orders
    ]
    fill_rows = [
        (
            row["fill_id"],
            row["symbol"],
            row["quantity"],
            _money(float(row["price"])),
            _money(float(row["fee"])),
            row["timestamp"],
        )
        for row in fills
    ]
    action_rows = [
        (
            row["action_id"],
            row["symbol"],
            row["action_type"],
            row["effective_at"],
            f"{row['quantity_before']} → {row['quantity_after']}",
            _money(float(row["cash_delta"])),
        )
        for row in corporate_actions
    ]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeLab Paper Account · {html.escape(account['account_id'])}</title>
<style>
:root {{ color-scheme: light dark; font-family: Inter, system-ui, sans-serif; }}
body {{ margin: 0; background: #111827; color: #e5e7eb; }}
main {{ max-width: 1180px; margin: auto; padding: 28px; }}
h1 {{ margin-bottom: 4px; }} .sub {{ color: #9ca3af; margin-top: 0; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 14px; }}
.card, section {{ background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 16px; }}
.card strong {{ display: block; font-size: 1.45rem; margin-top: 6px; }}
section {{ margin-top: 18px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #374151; white-space: nowrap; }}
th {{ color: #93c5fd; }} .empty {{ color: #9ca3af; text-align: center; }}
svg {{ width: 100%; height: auto; color: #60a5fa; }}
.badge {{ display: inline-block; padding: 3px 8px; border-radius: 999px; background: #374151; }}
.notice {{ border-left: 4px solid #f59e0b; padding-left: 12px; color: #fcd34d; }}
</style>
</head>
<body><main>
<h1>{html.escape(account['name'])}</h1>
<p class="sub">Account {html.escape(account['account_id'])} · Internal paper ledger only</p>
<p class="notice">No real broker is connected. Orders shown here exist only in the local SQLite simulation.</p>
<div class="cards">
<div class="card">Equity<strong>{_money(equity)}</strong></div>
<div class="card">Cash<strong>{_money(float(account['cash']))}</strong></div>
<div class="card">Gross exposure<strong>{gross:.2%}</strong></div>
<div class="card">Risk state<strong>{html.escape(account['risk_state'])}</strong></div>
<div class="card">Approval policy<strong>{html.escape(account['approval_policy'])}</strong></div>
<div class="card">Last session<strong>{html.escape(str(account.get('last_session') or '—'))}</strong></div>
</div>
<section><h2>Equity curve</h2>{_equity_svg(history)}</section>
<section><h2>Positions</h2>{_table(('Symbol', 'Quantity'), position_rows)}</section>
<section><h2>Open approval queue</h2>{_table(('Order', 'Symbol', 'Side', 'Target', 'Status', 'Scheduled open'), order_rows)}</section>
<section><h2>Recent fills</h2>{_table(('Fill', 'Symbol', 'Quantity', 'Price', 'Fee', 'Timestamp'), fill_rows)}</section>
<section><h2>Corporate actions</h2>{_table(('Action', 'Symbol', 'Type', 'Effective open', 'Quantity', 'Cash delta'), action_rows)}</section>
</main></body></html>"""
