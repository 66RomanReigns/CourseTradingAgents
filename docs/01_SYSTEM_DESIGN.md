# TradeLab-Agent 系统设计方案

## 1. 项目目标

实现一个适合课程展示和实验评估的多智能体交易系统。系统针对日频股票/ETF 数据，在指定日期生成交易建议，经过硬风险约束后进入本地模拟盘，并能够执行历史回测、记录决策依据、生成实验报告。

目标不是复制参考项目，而是构建一个更小、更可靠、更可解释的混合式 TradingAgents：

- LLM 只处理非结构化信息和反事实审查；
- 数值特征、风险控制、订单执行和绩效评估使用确定性代码；
- 默认完全离线可运行；
- 每次决策都可追溯到带时间戳的证据；
- 支持基线、消融和模拟盘闭环。

## 2. 核心创新点

### 2.1 Hybrid Agents：智能体不等于全部调用 LLM

将“智能体”定义为具有独立职责、输入输出协议和决策逻辑的模块。系统包含确定性智能体和 LLM 智能体，避免把所有计算都包装成提示词。

### 2.2 EvidencePack：证据先于结论

数据层生成统一的 `EvidencePack`，每条证据包含：

- `evidence_id`
- `symbol`
- `as_of`
- `available_at`
- `source`
- `field / value / unit`
- `window`
- `data_hash`

所有智能体输出必须引用 Evidence ID。未被证据支持的精确数值不得进入最终报告。

### 2.3 Deterministic Risk Governor：LLM 无权越过硬风控

风险模块根据组合状态和市场波动计算最大允许仓位。即使上游给出强烈 BUY，Risk Governor 也可以降为小仓位或拒绝交易。

### 2.4 Confidence Calibration：置信度由历史表现校准

智能体原始置信度不会直接使用。系统根据滚动窗口内同类信号的实际命中率进行校准，减少 LLM 过度自信。

### 2.5 Reproducibility First：固定快照和运行清单

每次运行生成 Manifest：

- 数据文件哈希；
- 配置哈希；
- 代码版本；
- 模型名与温度；
- 随机种子；
- 决策日期；
- 每个模块的输入输出摘要。

离线模式下，相同输入应得到一致的量化、风控和成交结果；LLM 部分可通过 Mock 回放实现完全复现。

## 3. 总体架构

```text
┌─────────────────────────────────────────────────────────────┐
│                       DataHub                               │
│ Local CSV / Cached Snapshot / Optional yfinance Adapter     │
└─────────────────────────────┬───────────────────────────────┘
                              │ time-aware slice
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ FeatureEngine + EvidenceBuilder                              │
│ returns, SMA, RSI, MACD, ATR, volatility, volume, drawdown   │
└─────────────────────────────┬───────────────────────────────┘
                              ▼
                      EvidencePack
                    ┌─────────┴─────────┐
                    ▼                   ▼
        ┌──────────────────┐  ┌─────────────────────┐
        │ QuantSignalAgent │  │ ContextAnalystAgent │
        │ deterministic    │  │ optional LLM        │
        └────────┬─────────┘  └──────────┬──────────┘
                 └────────────┬──────────┘
                              ▼
                    ┌──────────────────┐
                    │   CriticAgent    │
                    │ counter-evidence │
                    └────────┬─────────┘
                              ▼
                    ┌──────────────────┐
                    │ DecisionFusion   │
                    │ score + calibrate│
                    └────────┬─────────┘
                              ▼
                    ┌──────────────────┐
                    │  Risk Governor   │
                    │ hard constraints │
                    └────────┬─────────┘
                              ▼
                    ┌──────────────────┐
                    │   Paper Broker   │
                    │ orders / fills   │
                    └────────┬─────────┘
                              ▼
              Portfolio Ledger + Memory + Report
```

## 4. 模块设计

## 4.1 DataHub

职责：统一加载、校验、切片和缓存数据。

数据模式：

```python
MarketBar(
    symbol: str,
    timestamp: datetime,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    adjusted_close: float | None,
    source: str,
)
```

数据适配器：

1. `LocalCsvProvider`：默认必选，课程验收不依赖网络；
2. `YFinanceProvider`：可选在线更新；
3. `SnapshotProvider`：读取已冻结的数据快照；
4. 预留 `NewsSnapshotProvider`，只读取带发布时间的本地 JSONL。

时间约束：

- `slice(as_of=t)` 只能返回 `available_at <= t` 的记录；
- 回测时禁止直接访问完整 DataFrame；
- 所有数据入口经过同一时间切片器；
- 复权逻辑固定并写入 Manifest。

## 4.2 FeatureEngine

首版使用少量、互补、容易解释的指标：

- 收益：1/5/20 日收益；
- 趋势：SMA20、SMA60、价格相对均线；
- 动量：RSI14、MACD Histogram；
- 波动：ATR14、20 日年化波动率；
- 成交量：量比、20 日平均成交量；
- 风险：60 日最大回撤、下行波动率；
- 相对强弱：相对 SPY/基准的 20 日超额收益。

不在 MVP 中堆叠几十个高度相关指标。所有特征必须有单元测试，并保证只依赖当前时点及以前的数据。

## 4.3 QuantSignalAgent

类型：确定性智能体。

输入：`EvidencePack + PortfolioState`。

输出：

```python
AgentOpinion(
    agent="quant",
    direction=-1.0..1.0,
    confidence=0.0..1.0,
    horizon_days=int,
    evidence_ids=list[str],
    reasons=list[str],
    invalidation_conditions=list[str],
)
```

建议采用可解释的多因子评分，而不是训练复杂深度模型：

```text
quant_score =
    0.30 * trend_score
  + 0.25 * momentum_score
  + 0.20 * relative_strength_score
  + 0.15 * volume_confirmation
  - 0.10 * volatility_penalty
```

权重放在配置文件中，后续可通过训练集网格搜索，但不能使用测试集调参。

## 4.4 ContextAnalystAgent

类型：可选 LLM 智能体。

职责：处理价格指标无法表达的上下文，例如本地新闻快照、事件摘要和基本面描述。

约束：

- 只读取 EvidencePack 中提供的文本；
- 不能自行联网；
- 输出严格 JSON/Pydantic Schema；
- 必须给出引用的 Evidence ID；
- 没有新闻数据时明确返回 `insufficient_context`；
- 默认只调用一次，不进行开放式工具循环。

推荐输出：方向、置信度、事件有效期、主要催化剂、主要风险和证据引用。

MVP 可以先实现 `MockContextAgent`，根据已保存的 JSON 响应回放；配置 API 后再接入真实模型。

## 4.5 CriticAgent

类型：LLM 或规则混合智能体。

它不再扮演 Bull/Bear 双方进行多轮辩论，而是执行一次结构化反证审查：

1. 找出 Quant 与 Context 意见中的冲突；
2. 检查是否存在证据不足、时间错位和过度外推；
3. 给出最强反例；
4. 判断是否应降低置信度；
5. 输出 `PASS / DOWNGRADE / VETO`。

Critic 的价值可通过消融实验直接衡量。

## 4.6 DecisionFusion

类型：确定性融合器。

基础公式：

```text
raw_score = w_q * quant_direction * quant_confidence
          + w_c * context_direction * context_confidence

critic_factor = 1.0 / 0.6 / 0.0
calibrated_score = calibrator(raw_score) * critic_factor
```

映射规则示例：

- `score >= 0.35`：BUY 候选；
- `score <= -0.35`：SELL 候选；
- 其余：HOLD；
- 低数据质量强制 HOLD；
- Quant 与 Context 强冲突时降低仓位，而不是让语言模型随意裁决。

输出不仅包含方向，还包含 `target_weight` 初值、持有周期和失效条件。

## 4.7 Risk Governor

类型：确定性模块，是最终交易门控。

首版硬约束：

- 单标的最大仓位：20%；
- 最低现金比例：10%；
- 单次最大新增仓位：10%；
- 基于波动率的目标仓位缩放；
- 日换手上限；
- ATR 止损；
- 组合回撤熔断；
- 价格/成交量异常时禁止成交；
- 卖空默认关闭；
- 数据过期或缺失时强制 HOLD。

波动率缩放示例：

```text
target_weight = min(
    model_target_weight,
    max_position_weight,
    annual_risk_budget / annualized_volatility
)
```

LLM 输出永远不能绕过这些规则。

## 4.8 Paper Broker

本地模拟券商包含：

- 虚拟现金；
- 市价单；
- 订单状态；
- t 日收盘生成信号，t+1 开盘成交；
- 固定/比例手续费；
- 滑点模型；
- 持仓成本；
- 已实现/未实现盈亏；
- 交易日志和权益曲线。

接口预留 `BrokerAdapter`，后续可选接入 Alpaca Paper Trading，但课程验收默认不需要任何真实账户或外部密钥。

## 4.9 Memory 与 Reflection

不采用无限增长的自然语言记忆。每笔已完成交易保存结构化记录：

```python
TradeMemory(
    symbol,
    entry_date,
    exit_date,
    decision_features,
    agent_opinions,
    action,
    realized_return,
    benchmark_return,
    max_adverse_excursion,
    error_tags,
)
```

Reflection 只生成简短错误标签，例如：

- `trend_chasing`
- `ignored_volatility`
- `event_decay`
- `false_breakout`
- `risk_rule_saved_trade`

这些标签用于统计和调整置信度校准，不直接让 LLM 修改交易代码。

## 5. 状态与接口

核心运行状态：

```python
TradingState(
    run_id: str,
    symbol: str,
    decision_time: datetime,
    evidence_pack: EvidencePack,
    quant_opinion: AgentOpinion | None,
    context_opinion: AgentOpinion | None,
    critic_review: CriticReview | None,
    fused_decision: TradeDecision | None,
    risk_decision: RiskDecision | None,
    order: Order | None,
    fill: Fill | None,
    portfolio: PortfolioState,
    trace: list[TraceEvent],
)
```

最终决策 Schema：

```python
TradeDecision(
    action: Literal["BUY", "HOLD", "SELL"],
    score: float,
    confidence: float,
    proposed_target_weight: float,
    horizon_days: int,
    evidence_ids: list[str],
    invalidation_conditions: list[str],
)
```

## 6. 编排方式

MVP 不使用 LangGraph，采用显式 Pipeline：

```python
state = load_state(request)
state = data_stage(state)
state = quant_stage(state)
state = context_stage(state)
state = critic_stage(state)
state = fusion_stage(state)
state = risk_stage(state)
state = execution_stage(state)
state = report_stage(state)
```

每个 Stage：

- 只修改自己负责的字段；
- 输入输出使用 Pydantic；
- 失败时返回明确错误；
- 写 TraceEvent；
- 可单独测试和重放。

后续只有在确实需要并发、条件循环或中断恢复时，才考虑增加 LangGraph 适配层。

## 7. 技术栈

建议基础栈：

- Python 3.11；
- pandas、numpy；
- pydantic；
- typer；
- PyYAML；
- httpx；
- pytest；
- matplotlib；
- FastAPI + Uvicorn（展示阶段）；
- SQLite（订单、持仓、运行记录）；
- 可选 yfinance；
- 可选 OpenAI-compatible SDK/Ollama。

服务器当前系统 Python 为 3.14.6，部分金融/LLM 依赖可能尚未完整兼容，因此正式运行环境使用 Docker 或 `uv python install 3.11`，不污染系统环境。

## 8. 部署形态

Docker Compose 规划：

```text
trading-api       FastAPI 服务
trading-worker    回测/模拟盘任务
ollama            可选本地 LLM 服务
```

MVP 可以先合并 API 和 Worker，使用 SQLite；不引入 Redis、Celery 和微服务，避免过度设计。

## 9. 安全与边界

- 默认只允许本地模拟盘；
- 不保存真实券商密钥；
- `.env` 不进入 Git；
- 所有外部请求设超时与缓存；
- 日志避免记录密钥；
- 页面明确显示“课程研究，不构成投资建议”；
- 默认禁止真实下单适配器；
- 任何连接真实交易系统的功能必须单独配置和人工确认。

## 10. 预期课程展示亮点

1. 展示智能体协作轨迹，而不是只展示最后一段文字；
2. 点击每条结论可追溯到 Evidence ID；
3. 展示 Risk Governor 如何拒绝过大仓位；
4. 展示同一日期的 Quant-only、Hybrid、Hybrid+Critic 对比；
5. 展示无未来数据泄漏的 t→t+1 成交机制；
6. 展示回测指标、权益曲线、最大回撤和交易日志；
7. 关闭网络后仍可用样例数据完成完整演示。

## 11. 非目标

首版不实现：

- 高频或分钟级交易；
- 真实资金下单；
- 复杂期权与衍生品；
- 社交媒体全网爬虫；
- 十余个角色的多轮开放式辩论；
- 端到端深度强化学习；
- 用测试集调参后宣称策略有效。

本项目优先保证工程完整性、实验可信度和课程可讲解性。