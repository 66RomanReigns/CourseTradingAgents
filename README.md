# CourseTradingAgents / TradeLab-Agent

一个面向课程作业的、可复现的多智能体交易研究与模拟盘系统。

> 当前版本：`0.2.0`。离线数据、证据包、三个智能体、确定性风控、模拟成交、回测、基线、消融实验、CLI 与 FastAPI 均已实现。

## 项目定位

本项目参考 TauricResearch/TradingAgents 的“多角色协作分析”思想，但没有复制其十余个角色、多轮辩论和大量外部数据接口。项目重点是：

1. **少而有效的智能体**：Quant、Context、Critic 三个职责明确的模块。
2. **LLM 与量化规则分工**：上下文模块可替换模型；数值计算、风控和成交保持确定性。
3. **离线可运行**：默认使用本地 CSV、JSONL 新闻和 MockLLM。
4. **严格时间边界**：证据必须满足 `available_at <= decision_time`，收盘决策、下一交易日开盘成交。
5. **可复现实验**：固定随机种子、数据快照、配置、交易成本和模块消融。
6. **模拟盘闭环**：信号经过目标仓位、风险审查、滑点、手续费、现金与持仓更新。

项目只用于教学研究和模拟盘，不连接真实资金账户，也不构成投资建议。

## 系统流水线

```text
本地 OHLCV / 时间安全新闻
             ↓
Data Provider + Feature Engine
             ↓
EvidencePack（来源、时间、Evidence ID）
             ↓
Quant Agent ─────────┐
                     ├→ Decision Fusion → Critic Agent
Context Agent/MockLLM┘                         ↓
                                         Risk Governor
                                               ↓
                                          Paper Broker
                                               ↓
                              Portfolio / Metrics / Trace / Report
```

## 主要原创改进

- 用 `EvidencePack` 强制每个结论绑定可审计证据，并拒绝未来证据和重复 ID。
- 将多轮多空辩论压缩为一次 Critic 反证检查，降低成本并支持消融。
- 风控不是语言角色投票，而是持续生效的仓位上限、置信度阈值和回撤熔断。
- 仓位因价格上涨漂移超限时，即使上层信号为 HOLD，Risk Governor 也能强制减仓。
- 引入最小调仓阈值和冷却周期，避免每天微调造成不必要换手。
- 所有基线与智能体使用相同暖启动区间、手续费、滑点和执行时点。

## 快速运行

服务器项目目录：

```bash
cd /home/amax/mcp-workspace/projects/trading-agent-course/CourseTradingAgents
```

生成固定样例数据并运行全部测试：

```bash
make test
```

运行完整智能体：

```bash
make backtest
```

运行基线和消融实验：

```bash
make experiment
```

也可以直接使用 CLI：

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli experiment \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml \
  --output artifacts/experiment.json \
  --markdown artifacts/experiment.md
```

## 当前实验结果

固定合成数据包含上涨、下跌和恢复三种阶段。当前结果如下：

| 方案 | 总收益 | Sharpe | 最大回撤 | 成交数 |
|---|---:|---:|---:|---:|
| 完整智能体 | 5.91% | 2.619 | 1.23% | 8 |
| Quant + Critic | 4.81% | 2.198 | 1.83% | 11 |
| Quant only | 4.42% | 1.967 | 2.05% | 13 |
| 移除风控 | 11.75% | 2.682 | 2.52% | 35 |
| SMA Cross | 26.86% | 2.338 | 6.69% | 6 |
| Buy and Hold | 26.05% | 1.856 | 13.23% | 1 |

这些结果不用于证明盈利能力。它们展示的是：完整智能体牺牲部分收益，显著降低了回撤和交易次数；简单趋势策略在该合成样例上收益更高，因此报告不会宣称智能体在所有数据上优于传统策略。

## API

启动服务：

```bash
make api
```

接口：

- `GET /health`
- `POST /backtest`
- `POST /experiments`
- Swagger：`/docs`

请求示例：

```json
{
  "csv_path": "data/sample/demo.csv",
  "news_path": "data/sample/demo_news.jsonl",
  "symbol": "DEMO",
  "config_path": "config/default.yaml"
}
```

API 只允许读取项目根目录中的文件，阻止 `../` 路径逃逸。

## Docker

```bash
docker compose up --build
```

服务默认监听 `8000` 端口。容器仅提供模拟回测 API，不包含真实券商接口。

## 数据格式

行情 CSV：

```text
timestamp,open_at,open,high,low,close,volume,available_at
```

- `timestamp`：该根 K 线收盘时间；
- `open_at`：该根 K 线开盘时间；
- `available_at`：完整 K 线实际可被策略使用的时间，不能早于收盘。

新闻 JSONL：

```json
{"event_id":"evt-001","symbol":"DEMO","published_at":"2025-01-02T12:00:00","available_at":"2025-01-02T12:01:00","headline":"...","summary":"...","source":"..."}
```

## 目录

```text
CourseTradingAgents/
├── config/default.yaml
├── data/sample/
├── docs/
├── src/tradinglab_agents/
│   ├── agents/
│   ├── api/
│   ├── broker/
│   ├── data/
│   ├── engine/
│   ├── evaluation/
│   └── risk/
├── tests/
├── scripts/
├── artifacts/
├── Dockerfile
├── docker-compose.yml
└── Makefile
```

## 文档

- `docs/00_REFERENCE_ANALYSIS.md`：参考项目拆解和裁剪依据。
- `docs/01_SYSTEM_DESIGN.md`：系统设计和原创改进。
- `docs/02_IMPLEMENTATION_ROADMAP.md`：实施路线与验收标准。
- `docs/03_SERVER_NOTES.md`：服务器环境和网络约束。
- `docs/04_REFERENCE_CODE_REVIEW.md`：对用户上传源码的实际核对。
- `docs/05_EXPERIMENT_REPORT.md`：基线与消融实验分析。
- `docs/06_DEPLOYMENT.md`：CLI、API 和 Docker 部署说明。

## 参考

- 参考仓库：TauricResearch/TradingAgents
- 论文：TradingAgents: Multi-Agents LLM Financial Trading Framework
