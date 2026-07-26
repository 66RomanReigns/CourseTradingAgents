# v0.15.0 Workflow Core LangGraph

版本：v0.15.0  
日期：2026-07-24

## 1. 目标

v0.14.0 已经把候选筛选、动态 Top-K 单标的研究与 Portfolio Supervisor 统一到 Research Parent LangGraph，但 `DailyWorkflow.execute()` 仍然自行完成数据验证、Overlay 构造和 Paper Service 输入准备。

v0.15.0 增加一个更高层的持久核心图：

```text
<run-id>:WORKFLOW_CORE
```

它负责：

```text
Data Validation
      ↓
Research Parent
      ↓
Overlay Assembly
      ↓
Decision Preparation
```

真实 Provider 刷新和 Paper Session 仍然保留在图外。这是分阶段迁移边界，不是遗漏。

## 2. 顶层结构

```text
Provider Refresh（仅 live，图外）
      │
      ▼
WORKFLOW_CORE
  Data Validation
      │
      ▼
  RESEARCH_PARENT
      ├─ <run-id>:SPY
      ├─ <run-id>:QQQ
      └─ <run-id>:PORTFOLIO
      │
      ▼
  Overlay Assembly
      │
      ▼
  Decision Preparation
      │
      ▼
Paper Session（图外）
```

线程与数据库：

```text
<run-id>:WORKFLOW_CORE
artifacts/langgraph/workflow_core_checkpoints.db

<run-id>:RESEARCH_PARENT
artifacts/langgraph/research_parent_checkpoints.db

<run-id>:<symbol>
<run-id>:PORTFOLIO
artifacts/langgraph/research_checkpoints.db
```

## 3. Data Validation

核心图无论 dry-run、offline 还是 live，都在研究前验证本地市场文件。

最低约束：

- 验证状态必须为 `completed` 或显式 `not_required`；
- 研究、Paper 或行情刷新会消费数据时，至少存在一个市场文件；
- 仅执行网络 preflight 且研究、Paper、行情刷新均关闭时，可以返回 `not_required`；
- 缺少配置中任一标的行情文件时 fail-closed；
- live 模式也必须在 Provider 刷新后重新验证本地落盘结果。

Provider 访问凭据、网络探针和远程刷新仍由图外的现有安全层负责。

## 4. Research Parent

该节点调用 v0.14.0 已验证的 Research Parent：

```text
Candidate Screen
      ↓
Send Top-K Symbol Research
      ↓
Portfolio Supervisor Fan-in
```

核心图不会重写单标的或组合图内部逻辑，而是把完整 Research Parent 结果作为一个可恢复阶段保存。

如果研究被禁用：

```text
Data Validation
      ↓
Research Disabled
      ↓
Empty Overlay Assembly
      ↓
Safe no_research preparation
```

因此关闭 LLM 研究不会跳过数据验证或安全交接。

## 5. Overlay Assembly

Overlay Assembly 把每个候选的最终研究结果转换成 Paper Service 已有的研究覆盖层。

集合约束：

```text
Research Candidate Symbols
==
Final Overlay Symbols
```

多一个、少一个或 symbol 不一致都会 fail-closed。

每个 Overlay 继续保存：

- 最终 action；
- confidence；
- target weight；
- order type；
- human approval 标志；
- 分析师、辩论、风险委员会和 Portfolio Supervisor trace；
- deterministic non-expansion 结果。

## 6. Decision Preparation

这是 v0.15.0 新增的确定性 Paper 输入契约。

### 6.1 Action 与 Order 校验

```text
HOLD → NO_ORDER
BUY  → MARKET_NEXT_OPEN
SELL → MARKET_NEXT_OPEN 且 target_weight = 0
```

### 6.2 仓位校验

```text
target_weight >= 0
target_weight <= max_position_weight
```

Portfolio Supervisor 已经不能扩大单标的目标；Decision Preparation 会再次检查 trace 中的 `non_expansion_verified`。

### 6.3 审批校验

所有 Overlay 必须保留：

```text
requires_human_approval = true
```

这不等于自动审批，只表示 Paper Service 接收到的研究输入不能绕过审批边界。

### 6.4 Paper-only 校验

核心图输出必须满足：

```text
safe_for_paper_input = true
external_broker = false
```

任何节点尝试将 `external_broker` 设为 true，核心图都会失败。

## 7. Canonical Handoff Hash

所有最终 Overlay 按 symbol 排序并进行 canonical JSON 序列化：

```text
sort_keys = true
separators = compact
UTF-8
```

随后生成：

```text
overlay_payload_sha256
```

该哈希用于：

- 证明传入 Paper Service 的 Overlay 内容；
- 比较恢复前后输入是否一致；
- 后续关联订单、决策记忆和收益归因；
- 在报告中展示研究到执行的可复现交接点。

哈希不包含 API Key，也不替代数据库事务或审批记录。

## 8. 故障恢复

测试场景：

```text
Data Validation   成功
Research Parent   成功
Overlay Assembly  第一次失败
```

同一 `WORKFLOW_CORE` thread 恢复后：

```text
Data Validation   总执行 1 次
Research Parent   总执行 1 次
Overlay Assembly  总执行 2 次
Decision Prep     总执行 1 次
```

因此 Overlay 代码故障不会重新消耗完整 29 次研究预算。

非恢复模式使用同一 thread ID 时会先删除旧 checkpoint，防止 reducer 状态污染新运行。

多个 symbol 子图会共享同一个 SQLite checkpoint 数据库。v0.15.0 使用统一 saver：按数据库路径锁定 schema/WAL 初始化，配置 30 秒 `busy_timeout`，初始化完成后继续允许各子图并行执行。12 线程并发初始化回归测试已通过。

## 9. 配置

```yaml
workflow:
  workflow_core_graph_enabled: true
  workflow_core_checkpoint_database: artifacts/langgraph/workflow_core_checkpoints.db
```

关闭后保留 v0.14 路径：

```yaml
workflow:
  workflow_core_graph_enabled: false
```

即便关闭核心图，DailyWorkflow 仍会执行本地数据验证和 deterministic Decision Preparation，只是不使用 WORKFLOW_CORE checkpoint。

## 10. CLI

导出图：

```bash
make workflow-core-graph
```

输出：

```text
artifacts/workflow_core_graph.mmd
```

查看线程：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  research-thread-status '<workflow-run-id>:WORKFLOW_CORE'
```

只返回受限状态摘要：

- data validation status；
- research status；
- overlay count；
- decision preparation status；
- safe-for-paper 标志；
- overlay SHA-256；
- execution path。

不会返回完整 Prompt、Evidence 或研究输出。

## 11. API

受 Token 保护：

```text
GET /workflow/core-graph
GET /research/threads/{run-id}:WORKFLOW_CORE
GET /workflow/plan
POST /workflow/dry-run
```

图接口明确返回：

```text
provider_refresh_in_graph = false
paper_execution_in_graph = false
external_broker = false
```

## 12. 完整 Dry-run 验收

标准 Top-2：

```text
WORKFLOW_CORE checkpoint：6
RESEARCH_PARENT checkpoint：5
每个单标的 Research Graph：约 14
PORTFOLIO checkpoint：5
模型调用预算：29
Overlay 数量：2
Overlay SHA-256：64 位十六进制
safe_for_paper_input：true
外部请求：0
模拟账户修改：false
真实券商：false
```

## 13. 尚未迁入核心图

v0.15.0 有意保留：

- 网络 preflight；
- Twelve Data、Alpha Vantage、FRED、SEC 刷新；
- Provider fallback；
- Paper account lock；
- 下一开盘执行；
- 订单审批与过期处理；
- 真实券商连接。

下一阶段更合理的方向是先把确定性 Quant/Fusion/Regime/PortfolioRiskGovernor 的“决策准备”纳入核心图，再评估 Provider 和 Paper 子图，而不是直接把所有副作用塞入一个巨大状态图。
