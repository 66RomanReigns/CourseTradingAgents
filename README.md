# TradeLab-Agent

一个面向课程作业的、可复现且可审计的多智能体模拟交易系统。

当前版本：**v0.4.0**。

本项目参考 TauricResearch/TradingAgents 的“多角色协作分析”思想，但没有复制其十余个 LLM 角色、复杂 LangGraph 拓扑和多种在线数据接口。项目重新设计为一个更适合课程验收的混合系统：语言模型风格模块只处理非结构化上下文，行情计算、仓位控制、成交模拟与审计全部由确定性程序完成。

> 本项目只用于课程研究、软件工程演示和模拟盘实验，不构成投资建议，也不连接真实资金账户。

## 1. 核心设计

```text
Twelve Data 行情 + Alpha Vantage 新闻
FRED 宏观 + SEC EDGAR 基本面
            │
            ▼
点时缓存与统一 Data Provider
            │
            ▼
Feature Engine
            │
            ▼
      EvidencePack
            │
     ┌──────┴──────┐
     ▼             ▼
Quant Agent   Context Agent
     └──────┬──────┘
            ▼
      Decision Fusion
            ▼
       Critic Agent
            ▼
    Regime Guard Agent
            ▼
      Risk Governor
            ▼
       Paper Broker
            ▼
回测、审计、SQLite、HTML 报告
```

### 智能体职责

- **Quant Signal Agent**：计算动量、趋势、波动率和成交量因子，输出可解释量化意见。
- **Context Agent**：通过可替换的结构化推理接口分析点时新闻、宏观和基本面证据。默认使用确定性 `MockLLM`，无 LLM API 也能复现。
- **Critic Agent**：检查证据引用、信号冲突和高波动风险，将不可靠意见降级为 HOLD。
- **Regime Guard Agent**：根据仅使用历史数据识别牛市、熊市、震荡、高波动或过渡状态，只限制风险暴露，不主动制造买入信号。
- **Risk Governor**：执行仓位上限、最低置信度和最大回撤等硬约束。

## 2. 与参考项目的关键区别

| 方面 | 参考项目 | TradeLab-Agent |
|---|---|---|
| 智能体数量 | 十余个分析、辩论、风险角色 | 4 个职责明确的分析/约束模块 |
| LLM 依赖 | 主流程强依赖外部模型 | 默认离线 MockLLM，可替换但不强依赖 |
| 数据源 | 多个在线行情、新闻、社区和宏观接口 | Twelve Data、Alpha Vantage、FRED、SEC EDGAR，经统一缓存后转为本地点时数据 |
| 风控 | 多角色语言讨论 | 确定性硬约束 |
| 执行 | 侧重投资结论 | 完整目标仓位、手续费、滑点和持仓账本 |
| 复现 | 受 API、模型和网络影响 | 固定数据、配置、哈希、运行清单和 SQLite |
| 实验 | 主要比较最终收益 | 基线、模块消融、市场状态和四场景压力测试 |

## 3. 快速开始

进入项目：

```bash
cd /home/amax/mcp-workspace/projects/trading-agent-course/CourseTradingAgents
```

环境诊断：

```bash
make doctor
```

运行全部测试：

```bash
make test
```

运行单次回测：

```bash
make backtest
```

运行基线与消融实验：

```bash
make experiment
```

运行牛市、熊市、震荡和高波动压力测试：

```bash
make benchmark
```

查看已存档实验：

```bash
make runs
```

## 4. CLI

安装后可使用 `tradinglab`；未安装时可通过 `PYTHONPATH=src python3 -m tradinglab_agents.cli` 执行。

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli experiment \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml
```

每次正式实验会生成：

```text
artifacts/runs/<run_id>/
├── experiment.json
├── report.md
├── report.html
├── manifest.json
└── audit.json
```

并将结果写入：

```text
artifacts/tradinglab.db
```

## 5. FastAPI 演示

启动：

```bash
make api
```

常用接口：

- `GET /health`：环境和服务状态；
- `POST /backtest`：单次回测；
- `POST /experiments`：基线和消融实验；
- `GET /runs`：历史实验列表；
- `GET /runs/{run_id}`：完整实验结果；
- `GET /reports/latest`：最近一次 HTML 报告；
- `GET /docs`：Swagger 文档。

持久化实验示例：

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
    "csv_path": "data/sample/demo.csv",
    "news_path": "data/sample/demo_news.jsonl",
    "symbol": "DEMO",
    "config_path": "config/default.yaml",
    "persist": true
  }'
```

## 6. 时间安全与审计

系统明确区分：

- `timestamp`：行情收盘时间；
- `open_at`：下一根 K 线开盘时间；
- `available_at`：数据实际可被策略看到的时间。

约束包括：

1. 决策只能读取 `available_at <= decision_time` 的数据；
2. 当日收盘形成信号，最早下一交易日开盘成交；
3. 每条智能体意见必须引用当前 EvidencePack 中存在的 Evidence ID；
4. 自动审计检查时间顺序、置信度、目标仓位和证据覆盖率；
5. 输入文件、配置、Git 版本和 Python 环境写入 `manifest.json`。

## 7. 当前实验结论

在主合成数据上，完整智能体相较买入持有降低了收益，但显著降低了最大回撤。四场景压力测试中，完整智能体在熊市和高波动场景控制损失明显优于买入持有。

加入 Regime Guard 后：

- 四场景最差回撤从移除该模块时的约 **4.29%** 降至约 **2.60%**；
- 震荡场景收益从约 **-3.03%** 改善到约 **-1.13%**；
- 熊市场景仅损失约 **0.70%**，而买入持有约损失 **40.71%**；
- 牛市场景因仓位受限，收益明显低于买入持有，体现了低风险暴露的代价。

这些结果只说明系统逻辑与风险权衡在确定性场景中可验证，不证明真实市场盈利能力。详细结果见 `docs/05_EXPERIMENT_REPORT.md`。

## 8. 免费真实数据 API

项目已经原生接入：

- Twelve Data：日线 OHLCV；
- Alpha Vantage：新闻与 ticker 情绪；
- FRED：带首次 vintage 日期的宏观数据；
- SEC EDGAR：按 filing date 可见的 Company Facts 基本面。

下载示例：

```bash
export TWELVE_DATA_API_KEY="..."
PYTHONPATH=src python3 -m tradinglab_agents.cli fetch-market \
  --symbol SPY --start 2018-01-01 --output data/real/SPY.csv
```

真实证据回测：

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli experiment \
  --csv data/real/SPY.csv \
  --news data/real/SPY_news.jsonl \
  --evidence data/real/macro.jsonl \
  --evidence data/real/SPY_fundamentals.jsonl \
  --symbol SPY --config config/default.yaml
```

完整说明见 `docs/09_FREE_DATA_APIS.md`。原有 Yahoo Finance 风格 CSV 转换脚本仍保留在 `scripts/normalize_ohlcv_csv.py`。

## 9. 项目结构

```text
CourseTradingAgents/
├── config/                 # YAML 配置
├── data/sample/            # 主示例数据
├── data/scenarios/         # 四种确定性市场场景
├── docs/                   # 设计、实验、部署和答辩材料
├── scripts/                # 数据生成、转换和环境诊断
├── src/tradinglab_agents/
│   ├── agents/             # Quant / Context / Critic / Regime
│   ├── api/                # FastAPI
│   ├── broker/             # 模拟成交
│   ├── data/               # 点时数据 Provider
│   ├── engine/             # 特征、融合和回测编排
│   ├── evaluation/         # 指标、基线、消融、审计和压力测试
│   ├── reporting/          # Manifest 与 HTML 报告
│   ├── risk/               # 确定性风控
│   └── storage/            # SQLite 运行存档
├── tests/
├── Makefile
└── pyproject.toml
```

## 10. 文档索引

- `docs/00_REFERENCE_ANALYSIS.md`：参考项目拆解；
- `docs/01_SYSTEM_DESIGN.md`：系统架构；
- `docs/02_IMPLEMENTATION_ROADMAP.md`：实施路线；
- `docs/03_SERVER_NOTES.md`：服务器环境与限制；
- `docs/04_REFERENCE_CODE_REVIEW.md`：源码级核对；
- `docs/05_EXPERIMENT_REPORT.md`：实验结果；
- `docs/06_DEPLOYMENT.md`：CLI、API 与 Docker 配置；
- `docs/07_REPRODUCIBILITY.md`：可复现性和审计说明；
- `docs/08_COURSE_DEMO.md`：课程答辩演示流程。

## 11. 参考与声明

- 参考仓库：TauricResearch/TradingAgents；
- 参考论文：TradingAgents: Multi-Agents LLM Financial Trading Framework；
- 详细原创性边界见 `NOTICE.md`。
