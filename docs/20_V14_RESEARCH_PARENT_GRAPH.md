# v0.14.0 动态 Research Parent LangGraph

版本：v0.14.0  
日期：2026-07-24

## 1. 本阶段目标

v0.13.0 已经具备两类成熟子图：

```text
单标的 Research Graph
跨标的 Portfolio Supervisor Graph
```

但 `DailyWorkflow` 仍通过 Python 顺序代码启动候选研究，再启动组合图。v0.14.0 第一阶段新增顶层 Research Parent Graph，负责：

```text
Candidate Screening
→ 动态 Top-K fan-out
→ 等待所有候选研究完成
→ Portfolio Supervisor fan-in
→ 输出统一研究摘要
```

本阶段刻意不把数据刷新和 Paper Session 一次性迁入父图，避免同时改变数据安全、研究和执行三个边界。

## 2. 拓扑

```text
START
  │
  ▼
Candidate Screen
  │
  ├─ Send(symbol_research, SPY)  ─┐
  ├─ Send(symbol_research, QQQ)  ─┼─ dynamic Top-K
  └─ Send(symbol_research, ...)  ─┘
                                   │
                                   ▼
                         Portfolio Supervisor
                                   │
                                   ▼
                                  END
```

父图文件：

```text
src/tradinglab_agents/workflows/research_parent.py
```

## 3. 为什么使用 Send

候选集合不是固定图节点，而是由 Quant Screen 在运行时产生。LangGraph `Send` 允许路由函数按照状态动态创建多个同类任务：

```python
[
    Send("symbol_research", {"candidate": candidate})
    for candidate in candidates
]
```

候选数量从 Top-2 调整到 Top-3 或 Top-5 时，不需要重新构造静态节点名称。

父状态中的候选结果使用 reducer 合并：

```text
candidate_results: Annotated[list[dict], operator.add]
```

所有动态任务完成后，Portfolio Supervisor 只执行一次，并看到完整候选集合。

## 4. 三层 thread

v0.14.0 保留三层持久状态：

```text
<run-id>:RESEARCH_PARENT
<run-id>:SPY
<run-id>:QQQ
<run-id>:PORTFOLIO
```

职责分别为：

```text
RESEARCH_PARENT
候选列表、动态任务完成情况、组合 fan-in 状态

<symbol>
分析师、辩论、风险委员会、最终单标的计划

PORTFOLIO
相关性/集中度评审、组合建议、确定性 Guard
```

父图 checkpoint 默认数据库：

```text
artifacts/langgraph/research_parent_checkpoints.db
```

子图 checkpoint 默认数据库：

```text
artifacts/langgraph/research_checkpoints.db
```

分库的原因是第一阶段保持子图实现完全稳定，同时避免父图持有 SQLite 连接时与子图长时间共享同一连接生命周期。

## 5. 父级恢复

测试场景：

```text
SPY  成功
QQQ  失败
AAPL 成功
```

同一 `RESEARCH_PARENT` thread 恢复后：

```text
SPY  总执行 1 次
AAPL 总执行 1 次
QQQ 总执行 2 次
Portfolio Supervisor 总执行 1 次
```

成功分支通过 LangGraph pending writes 保留，父图只重新调度失败任务。

子图自身仍有更细粒度恢复能力。例如 QQQ 子图内部若只在 Macro Analyst 失败，恢复时不会重跑已经成功的 News 和 Fundamental Analyst。

因此恢复形成两级结构：

```text
父级：候选任务级恢复
子级：Agent 节点级恢复
```

## 6. 输入校验

父图在进入动态 fan-out 前执行：

- symbol 非空并转大写；
- priority 必须非负；
- 候选 symbol 不允许重复；
- 候选列表不能为空。

Portfolio fan-in 前再次验证：

```text
实际完成 symbol 集合 == Candidate Screen symbol 集合
```

缺少任何候选结果都会 fail-closed，不会用残缺组合结果继续执行。

## 7. 与子图的关系

本阶段的父图节点调用现有成熟运行时：

```text
symbol_research node
→ MultiAgentResearchPipeline
→ LangGraphResearchRuntime

portfolio_supervisor node
→ LangGraphPortfolioSupervisorRuntime
```

这是一种分阶段 sub-workflow 迁移，而不是重新实现内部节点。

保留的能力包括：

- 两轮 Bull/Bear；
- 三分析师并行；
- 三风险委员并行；
- Human interrupt；
- 成本感知 HOLD 路由；
- 组合 non-expansion guard；
- 点时决策记忆；
- 每角色 JSON 审计文件。

## 8. 配置

```yaml
workflow:
  research_parent_graph_enabled: true
  research_parent_checkpoint_database: artifacts/langgraph/research_parent_checkpoints.db
```

关闭父图：

```yaml
workflow:
  research_parent_graph_enabled: false
```

关闭后恢复 v0.13.0 的顺序编排，适用于：

- 消融实验；
- 紧急回退；
- 比较父图调度开销；
- 排查第三方 LangGraph 版本问题。

## 9. 调用预算

父图不增加模型调用：

```text
Top-2 单标的研究：2 × 13 = 26
Portfolio Supervisor：3
总计：29
```

父图只改变调度、持久化和恢复边界。

## 10. CLI

导出父图：

```bash
make research-parent-graph
```

输出：

```text
artifacts/research_parent_graph.mmd
```

查看父 thread：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  research-thread-status '<run-id>:RESEARCH_PARENT'
```

CLI 会根据 thread 后缀自动选择父图 checkpoint 数据库。

## 11. API

新增受 Token 保护的接口：

```text
GET /research/parent-graph
GET /research/threads/{run-id}:RESEARCH_PARENT
```

线程状态只返回：

```text
graph_type
checkpoint_count
candidate_count
completed_symbols
portfolio_status
execution_path
```

不返回 Prompt、Evidence、模型输出或密钥。

## 12. 审计层

父图新增 JSON 审计节点：

```text
candidate_screen
research.parent.symbol.<symbol>
research.parent.portfolio_supervisor
research_summary
```

LangGraph 负责可执行恢复，`WorkflowStateStore` 负责可读审计。两者继续并存。

## 13. 当前边界

v0.14.0 第一阶段尚未把以下内容放入同一个顶层图：

```text
网络前置检查
真实 Provider 刷新
本地数据验证
Paper Session
模拟订单审批与成交
```

这些模块仍由 `DailyWorkflow.execute()` 在父图外调度。

这样设计是有意的：真实数据刷新涉及密钥发送顺序和 fail-closed 策略，Paper Session 涉及账户锁和事务幂等性，不应为了图结构统一而一次性重写。

## 14. 后续阶段

### v0.14 第二阶段

```text
Data Validation
→ Research Parent Graph
→ Research Overlay Assembly
```

### v0.15

```text
Provider Refresh Subgraph
→ Network Gate
→ Provider fan-out
→ Data Quality fan-in
```

### v0.16

```text
Paper Planning Subgraph
→ Human approval interrupt
→ Next-open execution
```

每一阶段都应保留旧路径作为可测试回退，直到新路径通过完整模拟盘生命周期和崩溃恢复测试。

## 15. 验收结果

专项测试覆盖：

1. 三候选动态 `Send` fan-out；
2. Portfolio Supervisor 在完整 fan-in 后只执行一次；
3. 单候选失败恢复不重跑成功候选；
4. 完整 Dry-run 默认使用 `RESEARCH_PARENT`；
5. 父子 SQLite 数据库均成功创建；
6. 调用预算仍为 29；
7. 外部请求为 0；
8. Dry-run 模拟账户不修改；
9. 真实券商保持关闭。
