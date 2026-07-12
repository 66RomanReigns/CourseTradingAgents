# 参考源码核对记录

## 已核对版本

用户上传 ZIP 解压到：

`../reference/TradingAgents`

项目版本为 `0.3.1`，Python 要求 `>=3.10`。源码包含 178 个压缩条目。

## 实际依赖与复杂度

参考项目的 `pyproject.toml` 直接依赖 LangGraph、LangChain 的 OpenAI/Anthropic/Google 适配、Backtrader、Redis、yfinance、stockstats、Rich 与 Typer；另有 Bedrock 可选依赖。这验证了原方案中“课程项目不应直接复制完整运行栈”的判断。

## 实际图结构

核心入口 `tradingagents/graph/trading_graph.py` 在初始化时：

1. 写入全局数据源配置；
2. 同时实例化 deep/quick 两类 LLM；
3. 创建 Market、Social、News、Fundamentals 四组 ToolNode；
4. 创建 Debate/Risk 条件逻辑；
5. 建立 Propagator、Reflector、SignalProcessor；
6. 组装 LangGraph 并支持 checkpoint；
7. 运行期还会使用 yfinance 计算事后收益和基准 alpha。

## 值得吸收的设计

- 分离分析节点与数据工具；
- 统一的状态对象；
- 对新闻和行情时间边界的测试；
- 对数据缺失、过期行情、符号规范化的防御性处理；
- 结构化输出和执行日志。

## 不直接继承的设计

- 十余个文本角色和多轮辩论；
- 运行主链路强绑定外部 LLM；
- 同时支持大量供应商和社交/预测市场数据；
- Redis、SQLite checkpoint 和 memory log 全部进入课程 MVP；
- 用角色讨论代替确定性仓位风控。

## 本项目对应实现

- `EvidencePack` 对应可审计、时间安全的统一状态输入；
- `QuantSignalAgent` 对应市场分析，但完全确定性；
- `CriticAgent` 将多轮辩论压缩为一次可度量的反证检查；
- `RiskGovernor` 使用硬规则而不是风险角色投票；
- `PaperBroker` 将自然语言信号真正落为目标仓位和成交；
- `BacktestEngine` 强制收盘决策、下一根开盘成交。
