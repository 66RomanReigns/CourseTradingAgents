# 实施路线与验收标准

## 1. 总体策略

项目已重新定位为“持续模拟盘增强版”，按 `P0 → P1 → P2 → P3 → P4` 推进。每个阶段都必须可运行、可测试，避免直到最后才形成闭环。

### 当前状态（v0.18.0）

- **P10 Market Semantics 已完成**：正式 XNYS 会话、完整同步、点时拆股/分红、总回报复权和 Paper schema v3 公司行动账本；
- **P11 Provider Graph 已完成**：能力注册、持久健康状态、动态资源 fan-out、Twelve Data→Alpha Vantage→本地缓存路由、冲突解析、质量门控、共享节流、请求前额度预检和 DATA_PROVIDER thread 恢复均已完成。

- **P0 已完成**：回撤熔断强制清仓、风险状态机、严格配置校验、Python 3.11 统一入口、精确依赖锁和测试入口修复；
- **P1 已完成**：结构化 JSON Schema、分类型分析师、Bull/Bear、Research Manager、Trader、Evidence ID 校验、调用日志和缓存；
- **P2 已完成**：严格多标的市场快照、缺价失败的组合估值、先卖后买的多资产模拟成交、组合级风险预算、同步五标的离线回测；
- **P3 已完成**：持久模拟账户、订单队列、人工审批、过期处理、下一开盘执行、交易日锁、崩溃恢复、本地 API 和 HTML 仪表板；
- **P4 运行时硬化已完成**：GLM-4.7-Flash provider、dry-run/offline/live 三模式、供应商与七角色节点级 checkpoint、设置哈希恢复保护、API Token 认证、SQLite v2 迁移、结构化决策记忆、五日收益/Alpha 归因和统一账户锁；
- **P4 Live Provider 安全层已完成**：无凭据校园网门户/TLS 检查、供应商调用账本、真实模型 token 遥测、最小冒烟入口、显式 fail-closed/last-known-good 策略、增量行情与新闻刷新；
- **P5 LangChain/LangGraph 主运行时已完成**：LangChain Prompt/Runnable 与安全追踪、原生 LangGraph 并行分支、循环辩论、条件路由、SQLite thread checkpoint、pending-write 恢复、可选 interrupt/Command 人工审阅和双层审计；
- **P6 组合级 Portfolio Supervisor 已完成**：跨标的相关性与集中度并行评审、deep Portfolio Supervisor、确定性 non-expansion guard、高相关簇限额、独立 PORTFOLIO thread、恢复测试和 CLI/API 图可视化；
- **P7 顶层 Research Parent Graph 已完成**：Candidate Screening 进入原生 `Send` 动态 Top-K fan-out，候选子图完成后 fan-in 到 Portfolio Supervisor，独立 RESEARCH_PARENT thread、父级 pending-write 恢复、CLI/API 图可视化和顺序路径回退均已完成；
- **P8 Workflow Core Graph 已完成**：数据验证、Research Parent、Overlay Assembly 和 Decision Preparation 进入独立 WORKFLOW_CORE thread；完整 Overlay 通过集合一致性、订单、审批、仓位上限和 non-expansion 校验后生成 SHA-256 交接哈希；
- **P9 Deterministic Decision Graph 第一阶段已完成**：每个账户标的使用子图执行 Evidence、Quant、Fusion/Critic、Regime 与 Overlay Target，全部标的 fan-in 到 PortfolioRiskGovernor 和计划哈希；独立 DECISION thread、输入指纹、逐字段顺序等价、节点恢复和 Paper 决策记忆溯源均已完成；
- **P11 后续成熟化待完成**：执行分阶段真实 Provider smoke test，加入行业/风格因子暴露约束、真实公司行动供应商适配、角色/模型收益归因、主动告警和备份恢复；Paper 审批、订单落库与执行是否迁入子图仍必须以数据库幂等和事务边界为前提。

## 2. 阶段 A：离线可复现 MVP

### 任务

1. 建立 Python 3.11 + uv/Docker 环境；
2. 定义 Pydantic 数据模型；
3. 实现 LocalCsvProvider 和时间切片；
4. 实现 FeatureEngine；
5. 实现 QuantSignalAgent；
6. 实现 DecisionFusion 的 Quant-only 模式；
7. 实现 Risk Governor；
8. 实现 Paper Broker；
9. 实现日频回测器和基础指标；
10. 加入样例数据和 CLI。

### 验收标准

- 不联网可以运行；
- 使用相同配置与样例数据，结果一致；
- t 日信号只能在 t+1 成交；
- 单元测试能检测未来数据泄漏；
- 输出订单、成交、持仓、权益曲线和指标；
- 能与 Buy-and-Hold、SMA Cross 基线比较。

## 3. 阶段 B：混合式多智能体

### 任务

1. 定义统一 LLMClient；
2. 实现 MockLLMClient；
3. 实现 ContextAnalystAgent；
4. 实现 CriticAgent；
5. 实现证据引用验证；
6. 实现置信度校准；
7. 保存完整运行 Trace 和 Manifest。

### 验收标准

- LLM 输出必须通过 Schema 校验；
- 引用不存在的 Evidence ID 时自动拒绝或降级；
- LLM 不可直接产生订单；
- 关闭 Context 或 Critic 后系统仍可运行；
- Mock 模式可以重放固定输出；
- 能完成 Quant-only、Hybrid、Hybrid+Critic 三组消融。

## 4. 阶段 C：多资产组合回测（P2，已完成）

### 已完成任务

1. 新增严格 `MarketSnapshot`，组合缺少任一非零持仓价格时立即失败；
2. 新增 `AlignedMarketData`，只在全部标的共同时间戳上决策和成交；
3. 改造 `Portfolio` 和 `PaperBroker`，支持完整组合估值及先卖后买；
4. 新增 `PortfolioRiskGovernor`，限制单标的权重、持仓数量、总暴露和组合回撤；
5. 新增 `MultiAssetBacktestEngine`；
6. 新增 SPY、QQQ、AAPL、MSFT、NVDA 的 2018—2025 离线合成夹具；
7. 新增 `multi-backtest` CLI 与 `make multi-backtest`；
8. 增加多资产回归测试和执行后暴露审计。

### 验收标准

- 缺失持仓价格不能静默按零估值；
- 所有标的使用同一个同步开盘或收盘快照；
- 保护性减仓绕过策略阈值和冷却期；
- 成交后单标的权重和组合总暴露满足配置；
- 不出现负现金；
- 回撤熔断能够一次性清空整个组合；
- 无 API Key、无网络时仍可完整运行。

## 5. 阶段 D：持久化模拟盘（P3，已完成）

### 已完成任务

1. SQLite 保存模拟账户、持仓、日运行、订单、审批事件、成交和净值快照；
2. 实现 `PENDING_APPROVAL → APPROVED/REJECTED/CANCELLED/EXPIRED → EXECUTED` 状态机；
3. 实现下一同步交易日开盘执行和收盘后生成下一订单队列；
4. 实现 `ALL`、`RISK_AUTO` 和 `NONE` 三种审批策略；
5. 实现同交易日幂等、确定性订单 ID 和过期订单保护；
6. 持久化 Regime Guard 与组合风险状态；
7. 实现开盘成交提交后崩溃的阶段恢复，避免重复成交；
8. 增加非阻塞调度锁和 `paper-next` 一次性调度入口；
9. 增加账户、订单和审批 CLI 与本地 FastAPI；
10. 增加无前端依赖的 HTML 模拟账户仪表板。

### 验收标准

- 服务重启后现金、持仓、订单、成交和风险状态不丢失；
- 未审批订单错过开盘后自动过期；
- 同一天重复调用不重复订单或成交；
- 开盘成交后的进程故障可恢复且不重复成交；
- 并发调度被文件锁阻止；
- 页面可查看账户、持仓、审批队列、成交和净值；
- 无 API Key、无网络时仍能完整运行；
- 所有入口明确禁止真实券商和真实资金。

## 6. 阶段 E：容器化与长期运行（P4，框架完成、生产硬化中）

### 已完成运行时硬化

1. 将远程研究 provider 配置为 `zhipu / glm-4.7-flash`，默认 `dry_run`，真实调用必须显式确认；
2. 实现统一 `DailyWorkflow`、Top-2 单标的 26 次调用与组合监督 3 次调用，共 29 次结构化调用的应用层预算；
3. 将四类数据资源纳入独立 DATA_PROVIDER LangGraph，并将每个候选标的 13 个分层研究节点拆分为原子 checkpoint；
4. 增加 `run_id` 恢复、完整设置 SHA-256 校验和失败节点定位；
5. 实现行情、新闻、宏观和基本面的增量合并；
6. 增加 FastAPI 操作令牌，缺少服务端令牌时非公共接口 fail closed；
7. SQLite 引入 `PRAGMA user_version=2`、自动迁移和未来版本拒绝；
8. 增加不可变决策记忆、五日收益/基准/Alpha 归因和确定性失败类型；
9. API、CLI、DailyWorkflow 和 scheduler 统一使用按账户文件锁；
10. 增加 Compose API、dry-run 和显式 live profiles，本机绑定和真实券商禁用保持不变。

### 外部联调与长期运行待完成

1. 完成 Twelve Data、Alpha Vantage、FRED、SEC EDGAR 和 GLM 的分阶段最小真实请求验证；
2. 修复 Docker daemon 代理并完成镜像构建；
3. 增加 TLS、审批主体权限和令牌轮换流程；
4. 增加 SQLite 自动备份、恢复演练和迁移回滚策略；
5. 增加非 XNYS 资产元数据、停牌和代码变更处理；
6. 完成真实 Provider smoke test、响应头额度校准、staging 数据审计和原子发布；
7. 增加日志轮转、运行监控、健康告警和可选通知；
8. 完成真实数据 walk-forward 与 LLM 增量价值消融。

## 7. 阶段 F：实验与课程报告

### 数据划分

建议选取 5–10 个高流动性标的和一个市场基准，例如：

- AAPL、MSFT、NVDA、AMZN、GOOGL；
- SPY 作为基准；
- 可增加 1–2 个行业 ETF 检验泛化。

使用时间顺序划分：

- Train：用于选择阈值和因子权重；
- Validation：用于确定配置；
- Test：只做一次最终评估。

禁止随机打乱时间序列。

### 基线

1. Buy-and-Hold；
2. SMA20/60 Cross；
3. RSI Mean Reversion；
4. Quant-only；
5. Hybrid without Critic；
6. Full Hybrid；
7. 可选 Random Strategy 作为 sanity check。

### 指标

- 累计收益；
- 年化收益；
- 年化波动率；
- Sharpe Ratio；
- Sortino Ratio；
- 最大回撤；
- Calmar Ratio；
- 胜率；
- Profit Factor；
- 换手率；
- 交易次数；
- 手续费和滑点成本；
- 相对基准 Alpha；
- Critic Veto 后避免的损失比例。

### 消融实验

| 实验 | Quant | Context | Critic | Risk | 目的 |
|---|---:|---:|---:|---:|---|
| A | ✓ |  |  | ✓ | 量化基础性能 |
| B | ✓ | ✓ |  | ✓ | 上下文智能体边际贡献 |
| C | ✓ | ✓ | ✓ | ✓ | Critic 是否减少错误 |
| D | ✓ | ✓ | ✓ |  | 风控对回撤的影响 |
| E |  | ✓ | ✓ | ✓ | LLM 单独使用是否可靠 |

### 需要诚实报告的内容

- 结果对时间区间的敏感性；
- LLM 成本和延迟；
- 网络数据不可复现问题；
- 交易次数过少导致的统计不稳定；
- 参数选择是否存在过拟合；
- 策略是否只在少数标的有效。

## 8. 测试计划

### 单元测试

- 数据字段校验；
- 时间切片不泄漏未来数据；
- 指标数值正确；
- Risk Governor 仓位约束；
- 手续费/滑点计算；
- 订单与成交状态机；
- Pydantic Schema；
- Evidence ID 引用验证。

### 集成测试

- 单标的完整回测；
- 多标的组合更新；
- 服务重启恢复；
- Mock LLM 重放；
- 数据缺失时降级为 HOLD；
- 网络失败自动使用缓存/本地数据。

### 防未来函数测试

构造一个在 t+1 暴涨的样例，确认 t 日特征和决策不包含 t+1 信息。回测执行价必须来自 t+1 开盘，而不是 t 日收盘。

## 9. 课程交付物

1. 完整源码；
2. Dockerfile / docker-compose；
3. 离线样例数据；
4. 配置文件；
5. 单元测试；
6. 系统设计文档；
7. 实验报告；
8. Demo 页面或 CLI 录屏；
9. 一组固定可复现的运行结果；
10. 项目原创性说明与参考项目差异表。

## 10. 开发顺序

严格按以下顺序实施：

```text
数据正确性
  → 特征正确性
  → 量化决策
  → 风险与模拟成交
  → 回测与基线
  → LLM 上下文
  → Critic
  → API/UI
  → 实验与报告
```

不应先搭建复杂页面或十几个提示词，再补数据和回测。

## 11. 第一轮编码的具体清单

下一步应直接实现：

1. `pyproject.toml`；
2. `models.py`：MarketBar、Evidence、AgentOpinion、TradeDecision；
3. `LocalCsvProvider`；
4. `FeatureEngine`；
5. `QuantSignalAgent`；
6. `RiskGovernor`；
7. `PaperBroker`；
8. `BacktestEngine`；
9. `cli.py`；
10. 对应 pytest。

完成这十项后，项目就具备一个不依赖 LLM 的可运行骨架，再逐步加入真正有实验价值的智能体。