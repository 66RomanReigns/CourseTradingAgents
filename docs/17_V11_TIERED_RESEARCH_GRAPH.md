# v0.11.0 分层研究图、风险委员会与记忆反馈

版本：v0.11.0

## 1. 目标

v0.11.0 不复制参考项目的 LangGraph 拓扑，而是把已有原子 checkpoint 运行时扩展为原创、配置驱动的研究 DAG。核心目标是：

1. 让不同复杂度角色使用不同模型档位；
2. 将一次性 Bull/Bear 观点扩展为可恢复的多轮辩论；
3. 在语言模型交易意图与确定性 PortfolioRiskGovernor 之间增加结构化风险委员会；
4. 保存完整研究轨迹，并在结果成熟后以点时安全方式反馈给未来研究；
5. 保持最终 `trader` 输出兼容模拟盘、人工审批和硬风控。

## 2. 默认 13 节点研究图

单个候选标的默认执行：

```text
News Analyst ───────┐
Macro Analyst ──────┼─> Bull/Bear Round 1
Fundamental Analyst ┘          │
                               v
                      Bull/Bear Round 2
                               │
                               v
                      Research Manager
                               │
                               v
                     Preliminary Trader
                      /        |        \
             Aggressive   Balanced   Conservative
                      \        |        /
                               v
                      Portfolio Manager
                               │
                               v
                    final trader contract
```

节点及调用数：

- 三个分析师：3；
- 两轮 Bull/Bear：4；
- Research Manager：1；
- Preliminary Trader：1；
- 三类 Risk Reviewer：3；
- Portfolio Manager：1；
- 合计：13 次/候选；Top-2 为 26 次。

每个节点均保存独立 JSON checkpoint。失败恢复时只重跑未完成节点，完整设置哈希变化时拒绝恢复。

## 3. quick/deep 模型分层

默认配置仍使用同一个 `glm-4.7-flash`，但逻辑上已经分离：

```yaml
llm:
  quick_model: glm-4.7-flash
  deep_model: glm-4.7-flash
```

quick 节点：

- News Analyst；
- Macro Analyst；
- Fundamental Analyst；
- Aggressive/Balanced/Conservative Risk Reviewer。

deep 节点：

- 每轮 Bull/Bear；
- Research Manager；
- Preliminary Trader；
- Portfolio Manager。

两个档位使用独立缓存命名空间：

```text
artifacts/llm_cache/quick/
artifacts/llm_cache/deep/
```

因此本地电脑后续可以把 quick 节点切换到便宜、低延迟模型，把 deep 节点切换到推理能力更强的模型，而不改变研究图和模拟盘接口。

## 4. 多轮辩论

配置：

```yaml
workflow:
  debate_rounds: 2
```

允许 1–3 轮。第二轮及后续轮次会收到：

- 三分析师结构化结论；
- 自己上一轮观点；
- 对手上一轮观点；
- 已成熟的历史决策记忆。

Bull/Bear 必须继续引用当前 EvidencePack 中真实存在的 evidence ID。轮数变化会进入设置哈希和调用预算，不能与旧 checkpoint 混合。

## 5. 三类风险委员

每个委员输出严格 `RiskReview`：

```text
persona
verdict: APPROVE | REDUCE | VETO
confidence
max_target_weight
rationale
evidence_ids
conditions
```

约束：

- 委员不能提高 Preliminary Trader 的目标仓位；
- VETO 必须将最大目标仓位设为 0；
- 风险意见仍必须引用可见证据；
- 风险委员会不是最终硬风控，后续仍经过确定性 PortfolioRiskGovernor。

三种 persona 关注点不同：

- Aggressive：允许较高风险预算，但仍受证据和仓位上限约束；
- Balanced：在收益机会与回撤之间折中；
- Conservative：更强调不确定性、分歧和资本保护。

## 6. Portfolio Manager 最终约束

Portfolio Manager 输出仍使用原有 `TradePlan`，因此下游不需要重写。

强制规则：

1. 不能提高 Preliminary Trader 的目标仓位；
2. 任一 BUY VETO 会强制最终 HOLD；
3. REDUCE 形成的最小仓位上限必须被遵守；
4. 保护性 SELL 不能被弱化为 HOLD 或 BUY；
5. 所有方向性操作继续要求人工审批；
6. 不连接真实券商。

之后 Quant、Fusion、Critic、Regime Guard 和 PortfolioRiskGovernor 仍拥有最终确定性约束权。

## 7. 完整研究轨迹记忆

进入模拟盘的 overlay 保留最终 `action/confidence/target_weight`，并附带：

```text
clients
analyst outputs
debate rounds
research manager
preliminary trader
risk reviews
portfolio manager
```

这些内容写入不可变决策记忆的 `rationale_json.research_trace`。原始研究事实不会被后续反思覆盖。

观察期成熟后，`deterministic_outcome_attribution_v2` 追加：

- 标的收益；
- 基准收益；
- Alpha；
- failure type；
- Preliminary Trader 与 Portfolio Manager 是否发生改判；
- VETO/REDUCE 数量；
- 辩论轮数；
- quick/deep 模型身份。

## 8. 点时安全记忆反馈

默认配置：

```yaml
workflow:
  memory_feedback_enabled: true
  memory_feedback_limit: 5
```

未来研究只读取：

- 同一个模拟账户；
- 同一个标的；
- 状态为 MATURED；
- `outcome_timestamp <= 当前 decision_time`；
- 最近最多 5 条。

反馈内容为紧凑结构：action、收益、Alpha、失败类型、lesson 和委员会归因，不包含未来数据、密钥、原始 Prompt 或完整市场文件。

这些记忆会提供给：

- Bull/Bear；
- Research Manager；
- Risk Reviewers；
- Portfolio Manager。

分析师仍只分析当前点时证据，避免把历史结果伪装成新的市场事实。

## 9. 本地运行

服务器可继续执行完全离线验收：

```bash
make workflow-dry-run
```

在具备正常外网的本地电脑上，配置外部密钥后执行：

```bash
scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli workflow-run \
  --mode live --confirm-live \
  --config config/default.yaml
```

真实模式仍先执行无凭据网络检查，且永远不会连接真实券商。

## 10. 后续优先级

1. 用本地真实数据完成 quick/deep 模型消融；
2. 对 1/2/3 轮辩论进行成本与决策稳定性比较；
3. 聚合角色级长期表现，形成 analyst/risk persona calibration；
4. 增加交易所正式日历和 walk-forward 实验；
5. 增加可选的 LLM reflection，但只能追加，不能修改原始记忆。
