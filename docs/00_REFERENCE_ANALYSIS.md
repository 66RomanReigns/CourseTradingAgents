# 参考项目拆解与课程化裁剪

## 1. 参考对象

- 项目：TauricResearch/TradingAgents
- 仓库：https://github.com/TauricResearch/TradingAgents
- 论文：https://arxiv.org/abs/2412.20138
- 分析时间：2026-07-12
- GitHub 页面显示版本：v0.3.1

## 2. 参考项目核心结构

TradingAgents 模拟现实交易机构中的多角色协作，大致包含：

1. 分析师团队
   - Market / Technical Analyst
   - Sentiment Analyst
   - News Analyst
   - Fundamentals Analyst
2. 多空研究团队
   - Bull Researcher
   - Bear Researcher
   - Research Manager
3. 交易与风险团队
   - Trader
   - Aggressive Risk Analyst
   - Neutral Risk Analyst
   - Conservative Risk Analyst
   - Portfolio Manager
4. 工程基础设施
   - LangGraph 状态图与条件路由
   - Tool Calling 数据获取
   - 多 LLM Provider 适配
   - SQLite Checkpoint
   - 决策记忆与复盘
   - CLI、Docker、缓存和多市场支持

典型流程是：四类分析师生成报告，多空研究员辩论，Research Manager 汇总，Trader 生成交易建议，三类风险智能体再次辩论，Portfolio Manager 给出最终决策。

## 3. 值得保留的思想

### 3.1 职责分离

将行情、基本面、新闻、决策和风险拆分，能够减少单一提示词承担过多职责的问题。

### 3.2 结构化状态传递

不同模块不只传递自然语言，还通过统一状态保存报告、交易计划、最终决策和历史上下文。课程项目应继续使用 typed state / schema。

### 3.3 工具调用与证据 grounding

参考项目要求分析师先调用行情和指标工具，并用验证后的市场快照约束精确价格描述。这一思想应强化为本项目的 `EvidencePack`。

### 3.4 决策记忆与结果复盘

将历史决策、后续收益和反思重新注入下一次决策，可以形成简单的持续学习闭环。

### 3.5 研究而非真实投顾

参考项目明确强调结果受模型、数据、时间区间和随机性影响。课程项目也必须明确仅用于实验和模拟盘。

## 4. 不适合直接用于课程作业的部分

### 4.1 角色数量过多，难以证明每个角色有增益

十余个 LLM 节点会带来大量调用成本和长链路误差。Bull/Bear 辩论、三种风险偏好辩论在展示上直观，但如果没有消融实验，很难证明它们优于一个结构化决策器。

### 4.2 多轮自然语言辩论容易产生“伪协作”

多个角色可能只是重复同一组分析材料，以不同语气生成文本。对课程项目而言，应该让不同智能体使用不同输入、输出不同结构化变量，并通过实验衡量边际贡献。

### 4.3 风险管理不应主要由 LLM 决定

仓位上限、最大回撤、波动率缩放、止损、换手和现金约束都可以精确计算。让 LLM 扮演激进/中性/保守分析师，不能替代确定性风险规则。

### 4.4 数据源过多且不稳定

参考项目集成 yfinance、Alpha Vantage、FRED、Reddit、StockTwits、Polymarket 等来源。问题包括：

- 部分接口需要 API Key；
- 限流、字段变化和网络故障会导致流程失败；
- 历史新闻和社交数据难以按交易日期严格回放；
- 实时内容会变化，难以复现实验；
- 多源信息容易产生时间错位和未来数据泄漏。

### 4.5 LangGraph 对本作业不是必要条件

LangGraph 适合复杂条件路由、断点续跑和工具循环，但会增加理解和调试成本。MVP 只需要有限状态机或显式 DAG，就能完整展示多智能体协作。

### 4.6 LLM Provider 适配范围过大

参考项目支持大量模型厂商和推理参数。课程作业只需统一的 `LLMClient` 接口，支持：

- OpenAI-compatible API；
- Ollama；
- Mock/Rule 模式。

### 4.7 仅输出文本决策不够形成实验闭环

课程项目需要从信号到订单、成交、持仓和收益的完整链路，同时与 Buy-and-Hold、均线策略等基线比较。

## 5. 本项目的裁剪原则

| 参考项目设计 | 本项目处理方式 | 原因 |
|---|---|---|
| 4 个 LLM 分析师 | 量化智能体 + 可选上下文智能体 | 降低调用成本，避免重复分析 |
| Bull/Bear 多轮辩论 | 1 个 Critic 智能体进行反证审查 | 保留对抗思考，但减少冗余 |
| 3 个风险辩论智能体 | 确定性 Risk Governor | 风控应可验证、可测试 |
| Portfolio Manager LLM | 规则融合器输出目标仓位 | 保证约束严格执行 |
| 多数据供应商链 | Local CSV 为主、yfinance 可选 | 离线可运行和可复现 |
| LangGraph | 显式 Pipeline/State Machine | 更适合课程讲解和单元测试 |
| 多厂商 LLM 支持 | OpenAI-compatible / Ollama / Mock | 控制工程范围 |
| 文本报告 | typed decision + report | 便于回测和自动评估 |
| 动态在线新闻 | 固定时间戳新闻快照 | 防止历史回测看见未来信息 |

## 6. 课程项目真正要回答的问题

本项目不试图证明“多智能体一定能赚钱”，而是回答以下更可验证的问题：

1. 在相同市场数据下，混合式多智能体是否优于纯规则策略？
2. Critic 智能体是否能减少高置信度错误交易？
3. 确定性风险约束能否显著降低最大回撤？
4. LLM 上下文分析在加入成本后是否仍有边际收益？
5. 系统能否在无外部 API 的情况下复现实验结果？

## 7. 结论

参考项目最有价值的是“角色分工、状态传递、工具 grounding、记忆复盘”的思想，而不是角色数量本身。本课程项目将以可复现性、可解释性和完整模拟盘闭环为优先，采用更少的 LLM 节点和更多确定性模块，形成具有自身风格的独立实现。