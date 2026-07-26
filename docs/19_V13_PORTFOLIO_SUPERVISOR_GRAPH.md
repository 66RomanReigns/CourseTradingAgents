# v0.13.0 跨标的 Portfolio Supervisor LangGraph

版本：v0.13.0  
日期：2026-07-24

## 1. 为什么需要第二个图

v0.12.0 已经把每个候选标的的研究过程迁移到原生 LangGraph，但各候选仍然独立得出结论。例如 SPY 与 QQQ 都可能得到合理的 BUY，同时它们又可能高度相关。单标的 Agent 无法判断：

- 两个建议是否集中在同一风险来源；
- 候选之间的相关性是否过高；
- 多个独立 20% 建议组合后是否过度集中；
- 某个标的是否应当因为组合关系而进一步降权。

v0.13.0 增加一个独立、跨标的 Portfolio Supervisor 图。它不是替代原有组合风险治理，而是在单标的语言研究与确定性 `PortfolioPlanner` 之间增加一层“只减不增”的组合研究。

## 2. 总体执行顺序

```text
全市场 Quant 筛选
        │
        ▼
Top-2 候选
  ├─ <run-id>:SPY ── 单标的 Research LangGraph
  └─ <run-id>:QQQ ── 单标的 Research LangGraph
        │
        ▼
同步点时跨标的统计
        │
        ▼
<run-id>:PORTFOLIO
  ├─ Correlation Reviewer ───┐
  └─ Concentration Reviewer ─┤  并行
                             ▼
                    Portfolio Supervisor
                             ▼
              Deterministic Non-expansion Guard
                             ▼
             受监督的 Research Overlay
                             ▼
Quant / Fusion / Critic / Regime
                             ▼
PortfolioRiskGovernor
                             ▼
Paper approval / next-open execution
```

语言模型不处于最终风险边界。执行顺序明确保证：

```text
LLM portfolio suggestion
        ↓
deterministic portfolio guard
        ↓
existing deterministic planner
        ↓
PortfolioRiskGovernor
        ↓
paper approval
```

## 3. LangGraph 拓扑

文件：

```text
src/tradinglab_agents/agents/portfolio_supervisor.py
```

拓扑：

```text
START
 ├─ correlation_reviewer ──┐
 └─ concentration_reviewer ┤
                           ▼
                 portfolio_supervisor
                           ▼
                  deterministic_guard
                           ▼
                          END
```

两个 Reviewer 处于同一个 LangGraph super-step。`portfolio_supervisor` 通过多起点 fan-in 等待两个结果，再进入确定性 Guard。

默认模型层级：

```text
Correlation Reviewer    quick model
Concentration Reviewer  quick model
Portfolio Supervisor    deep model
Deterministic Guard     no LLM
```

## 4. 点时跨标的市场上下文

函数：

```text
build_portfolio_market_context(...)
```

输入只来自已经缓存的同步日线收盘价，不读取未来数据。默认窗口为 60 个同步交易时段。

输出包括：

```text
as_of
window_sessions
annualized_volatility
trailing_return
pairwise correlations
high_correlation_pairs
point_in_time=true
```

相关系数使用标准 Pearson 相关，不新增 NumPy 依赖。零方差或历史不足时采用保守的零相关结果或直接失败。

默认配置：

```yaml
workflow:
  portfolio_supervisor_enabled: true
  portfolio_correlation_window: 60
  portfolio_high_correlation_threshold: 0.80
  portfolio_cluster_gross_cap: 0.25
```

## 5. 结构化 Schema

新增严格 Pydantic 类型：

```text
PortfolioSymbolCap
PortfolioCommitteeReview
PortfolioAllocationItem
PortfolioSupervisorDecision
GuardedPortfolioAllocation
```

所有类型均：

- `extra="forbid"`；
- frozen；
- 限定 action；
- 限定目标权重；
- 校验 symbol 唯一性；
- 校验 `gross_target` 等于 allocation 总和。

Reviewer 必须为每个候选返回一个 cap，不能省略或加入未知标的。

## 6. 三个 LangChain 角色

### 6.1 Correlation Reviewer

输入：

- 各候选最终单标的 TradePlan；
- 60 日相关性；
- 年化波动率；
- trailing return；
- 高相关标的对；
- 组合总暴露和相关簇限制。

权限：

```text
允许：维持、降低、否决
禁止：提高标的目标、创建新仓位、削弱 SELL
```

### 6.2 Concentration Reviewer

关注：

- 单标的目标是否集中；
- 多个候选的总目标是否过高；
- 候选数量与权重分布；
- 单一研究结论是否主导组合。

输出每个 symbol 的最大目标和组合最大 gross target。

### 6.3 Portfolio Supervisor

综合两个 Reviewer，但仍然只生成“建议”。它必须返回所有候选，并且不能：

- HOLD → BUY；
- SELL → HOLD/BUY；
- 超过单标的原始目标；
- 超过 Reviewer cap；
- 超过组合 gross cap。

即使模型违反这些约束，后续确定性 Guard 仍会修正。

## 7. Deterministic Non-expansion Guard

函数：

```text
guard_portfolio_allocation(...)
```

这是 v0.13.0 的核心安全边界。

### 7.1 单标的非扩张

对每个 symbol：

```text
最终目标 <= 单标的最终 TradePlan 目标
最终目标 <= Correlation Reviewer cap
最终目标 <= Concentration Reviewer cap
```

### 7.2 Action 单向约束

```text
原 SELL  → 最终必须 SELL 0%
原 HOLD  → 最终只能 HOLD，不能 BUY
原 BUY   → 最终可以 BUY、降低或变为 HOLD
```

Portfolio Supervisor 不能基于相关性把 BUY 改成新的保护性 SELL，因为它不知道真实当前仓位。减到零时使用 HOLD，真正的减仓与清仓仍由单标的 SELL、Quant 和硬风控决定。

### 7.3 高相关簇约束

当两个或多个候选的相关系数达到阈值时，它们形成一个连通分量。若簇内目标总和超过配置上限，则按比例缩放：

```text
cluster_scale = cluster_gross_cap / cluster_gross
```

默认：

```text
correlation >= 0.80
cluster gross <= 25%
```

例如测试数据：

```text
SPY / QQQ 60 日相关性 ≈ 0.915
原目标：SPY 18%，QQQ 17%
Guard 后：SPY 12.5%，QQQ 12.5%
簇总和：25%
```

### 7.4 持仓数量与总暴露

Guard 还会执行：

```text
max_positions
max_gross_exposure
```

但这仍不是最终硬风控。`PortfolioRiskGovernor` 会在真实账户权重、回撤状态和所有持仓价格基础上再次约束。

## 8. 调用预算

单标的研究：

```text
13 calls / candidate
Top-2 = 26 calls
```

组合监督：

```text
Correlation Reviewer    1
Concentration Reviewer  1
Portfolio Supervisor    1
```

标准总计：

```text
26 + 3 = 29 calls
```

默认配置已更新：

```yaml
llm:
  max_calls_per_run: 29
```

`workflow-plan` 会分别显示：

```text
symbol_research_calls
portfolio_supervisor.calls
remote_llm.planned_calls
```

## 9. 持久化与恢复

组合图使用独立 thread：

```text
<workflow-run-id>:PORTFOLIO
```

与单标的图共享配置的 SQLite checkpoint 文件，但 thread 完全隔离。

JSON 审计节点包括：

```text
research.portfolio.market_context
research.portfolio.correlation_reviewer
research.portfolio.concentration_reviewer
research.portfolio.supervisor
research.portfolio.deterministic_guard
research_summary
```

恢复测试：

```text
Correlation Reviewer    第一次失败
Concentration Reviewer  成功
```

同一 thread 恢复后：

```text
Correlation Reviewer    总执行 2 次
Concentration Reviewer  总执行 1 次
```

说明 LangGraph pending writes 正常保存成功的并行兄弟分支。

## 10. Research Overlay 集成

单标的原始结果保持不可变：

```text
candidate.result.trader
```

组合调整另存：

```text
candidate.portfolio_adjustment
```

进入 `PortfolioPlanner` 前生成最终 overlay：

```text
portfolio_supervised_research_graph_v2
```

Overlay 再次检查：

- target 未增加；
- SELL 未削弱；
- human approval 保留；
- Evidence IDs 仍使用单标的原始证据；
- 组合调整轨迹写入 trace。

随后原有 `PortfolioPlanner` 仍要求 Quant BUY 才能创建仓位。因此即使组合 Supervisor 建议 BUY，它也不能独立制造仓位。

## 11. CLI

导出组合图：

```bash
make portfolio-graph
```

或者：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  portfolio-graph \
  --config config/default.yaml \
  --output artifacts/portfolio_supervisor_graph.mmd
```

查看组合 thread：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  research-thread-status '<workflow-run-id>:PORTFOLIO'
```

线程摘要会返回：

```text
graph_type=portfolio_supervisor
portfolio_gross_target
portfolio_allocations
checkpoint_count
execution_path
```

## 12. API

受 API Token 保护：

```text
GET /research/portfolio-graph
GET /research/threads/{run-id}:PORTFOLIO
```

图接口只返回 Mermaid 和安全元数据，不执行模型。

## 13. Doctor

Doctor 新增实际无网络探针：

```text
portfolio_supervisor_runtime
```

检查：

- 多资产点时上下文；
- 两个并行 Reviewer；
- Portfolio Supervisor；
- deterministic guard；
- 25% 高相关簇限制；
- SQLite checkpoint；
- Mermaid；
- external_request=false。

## 14. 测试

新增五项专项测试：

1. 模型尝试提高目标时被 Guard 限制；
2. 高相关簇按 25% 总目标缩放；
3. protective SELL 被恢复，HOLD 不可升级；
4. 并行 Reviewer 故障恢复只重跑失败节点；
5. 完整 DailyWorkflow 使用 29 次预算并持久化组合调整。

## 15. 尚未完成

Portfolio Supervisor 当前使用价格相关性和目标集中度，还没有加入：

- 行业分类与行业暴露；
- 风格因子暴露；
- Beta、VaR、CVaR；
- 宏观情景相关性变化；
- 相关性稳定性与置信区间；
- 真实账户税务与流动性约束；
- 组合级收益归因学习；
- 真实券商。

这些属于后续成熟化工作。当前实现优先保证：组合语言推理只能降风险，不能扩大风险。
