# v0.12.0 LangChain / LangGraph 主运行时

版本：v0.12.0  
日期：2026-07-20

> 后续状态：v0.13.0 已实现本文件列为未完成的跨标的 Portfolio Supervisor LangGraph，详见 `docs/19_V13_PORTFOLIO_SUPERVISOR_GRAPH.md`。本文件其余内容保留为 v0.12.0 历史设计记录。

## 1. 目标与边界

v0.12.0 的目标不是把现有代码机械改名为 LangGraph，也不是复制 TauricResearch/TradingAgents 的节点拓扑，而是让 LangChain 与 LangGraph 真正承担它们适合的运行职责：

- LangChain：Prompt 组合、Runnable 角色调用、统一元数据和安全本地追踪；
- LangGraph：共享状态、并行 super-step、循环辩论、条件路由、线程持久化、失败恢复和可选人工中断；
- TradeLab-Agent 自身：点时 Evidence、Pydantic 业务 Schema、不可变审计产物、确定性组合硬风控、持久模拟盘和下一开盘成交。

项目仍然不连接真实券商，研究图的任何输出都只是模拟订单意图。

## 2. 必需依赖

`pyproject.toml` 已将以下包提升为主依赖：

```text
langchain>=1.3,<2
langgraph>=1.2,<2
langgraph-checkpoint-sqlite>=3.1,<4
```

当前锁定并验证的版本：

```text
langchain                    1.3.13
langchain-core               1.4.9
langgraph                    1.2.9
langgraph-checkpoint         4.1.1
langgraph-checkpoint-sqlite  3.1.0
```

服务器无法安全访问公网 PyPI 时，项目使用南京大学 HTTPS 镜像：

```bash
make lock
make sync
```

可以通过环境变量覆盖：

```bash
make sync PYPI_INDEX=https://pypi.org/simple
```

项目不会通过关闭 TLS 校验安装依赖。

## 3. LangChain 层

文件：

```text
src/tradinglab_agents/agents/langchain_runtime.py
```

每个角色调用由以下 Runnable 组合执行：

```text
PromptTemplate
      │
      ▼
RunnableLambda(render)
      │
      ▼
RunnableLambda(invoke provider-neutral structured client)
      │
      ▼
Pydantic validated result
```

这层没有抛弃原有 `StructuredLLMClient`。原因是行情供应商、模型供应商、缓存和供应商调用账本已经基于该稳定接口实现。LangChain 负责组合与生命周期，底层客户端继续负责真实 SDK 调用和严格 Schema 校验。

### 安全追踪

默认写入：

```text
artifacts/langchain_events.jsonl
```

只记录：

```text
role_start / role_end / role_error
任务名
quick / deep tier
symbol
LangGraph thread_id
耗时
错误类型与截断错误
```

明确不记录：

```text
API Key
Authorization
完整 Prompt
输入 payload
完整模型输出
```

并行角色使用进程内锁追加日志，防止多线程写入互相覆盖。

## 4. LangGraph 状态图

文件：

```text
src/tradinglab_agents/agents/langgraph_research.py
```

默认拓扑：

```text
START
 ├─ News Analyst ─────────┐
 ├─ Macro Analyst ────────┼─ fan-in
 └─ Fundamental Analyst ──┘
                │
                ▼
         Debate Dispatch
          ├─ Bull ─┐
          └─ Bear ─┘
                │ fan-in
                ▼
          Debate Join
                │
       round < configured?
          ├─ yes → Debate Dispatch
          └─ no  → Research Manager
                         │
              cost-aware condition?
                 ├─ low-confidence HOLD → deterministic HOLD
                 └─ full committee → Preliminary Trader
                                      ├─ Aggressive Risk ─┐
                                      ├─ Balanced Risk ───┼─ fan-in
                                      └─ Conservative Risk┘
                                                    │
                                                    ▼
                                           Portfolio Manager
                                                    │
                                                    ▼
                                            Human Review Gate
                                                    │
                                                    ▼
                                                   END
```

### 原生并行

三分析师和三风险委员不是顺序 for-loop。它们由 `START` fan-out 和多起点 `add_edge([...], target)` 在同一 super-step 中并行执行。

### 原生循环

Bull/Bear 辩论使用 `add_conditional_edges`：

```text
Debate Join → continue → Debate Dispatch
Debate Join → manager  → Research Manager
```

默认两轮，支持一至三轮。轮数进入完整配置哈希和调用预算。

### 条件成本路由

可配置：

```yaml
workflow:
  langgraph_cost_aware_routing: false
  langgraph_hold_skip_confidence: 0.40
```

启用后，Research Manager 输出低置信度 HOLD 时，可以跳过 Preliminary Trader、三风险委员和 Portfolio Manager 的模型调用，直接生成确定性 HOLD。

该能力默认关闭，保证 v0.11 的研究行为不因升级自动变化。

## 5. 原生流式事件

主执行使用：

```text
graph.stream(..., stream_mode="updates")
```

默认事件文件：

```text
artifacts/langgraph_events.jsonl
```

事件只包含：

```text
graph_start
graph_step + node names
graph_interrupted
graph_complete
graph_error
thread_id
checkpoint_count
```

不写入 channel state、TradePlan、Evidence、Prompt 或模型输出。LangChain 角色追踪与 LangGraph 图事件分别落盘，便于区分“某个模型角色耗时”和“整个图走到了哪个节点”。

## 6. 双层 checkpoint

### LangGraph 可执行 checkpoint

默认：

```text
artifacts/langgraph/research_checkpoints.db
```

每个候选使用独立 thread：

```text
<workflow-run-id>:<symbol>
```

LangGraph SQLite 保存：

- 各 super-step 状态；
- channel values；
- pending writes；
- 中断状态；
- thread history。

设置：

```text
LANGGRAPH_STRICT_MSGPACK=true
```

### 项目 JSON 审计 checkpoint

仍然保留：

```text
artifacts/workflows/<run_id>/state.json
artifacts/workflows/<run_id>/nodes/*.json
```

JSON 层用于人类审计、结果哈希、答辩展示和不可变角色产物；LangGraph 层用于实际恢复。二者不互相替代。

`WorkflowStateStore` 已升级到 schema v2：

```text
active_node   兼容旧接口
active_nodes  支持并行 super-step
```

所有状态合并和原子文件替换都受线程锁保护。

## 7. 故障恢复

测试场景：News、Macro、Fundamental 并行，Macro 第一次抛出异常。

第一次执行：

```text
News         成功并保存 pending write
Fundamental  成功并保存 pending write
Macro        失败
```

同一 thread 恢复后：

```text
News         不重跑，总调用 1 次
Fundamental  不重跑，总调用 1 次
Macro        重跑，总调用 2 次
后续图       继续完成
```

LangGraph `RetryPolicy` 只重试连接、超时、限流和服务端类错误。业务 Schema 错误、Evidence 引用错误和安全约束错误不会被盲目重试。

同步 Python 节点不能安全使用 LangGraph 可取消 node timeout，因此：

- 图负责 RetryPolicy；
- OpenAI-compatible、HTTP 数据客户端继续负责真实网络超时；
- 配置中的 timeout 作为 provider timeout 边界，而不是强杀同步线程。

## 8. 人工中断

默认：

```yaml
workflow:
  langgraph_human_review_mode: paper_queue
```

默认行为仍然是将方向性模拟订单交给现有持久审批队列。

研究层实验模式：

```yaml
workflow:
  langgraph_human_review_mode: interrupt_directional
```

方向性 BUY/SELL 会调用 LangGraph `interrupt()`，保存 thread 后返回：

```json
{
  "type": "TRADING_RESEARCH_REVIEW",
  "symbol": "X",
  "proposed_plan": {},
  "allowed_decisions": ["approve", "reject", "reduce"],
  "external_broker": false
}
```

恢复：

```text
Command(resume={"decision": "approve"})
Command(resume={"decision": "reject"})
Command(resume={"decision": "reduce", "target_weight": 0.08})
```

约束：

- reduce 只能用于 BUY；
- 新仓位不能超过原计划；
- reject 转为 HOLD；
- 仍不连接真实券商；
- 研究中断不能替代组合硬风控和模拟盘审批。

## 9. CLI

导出图：

```bash
make research-graph
```

查看线程：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli \
  research-thread-status '<thread-id>'
```

研究层中断：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml \
  --human-review-mode interrupt_directional
```

恢复：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml \
  --thread-id '<thread-id>' \
  --resume-decision reject
```

## 10. API

所有接口仍受 `TRADINGLAB_API_TOKEN` 保护：

```text
GET  /research/graph
GET  /research/threads/{thread_id}
POST /research
```

`POST /research` 支持：

```text
human_review_mode
thread_id
resume_decision
resume_target
```

管理 API 明确拒绝 live LLM 模式，只能执行 dry-run/offline 研究。

## 11. 验收

实际完整 Dry-run：

```text
候选：SPY、QQQ
每候选结构化调用：13
总计划调用：26
每候选 LangGraph checkpoint：14
项目 JSON 完成节点：31
外部请求：0
账户修改：false
真实券商：false
```

专项测试覆盖：

1. 三分析师与三风险委员原生并行；
2. 两轮 Bull/Bear 条件循环；
3. 13 个角色均通过 LangChain Runnable；
4. 角色追踪不含 Prompt、输出和密钥；
5. SQLite pending-write 故障恢复；
6. `interrupt` + `Command(resume)`；
7. 成本感知 HOLD 条件路由；
8. 12 个并行 JSON 审计节点不丢失；
9. `stream_mode="updates"` 事件与图执行步数一致，且不包含状态 payload。

## 12. 尚未完成

v0.12.0 没有声称已经解决：

- 真实 API 在用户本地电脑上的全链路联调；
- 正式交易所日历与节假日处理；
- 供应商自动 fallback 与质量评分；
- 跨标的组合级 LangGraph；
- 按角色、模型和市场状态的收益贡献面板；
- LangSmith 云追踪部署；
- Docker daemon 代理修复；
- 真实券商连接。

这些内容属于后续成熟化工作，不能通过扩大 Agent 数量替代。
