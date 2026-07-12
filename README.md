# CourseTradingAgents / TradeLab-Agent

一个面向课程作业的、可复现的多智能体交易研究与模拟盘系统。

> 当前阶段：已完成参考项目拆解、需求裁剪、系统架构和实验方案设计；尚未开始正式编码与部署。

## 项目定位

本项目参考 TauricResearch/TradingAgents 的“多角色协作分析”思想，但不会复制其复杂的角色拓扑和数据接入层。我们的实现重点是：

1. **少而有效的智能体**：只保留能形成可验证增益的角色。
2. **LLM 与量化规则分工**：LLM 负责非结构化信息理解和反思，数值计算、风控、下单全部由确定性程序完成。
3. **离线可运行**：默认使用本地 CSV 和缓存数据，网络或 API 不可用时仍能完成演示、回测和评分。
4. **严格防止未来数据泄漏**：决策只使用交易时点之前的数据，信号在 t 日生成、最早在 t+1 开盘成交。
5. **可复现实验**：固定数据快照、配置、随机种子和运行轨迹，支持基线与消融实验。
6. **模拟盘闭环**：决策不仅输出 BUY/HOLD/SELL，还要经过风险约束、订单生成、成交模拟、持仓和盈亏更新。

## 核心流水线

```text
历史行情 / 本地新闻快照
          │
          ▼
DataHub + FeatureEngine
          │  生成带时间戳和来源的 EvidencePack
          ▼
Quant Agent ───────┐
                   ├──> Critic Agent ──> Decision Fusion
Context Agent ─────┘                         │
                                             ▼
                                      Risk Governor
                                             │
                                             ▼
                                      Paper Broker
                                             │
                                             ▼
                              Portfolio / PnL / Memory / Report
```

## 与参考项目的主要区别

- 不使用十余个 LLM 角色反复辩论，而是采用 **1 个确定性量化智能体 + 1 个可选上下文智能体 + 1 个批判智能体**。
- 不把风险控制交给语言模型，而是用硬约束实现仓位、波动率、回撤、止损和换手限制。
- 不依赖 Reddit、StockTwits、Polymarket、Alpha Vantage 等多源接口；默认只要求本地数据，在线数据是可替换适配器。
- 不要求 LangGraph；MVP 使用透明的状态机/DAG，降低调试难度和依赖复杂度。
- 不只生成分析文本，而是提供带手续费和滑点的回测、模拟订单、持仓账本和实验指标。
- 引入 EvidencePack、数据哈希、时间切片和基线/消融实验，使课程成果可验证。

## 计划目录

```text
CourseTradingAgents/
├── config/                 # YAML 配置
├── data/sample/            # 离线样例数据
├── docs/                   # 设计与实验文档
├── src/tradinglab_agents/
│   ├── agents/             # Quant / Context / Critic / Fusion
│   ├── data/               # 数据适配、缓存、时间切片
│   ├── engine/             # 编排、状态、特征与证据包
│   ├── risk/               # 确定性风险控制
│   ├── broker/             # 模拟盘、订单与持仓
│   ├── evaluation/         # 回测、指标、基线、消融
│   └── api/                # CLI / Web API
├── tests/
├── scripts/
├── docker/
└── artifacts/              # 运行报告、图表和日志
```

## 文档

- `docs/00_REFERENCE_ANALYSIS.md`：参考项目拆解与裁剪依据。
- `docs/01_SYSTEM_DESIGN.md`：完整系统方案、模块接口和原创改进点。
- `docs/02_IMPLEMENTATION_ROADMAP.md`：实施步骤、验收标准与实验设计。
- `docs/03_SERVER_NOTES.md`：服务器环境及网络约束记录。

## 参考

- 参考仓库：https://github.com/TauricResearch/TradingAgents
- 论文：https://arxiv.org/abs/2412.20138

本项目仅用于课程研究与软件工程演示，不构成投资建议，也不连接真实资金账户。