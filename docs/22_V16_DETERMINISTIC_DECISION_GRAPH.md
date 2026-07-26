# v0.16.0 Deterministic Decision LangGraph

版本：v0.16.0  
日期：2026-07-24

## 1. 目标

v0.15.0 已经把研究结果整理成经过验证的 Paper 输入，但 Quant、Fusion/Critic、Regime 和 PortfolioRiskGovernor 仍集中在 `PortfolioPlanner.plan()` 的一个顺序函数中。

v0.16.0 将这段确定性决策拆成原生 LangGraph：

```text
<daily-run-id>:DECISION
```

目标不是让 LLM 接管风控，而是让确定性程序获得与研究 Agent 相同的：

- 节点级 checkpoint；
- 动态多标的 fan-out；
- 失败恢复；
- 输入与输出哈希；
- Mermaid 可视化；
- bounded thread inspection；
- Paper 决策记忆溯源。

## 2. 图结构

```text
Prepare Context
      │
      ▼
Dynamic Send over account symbols
 ┌────┼────┐
 ▼    ▼    ▼
SPY  QQQ  ... Symbol Decision Subgraph

每个 Symbol Subgraph：

Evidence Pack
  ├─ Quant Signal → Context/Fusion/Critic ─┐
  └─ Regime Guard ─────────────────────────┤
                                           ▼
                                     Overlay Target

全部 symbol fan-in：

PortfolioRiskGovernor
      ↓
Plan Finalize + SHA-256
      ↓
END
```

Quant/Fusion/Critic 与 Regime 在 Evidence 完成后形成两条分支；Target Overlay 只在两条分支都完成后执行。

## 3. PortfolioPlanner 阶段化

`PortfolioPlanner.plan()` 仍然保留，作为：

- 回测兼容路径；
- 顺序回退；
- 消融基线；
- LangGraph 结果对照。

内部拆分为：

```text
prepare_context
build_evidence_pack
quant_signal
fuse_and_criticize
assess_regime
apply_symbol_target
finalize_plan
```

LangGraph 与顺序 `plan()` 复用同一批纯阶段方法，因此不存在两套交易逻辑。

专项测试对 `PortfolioPlan` 做逐字段比较：

```text
proposed_targets
approved target_weights
confidences
symbol_decisions
risk_decision
strategy_state
```

结果完全一致。

## 4. Symbol 子图

### 4.1 Evidence Pack

每个 symbol 根据当前 close-time `decision_time` 构建点时 Evidence：

- 动量；
- 趋势；
- 波动率；
- 成交量；
- 可见新闻；
- 可见宏观与基本面证据。

未来 evidence 仍由 `EvidencePack.add()` 拒绝。

### 4.2 Quant Signal

`QuantSignalAgent` 只解释 Python 已计算的指标，不直接访问网络，也不修改仓位。

输出：

```text
action
score
confidence
rationale
evidence_ids
```

### 4.3 Context / Fusion / Critic

该分支依次执行：

```text
ContextAnalystAgent（有可见上下文时）
DecisionFusion
CriticAgent
TradeIntent
```

Critic 继续执行 Evidence ID 校验、波动率处罚和动量冲突降级。

### 4.4 Regime Guard

Regime 子分支独立执行：

```text
bull
bear
sideways
volatile
transition
```

状态使用 hysteresis，输出只限制 exposure multiplier，不制造新的 BUY。

### 4.5 Overlay Target

两条分支 fan-in 后应用研究 Overlay：

```text
Research SELL → protective zero
Research HOLD / low confidence → 不允许增加敞口
Research BUY + Quant BUY → 只做受限融合
Research BUY + Quant 非 BUY → 不能创建新仓位
```

## 5. Portfolio Hard Risk

全部 symbol 子图完成后，父图进入 `PortfolioRiskGovernor`：

- max position weight；
- max gross exposure；
- max positions；
- min confidence；
- drawdown circuit breaker；
- `ACTIVE → LIQUIDATING → HALTED` 风险状态；
- protective liquidation 的 force execution。

Hard Risk 使用输入 Portfolio 的快照副本。Graph 不修改调用方 Portfolio 对象，也不写账户数据库。

## 6. 输入指纹

每次运行计算：

```text
input_sha256
```

输入包括：

- market timestamp；
- decision time；
- universe；
- 行情文件路径与 SHA-256；
- 新闻文件路径与 SHA-256；
- 外部 Evidence 文件路径与 SHA-256；
- portfolio cash / positions / peak equity；
- current weights；
- strategy state；
- research overlays。

恢复已有 DECISION thread 时，checkpoint 中的输入哈希必须完全一致：

```text
expected input_sha256 == checkpoint input_sha256
```

不一致时 fail-closed，不允许把旧节点结果拼接到新行情、账户或 Overlay。

## 7. 计划哈希

PortfolioRiskGovernor 完成后，对不包含运行元数据的 `PortfolioPlan` 做 canonical JSON 序列化：

```text
sort_keys = true
compact separators
UTF-8
```

生成：

```text
plan_sha256
```

该哈希覆盖：

- proposed targets；
- approved targets；
- confidences；
- symbol decisions；
- risk decision；
- next strategy state。

## 8. 故障恢复

测试场景：QQQ 的 `fusion_critic` 首次失败。

第一次：

```text
SPY 子图完成
AAPL 子图完成
QQQ Evidence 完成
QQQ Quant 完成
QQQ Regime 完成
QQQ Fusion/Critic 失败
```

恢复同一 thread：

```text
SPY 不重跑
AAPL 不重跑
QQQ Evidence 不重跑
QQQ Quant 不重跑
QQQ Regime 不重跑
QQQ Fusion/Critic 重跑
QQQ Target 执行
PortfolioRiskGovernor 执行一次
Finalize 执行一次
```

因此一个确定性节点故障不会重新执行完整多标的计划。

## 9. Paper Service 边界

Decision Graph 位于：

```text
Close valuation / memory maturation
      ↓
DECISION graph
      ↓
record decision memories
      ↓
create paper orders
```

Graph 内明确不执行：

- account database mutation；
- decision-memory INSERT；
- order INSERT；
- approval；
- broker fill；
- external broker call。

Graph 失败时，新的决策记忆和新订单都不会创建。

Paper Session 输出保存：

```text
graph_type
thread_id
checkpoint_count
stream_event_count
execution_path
input_sha256
plan_sha256
checkpoint_database
account_mutation_in_graph=false
order_persistence_in_graph=false
external_broker=false
```

相同 provenance 也进入不可变决策记忆的 `rationale.decision_graph`。

## 10. Runtime 路径隔离

默认配置：

```yaml
workflow:
  decision_graph_enabled: true
  decision_checkpoint_database: artifacts/langgraph/decision_checkpoints.db
```

`PaperTradingService` 对相对 artifact 路径按当前 Paper Store 目录解析：

```text
production:
artifacts/paper_trading.db
artifacts/langgraph/decision_checkpoints.db

isolated test ledger:
/tmp/.../paper.db
/tmp/.../langgraph/decision_checkpoints.db
```

因此不同 Paper ledger 不会意外共享 DECISION thread。

## 11. 顺序回退

关闭图：

```yaml
workflow:
  decision_graph_enabled: false
```

Paper Service 会回到：

```text
PortfolioPlanner.plan()
```

回退路径使用相同阶段实现，结果应与图模式一致。

## 12. CLI

导出嵌套 Mermaid：

```bash
make decision-graph
```

输出：

```text
artifacts/decision_graph.mmd
```

查看 thread：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  research-thread-status '<paper-daily-run-id>:DECISION'
```

线程摘要只返回：

- input SHA-256；
- plan SHA-256；
- symbol count；
- approved target weights；
- risk state；
- force execution；
- execution path。

不会返回完整 Evidence、新闻、Prompt 或账户明细。

## 13. API

受 Token 保护：

```text
GET /workflow/decision-graph
GET /research/threads/{daily-run-id}:DECISION
```

图接口明确返回：

```text
account_mutation_in_graph = false
order_persistence_in_graph = false
external_broker = false
```

## 14. Doctor 与测试

Doctor 实际运行三标的无网络 Decision Graph：

```text
checkpoint_count：6
stream_event_count：21
symbol_count：3
input_sha256：64 位
plan_sha256：64 位
risk_state：ACTIVE
input_portfolio_mutated：false
external_request：false
```

专项测试覆盖：

1. LangGraph 与顺序 Planner 逐字段一致；
2. 输入 Portfolio 不被修改；
3. nested node pending-write 恢复；
4. 输入哈希变化拒绝恢复；
5. Mermaid 显示完整内部节点；
6. Paper run 与决策记忆保存 graph provenance。

## 15. 尚未迁入图

v0.16.0 有意保留在图外：

- decision-memory 数据库写入；
- order 创建与幂等键；
- approval state machine；
- account lock；
- next-open execution；
- fill 和现金持仓事务；
- external broker。

这些功能只有在 LangGraph checkpoint 与 Paper Store 事务能够形成明确的 outbox / idempotency contract 后，才适合进一步迁移。
