# v0.17.0 Market Calendar and Corporate Actions

版本：v0.17.0  
日期：2026-07-24

## 1. 为什么需要这一层

v0.16.0 已经把研究、组合监督、确定性决策与 Paper 执行边界拆分清楚，但市场数据仍存在两个基础风险：

1. 只按周一到周五生成交易日，会把圣诞节等休市日错误地当作可交易日；
2. 所有日期固定使用 16:00 收盘，无法表达提前收盘；
3. 拆股会制造巨大的虚假负动量；
4. 现金分红如果只给账户加现金、却不调整技术指标历史，除息缺口仍会被误判为价格下跌；
5. 多标的公共时间交集可能静默删除某个标的缺失或多出的日期；
6. Paper 重启后如果重复处理公司行动，持仓和现金会被二次修改。

v0.17.0 的目标是补齐市场时钟与公司行动语义，而不是增加新的 LLM Agent。

## 2. ExchangeTradingCalendar

文件：

```text
src/tradinglab_agents/engine/trading_calendar.py
```

项目使用 `exchange-calendars` 的 XNYS 规则，但第三方对象不会直接散布到各业务模块。内部适配层统一输出：

```text
ExchangeSession
calendar
session_date
open_at
close_at
is_early_close
duration_minutes
```

当前 Bar、订单和 Paper Schema 使用纽约交易所本地无时区时间，因此适配层把第三方 UTC aware 时间转换为：

```text
America/New_York naive datetime
```

这保留了已有 Schema，同时统一处理：

- 交易所节假日；
- 夏令时；
- 正常 09:30–16:00 会话；
- 13:00 提前收盘；
- 下一交易日和上一交易日查询。

### 示例

```text
2025-12-25：非交易日
2025-12-26：正常交易日
2025-11-28：09:30–13:00 提前收盘
```

## 3. 会话缓存

严格校验会被多个标的、Workflow、Paper 和测试反复调用。

v0.17.0 使用进程级 LRU 缓存：

```text
calendar instance cache
session schedule cache
```

缓存只保存交易所规则和日期对应的开收盘时间，不保存账户、价格或密钥。

缓存前，9 项 Paper 生命周期测试约需 48 秒；缓存后约需 6 秒，同时保持完全相同的严格校验。

## 4. 正式数据校验

`AlignedMarketData` 新增：

```text
calendar
strict_session_times
require_complete_alignment
corporate_actions
adjust_history_for_splits
adjust_history_for_dividends
```

### 4.1 每条 Bar 校验

启用正式日历后，每条源 Bar 都必须满足：

```text
日期是 XNYS session
open_at == 交易所开盘时间
close timestamp == 交易所收盘时间
available_at >= 收盘时间
open 和 close 位于同一 session date
```

因此以下数据会被拒绝：

```text
圣诞节日线
提前收盘日仍写 16:00
Bar 在收盘前就被标记为可用
跨日期日线
```

### 4.2 完整跨标的同步

旧逻辑使用所有标的时间戳的公共交集。若某一标的多出错误日期或缺少日期，交集可能静默丢弃问题数据。

正式入口默认：

```yaml
market:
  require_complete_alignment: true
```

只要任一标的日期集合不同，立即失败并返回不匹配日期样例。

## 5. 生成数据基线修复

旧的 2018–2025 多资产数据按工作日生成：

```text
2087 行
固定 16:00 收盘
包含错误休市日
```

v0.17.0 改为 XNYS 官方会话：

```text
2011 个同步 session
18 个提前收盘 session
圣诞节等休市日为 0 行
所有标的日期完全一致
```

以下生成器均已更新：

```text
scripts/generate_sample_data.py
scripts/generate_multi_asset_data.py
scripts/generate_scenario_suite.py
scripts/normalize_ohlcv_csv.py
```

Twelve Data 日线转换也通过同一市场日历映射开收盘时间。

## 6. CorporateAction Schema

文件：

```text
src/tradinglab_agents/data/corporate_actions.py
```

支持：

```text
SPLIT
CASH_DIVIDEND
```

标准字段：

```text
action_id
symbol
action_type
effective_at
available_at
split_ratio
cash_amount_per_share
currency
source
```

核心点时约束：

```text
available_at <= effective_at
effective_at == exchange session open
```

如果拆股或分红在其生效开盘后才“被系统知道”，数据会被拒绝，避免回测用到事后修订信息。

## 7. 三重价格与账户语义

v0.17.0 明确区分三种职责。

### 7.1 原始价格

用于：

```text
MarketSnapshot
账户估值
Paper Broker 成交
订单目标权重换算
现金替代参考价
```

模型和 Broker 永远不会用复权价成交。

### 7.2 总回报复权历史

用于：

```text
FeatureEngine
Quant Signal
Regime Guard
相关性和波动率
Decision Graph
```

### 7.3 公司行动账户事件

用于：

```text
拆股数量调整
非整数拆股现金替代
现金分红入账
Paper 审计与幂等恢复
```

## 8. 拆股复权

对于比例为 `r` 的拆股，在动作生效后查看历史时：

```text
历史 OHLC = 原始 OHLC / r
历史 Volume = 原始 Volume × r
生效日及之后价格不变
```

例如 4:1 拆股：

```text
拆股前 close 400 → 复权 close 100
拆股前 volume 1M → 复权 volume 4M
拆股日原始 open 约 100，保持不变
```

拆股生效前查询历史时不会提前复权。

## 9. 现金分红总回报复权

假设除息前最后收盘价为 `P`，每股现金分红为 `D`：

```text
历史价格调整因子 = (P - D) / P
```

动作生效后：

```text
除息日前历史 OHLC × 调整因子
Volume 不变
除息日及之后原始价格不变
```

同时 Paper 账户收到：

```text
cash_delta = 持仓数量 × 每股分红
```

因此：

- 账户总资产包含现金分红；
- 技术指标不会把除息缺口当成普通下跌；
- 成交仍使用原始除息后价格。

## 10. 拆股账户处理

公司行动在交易日开盘、已审批订单执行之前处理。

整数拆股：

```text
quantity_after = quantity_before × split_ratio
```

非整数或反向拆股：

```text
integer_quantity = floor(exact_quantity)
fractional_cash = fractional_shares × session_open_price
```

项目保持整数股 Paper 仓位，因此剩余碎股以开盘参考价转换为现金。

## 11. Paper Schema v3

新增表：

```text
paper_corporate_action_events
```

记录：

```text
event_id
account_id
run_id
action_id
symbol
action_type
effective_at
available_at
quantity_before
quantity_after
cash_delta
fractional_shares
reference_price
payload_json
```

幂等约束：

```text
UNIQUE(account_id, action_id)
```

公司行动处理、账户现金更新、持仓更新和事件 INSERT 在同一 SQLite 事务中完成。

若进程在之后的订单执行或决策阶段崩溃：

```text
公司行动已经落账 → 恢复时跳过
公司行动尚未落账 → 恢复时执行
```

不会重复拆股或重复派息。

## 12. Paper Session 顺序

```text
Begin Daily Run
      ↓
Load session corporate actions
      ↓
Atomic corporate-action posting
      ↓
Reload account
      ↓
Execute previously approved orders at raw open
      ↓
Close mark
      ↓
DECISION LangGraph
      ↓
Decision memory and next-open paper orders
```

目标权重订单不保存固定股数，因此拆股后不需要重写订单数量；只需在订单执行前更新账户持仓与现金。

## 13. Backtest 顺序

单标的和多标的回测均使用：

```text
Decision close
      ↓
Split/dividend-adjusted feature history
      ↓
Deterministic decision
      ↓
Next session raw open
      ↓
Apply corporate actions
      ↓
Simulated broker rebalance
```

结果增加：

```text
market_semantics
corporate_action_events
```

## 14. Decision Graph 输入指纹

`input_sha256` 新增覆盖：

```text
公司行动文件路径与 SHA-256
calendar name
strict_session_times
require_complete_alignment
adjust_history_for_splits
adjust_history_for_dividends
```

修改公司行动数据或复权模式后，旧 DECISION checkpoint 不能继续恢复。

## 15. 配置

```yaml
market:
  calendar: XNYS
  strict_sessions: true
  strict_session_times: true
  require_complete_alignment: true
  corporate_actions_enabled: true
  corporate_action_suffix: _actions.jsonl
  adjust_history_for_splits: true
  adjust_history_for_dividends: true
```

## 16. CLI

```bash
make market-calendar
make market-validate
```

或：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli market-calendar \
  --start 2025-01-01 --end 2025-12-31

PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli market-validate \
  --data-dir data/multi_sample \
  --symbols SPY QQQ AAPL MSFT NVDA

PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-actions \
  --account demo-paper
```

## 17. API

所有接口受本地 API Token 保护：

```text
GET /market/calendar
GET /market/semantics
GET /paper/accounts/{account_id}/corporate-actions
```

全部为本地读取，外部请求为零。

## 18. Doctor

Doctor 实际验证：

```text
exchange-calendars package version
2025 Christmas closed
2025 post-Thanksgiving close = 13:00
2011 synchronized fixture sessions
18 early closes
0 unmatched timestamps
5 corporate actions
AAPL split fixture
Paper schema version = 3
external request = false
```

## 19. 专项测试

覆盖：

1. 节假日与提前收盘；
2. 非交易日 Bar 拒绝；
3. 错误提前收盘时间拒绝；
4. 跨标的日期不一致拒绝；
5. 拆股点时可见性；
6. 拆股 OHLC/Volume 复权；
7. 分红总回报复权；
8. 公司行动文件进入 Decision 指纹；
9. 拆股持仓只调整一次；
10. 分红现金只入账一次；
11. legacy Paper DB 自动迁移到 schema v3。

## 20. 尚未完成

v0.17.0 不声称已经支持所有公司行动。目前尚未实现：

- 合并与换股；
- 分拆与 spin-off；
- 配股与认股权；
- 股票代码变更；
- 退市现金结算；
- ADR 比例调整；
- 税后分红与多币种预扣税；
- 真实公司行动供应商自动同步；
- 非 XNYS 标的自动选择交易所日历。

这些内容应在真实 Provider fallback 和资产元数据层完成后逐步加入。
