# 实施路线与验收标准

## 1. 总体策略

项目分为四个递进阶段。每个阶段都必须可运行、可测试，避免直到最后才形成闭环。

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

## 4. 阶段 C：模拟盘服务与展示

### 任务

1. FastAPI 提供分析、回测、订单、持仓和报告接口；
2. SQLite 保存运行、订单、成交和组合；
3. 简单 Web 页面展示：
   - 当前组合；
   - 智能体意见；
   - 证据链；
   - 风控修改；
   - 订单和收益；
4. Docker Compose 部署；
5. 增加定时模拟交易脚本，但默认不连接真实账户。

### 验收标准

- 服务重启后持仓与订单不丢失；
- 页面可以查看任意一次决策的完整 Trace；
- API 健康检查正常；
- Docker 一条命令启动；
- 无 API Key 时仍能运行离线 Demo。

## 5. 阶段 D：实验与课程报告

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

## 6. 测试计划

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

## 7. 课程交付物

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

## 8. 开发顺序

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

## 9. 第一轮编码的具体清单

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