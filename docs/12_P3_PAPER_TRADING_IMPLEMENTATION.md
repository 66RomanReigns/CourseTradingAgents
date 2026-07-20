# P3 持久化模拟盘实施记录

版本：v0.7.0  
范围：内部模拟账户、订单队列、人工审批、下一开盘执行、交易日调度、重启恢复、本地管理 API 和 HTML 仪表板。

## 1. 安全边界

P3 仍然是内部模拟盘：

- 不连接真实券商；
- 不提交外部订单；
- 不涉及真实资金；
- 不需要 LLM 或金融数据 API Key；
- 所有账户、订单和成交仅存在于本地 SQLite；
- API、CLI 和仪表板均明确标记 `external_broker: false`。

## 2. SQLite 持久化模型

新增 `PaperTradingStore`，默认数据库：

```text
artifacts/paper_trading.db
```

数据库使用 WAL、外键和事务，包含：

```text
paper_accounts
paper_positions
paper_daily_runs
paper_orders
paper_order_events
paper_fills
paper_equity_snapshots
```

保存内容包括：

- 初始资金、现金、峰值净值和账户状态；
- 多标的持仓；
- Regime Guard 状态和组合风险状态；
- 日运行阶段和幂等键；
- 目标权重订单；
- 审批、拒绝、取消、过期和成交事件；
- 手续费、滑点后的模拟成交；
- 开盘和收盘净值快照。

## 3. 订单状态机

```text
PENDING_APPROVAL
    ├── APPROVED ── EXECUTED
    │       ├── CANCELLED
    │       ├── EXPIRED
    │       └── FAILED
    ├── REJECTED
    ├── CANCELLED
    └── EXPIRED
```

终态订单不能重新批准。所有状态变化写入 `paper_order_events`。

审批策略：

- `ALL`：所有订单必须人工审批，默认值；
- `RISK_AUTO`：普通订单人工审批，组合熔断清仓自动批准；
- `NONE`：所有内部模拟订单自动批准。

错过计划开盘仍未批准的订单自动转为 `EXPIRED`，不会在未来交易日执行陈旧意图。

## 4. 每日运行流程

`PaperTradingService.run_session()`：

```text
同步交易日开盘
  → 过期未审批订单
  → 执行已批准目标权重订单
  → 原子保存现金、持仓、订单状态和成交
  → 同步收盘估值
  → 运行多资产 PortfolioPlanner
  → 保存风险与 Regime 状态
  → 生成下一公共交易日开盘订单
  → 完成日运行记录
```

成交始终：

- 使用统一 `MarketSnapshot`；
- 先卖后买；
- 不允许负现金；
- 受单标的、总暴露和持仓数量约束；
- 使用下一同步交易日开盘价格；
- 不产生外部调用。

## 5. 幂等和崩溃恢复

日运行唯一键：

```text
(account_id, session_date)
```

同一天重复调用不会创建重复订单或成交。

运行阶段：

```text
STARTED → OPEN_EXECUTED → COMPLETE
```

如果进程在开盘成交提交后、收盘规划前异常：

1. 现金、持仓、订单和成交已经在一个事务中提交；
2. 失败记录保留 `failed_after_status=OPEN_EXECUTED`；
3. 重启后从收盘规划继续；
4. 不再次执行开盘订单；
5. 成交计数和成交明细不会丢失。

对应回归测试已模拟该故障。

## 6. 调度

`paper-next` 每次只处理一个未运行的同步交易日，并使用非阻塞文件锁：

```text
artifacts/paper_scheduler.lock
```

并发调用会返回调度器正忙，不会重叠处理同一个账户。

采用一次性入口而非内置常驻死循环，后续可以安全交给：

- cron；
- systemd timer；
- Docker 定时任务；
- CI 或外部编排器。

当前服务器尚未处理 Docker daemon 代理，因此未配置长期系统级调度。

## 7. CLI

初始化：

```bash
make paper-init
```

运行下一交易日收盘循环：

```bash
make paper-next
```

查看审批队列：

```bash
make paper-orders
```

批准全部待审批订单：

```bash
make paper-approve-all
```

批准后再次运行下一交易日，订单在开盘执行：

```bash
make paper-next
```

查看账户：

```bash
make paper-account
```

生成 HTML 仪表板：

```bash
make paper-dashboard
```

也可以使用单订单命令：

```text
paper-approve
paper-reject
paper-cancel
paper-run
```

## 8. 本地 API

新增：

```text
POST /paper/accounts
GET  /paper/accounts/{account_id}
GET  /paper/accounts/{account_id}/dashboard
GET  /paper/accounts/{account_id}/orders
POST /paper/accounts/{account_id}/sessions
POST /paper/orders/{order_id}/approve
POST /paper/orders/{order_id}/reject
POST /paper/orders/{order_id}/cancel
```

数据库和数据目录必须位于项目根目录内，防止路径逃逸。

## 9. HTML 仪表板

仪表板无需前端构建工具，展示：

- 当前权益和现金；
- 总暴露；
- 风险状态；
- 审批策略；
- 当前持仓；
- 待审批和已批准订单；
- 最近成交；
- 净值曲线；
- 明确的“未连接真实券商”提示。

## 10. 实际验收

CLI 完整流程：

```text
初始化账户
→ 首日运行生成 5 个待审批订单
→ 人工批量批准
→ 下一日开盘成交 5 笔
→ 重启服务
→ 账户现金和五标的持仓完整恢复
→ 生成 HTML 仪表板
```

样例结果：

```text
last_session: 2018-03-28
cash: 40,112.78
fills: 5
positions: AAPL、MSFT、NVDA、QQQ、SPY
external broker: disabled
```

这些数据来自离线合成行情，只验证软件闭环，不代表投资收益。

## 11. 测试结果

```text
55 项测试通过
Ruff 通过
compileall 通过
```

P3 新覆盖：

- 合法和非法订单状态迁移；
- 人工审批、拒绝和取消；
- 未审批订单过期；
- 自动审批策略；
- 下一开盘执行；
- SQLite 重启恢复；
- 同日运行幂等；
- 开盘成交后崩溃恢复；
- 调度器互斥锁；
- 本地 API 完整闭环；
- HTML 仪表板和安全提示。

## 12. 后续 P4

P4 仍包括：

- 修复 Docker daemon 无效代理；
- 容器化长期运行；
- 正式交易日历和节假日处理；
- 监控、日志轮转和健康告警；
- 可选邮件或 Telegram 通知；
- 更完整的前端交互；
- 真实数据和 DeepSeek 联调。

在明确批准前，项目不会增加真实资金交易能力。
