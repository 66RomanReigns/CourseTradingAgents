# TradeLab-Agent

一个面向持续模拟盘目标、可复现且可审计的多智能体交易研究系统。

当前版本：**v0.18.0（可恢复 Provider Graph + 自动 Fallback + 数据质量门控）**。

本项目参考 TauricResearch/TradingAgents 的“多角色协作分析”目标，但没有复制其图拓扑或角色实现。v0.18.0 在 v0.17 正式市场语义之上加入独立 `<run-id>:DATA_PROVIDER` LangGraph：每个行情、新闻、宏观和基本面资源动态并行路由，经能力注册、持久健康状态、请求前额度门控、主备 Fallback、多源冲突解析和确定性质量评分后，才允许原子写入本地数据。降级或缓存数据只能维持或降低风险，任何研究 BUY 都会被强制转为 HOLD；严重冲突、Schema 错误或点时违规在写盘和 Research 前 fail closed。真实 Provider 仍需显式确认和无凭据联网检查，本版本的发布测试没有消耗真实 API 额度。

> 本项目只用于课程研究、软件工程演示和模拟盘实验，不构成投资建议，也不连接真实资金账户。

## 1. 核心设计

```text
Twelve Data 行情 + Alpha Vantage 行情/新闻
FRED 宏观 + SEC EDGAR 基本面 + 本地点时缓存
            │
            ▼
      DATA_PROVIDER LangGraph
 Capability Registry → Health / Quota Preflight
 Dynamic Resource Fan-out → Primary / Fallback / Cache
 Cross-source Conflict Resolver → Data Quality Gate
            │
            ├─ BLOCKED：写盘前停止
            ├─ DEGRADED：允许解释，禁止扩大仓位
            └─ NORMAL：进入正式数据层
            │
            ▼
点时缓存与统一 Data Provider
            │
            ▼
XNYS Calendar + Corporate Actions
节假日 / DST / 提前收盘 / 拆股 / 现金分红
            │
            ├─ 原始价格 → 估值与成交
            └─ 总回报复权历史 → Quant / Regime
            │
            ▼
同步 MarketSnapshot（缺价或错位即失败）
            │
            ▼
Feature Engine
            │
            ▼
      EvidencePack
            │
            ▼
 LangChain Prompt / Runnable / Pydantic
            │
            ▼
      Workflow Core LangGraph
 Data Validation → Research Parent
            │
            ▼
 Candidate Screening → Dynamic Send
   ┌───────────┼───────────┐
   ▼           ▼           ▼
 SPY 子图     QQQ 子图     Top-K 子图       （动态并行）
   │           │           │
   └───────────┼───────────┘
               ▼
每个单标的 Research LangGraph
   ┌────────┼────────┐
   ▼        ▼        ▼
 News     Macro   Fundamental       （并行）
 Analyst  Analyst    Analyst
   └────────┼────────┘
            ▼
 Bull / Bear Multi-round Loop       （条件循环）
            ▼
 Research Manager → Preliminary Trader
            │
   ┌────────┼────────┐
   ▼        ▼        ▼
Aggressive Balanced Conservative   （并行风险评审）
   └────────┼────────┘
            ▼
 Portfolio Manager → Human Gate
            │
            └────────────── fan-in ──────────────┐
                                                 ▼
   Correlation Reviewer              Concentration Reviewer
            └──────────────────┬─────────────────┘
                               ▼
                    Portfolio Supervisor
                               ▼
                 Deterministic Non-expansion Guard
                               ▼
       Overlay Assembly → Decision Preparation
        action/order/approval/position checks
          canonical SHA-256 handoff hash
                               ▼
       Deterministic Decision LangGraph
   ┌─────────────── per-symbol subgraph ───────────────┐
   Evidence → Quant → Fusion/Critic ─┐                 │
   Evidence → Regime Guard ──────────┴→ Overlay Target │
   └────────────────── dynamic fan-in ─────────────────┘
                               ▼
 PortfolioRiskGovernor → Plan SHA-256
                               ▼
开盘前幂等 Corporate Action Ledger
拆股数量 / 现金替代 / 现金分红
                               ▼
单标的 / 组合 Risk State Machine
            │
            ▼
多资产 Paper Broker（先卖后买）
            │
            ▼
节点级 Checkpoint、决策结果归因、单/多标的回测、持久模拟账本、HTML 报告与账户仪表板
```

### 智能体职责

- **Quant Signal Agent**：计算动量、趋势、波动率和成交量因子，输出可解释量化意见。
- **News / Macro / Fundamental Analysts**：分别使用独立输入结构、提示词和输出 Schema，不再把宏观与基本面伪装成新闻文本。
- **Bull / Bear Researchers**：基于分析师结果构建相互对抗且可引用证据的多空观点。
- **Research Manager**：综合分歧、证据质量和不确定性，输出 `BUY/HOLD/SELL` 与不超过 20% 的目标权重。
- **Trader**：只生成下一交易日开盘的模拟订单意图，方向性操作始终要求人工审批，不连接真实资金。
- **Correlation Reviewer**：使用同步收盘价构造点时相关性、波动率与高相关标的对，只能提出减仓上限。
- **Concentration Reviewer**：跨候选检查目标仓位和集中度，只能维持、降低或否决原建议。
- **Portfolio Supervisor**：综合两个组合评审输出跨标的配置；最终结果还必须经过确定性 non-expansion guard 和原有 `PortfolioRiskGovernor`。
- **Context Agent**：保留给原回测链路的兼容接口，但内部已按新闻、宏观和基本面分流。
- **Critic Agent**：检查证据引用、信号冲突和高波动风险，将不可靠意见降级为 HOLD。
- **Regime Guard Agent**：根据仅使用历史数据识别牛市、熊市、震荡、高波动或过渡状态，只限制风险暴露，不主动制造买入信号。
- **Risk Governor**：使用 `ACTIVE → LIQUIDATING → HALTED` 粘性状态机执行仓位上限、最低置信度和最大回撤硬约束；组合版本进一步限制单标的权重、持仓数量和总暴露，所有保护性减仓均绕过策略阈值与冷却期。
- **MarketSnapshot / Portfolio Valuation**：组合估值必须提供全部非零持仓价格，缺少任何标的都会立即失败，不再按零估值。
- **Multi-Asset Paper Broker**：在同一个同步开盘快照上先执行减仓、再执行加仓，复用释放现金并禁止负现金。

## 2. 与参考项目的关键区别

| 方面 | 参考项目 | TradeLab-Agent |
|---|---|---|
| 智能体编排 | 多角色动态图与多轮讨论 | 双层原创 LangGraph：单标的并行/循环研究 + 跨标的 Portfolio Supervisor 与确定性 Guard |
| LLM 依赖 | 主流程强依赖外部模型 | 默认 GLM dry-run，不联网即可执行完整结构化链；live 需显式确认 |
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

创建并同步锁定的 Python 3.11 环境（默认使用 HTTPS 南京大学 PyPI 镜像，可通过 `PYPI_INDEX` 覆盖）：

```bash
make lock   # 依赖变化时重新生成 requirements.lock
make sync
```

环境诊断：

```bash
make doctor
```

运行全部测试和静态检查：

```bash
make check
```

仅运行全部测试：

```bash
make test
```

运行单次回测：

```bash
make backtest
```

运行 P2 五标的同步组合回测（离线合成数据，2018—2025）：

```bash
make multi-backtest
```

结果写入 `artifacts/multi_backtest.json`。数据由 `scripts/generate_multi_asset_data.py` 确定性生成，不是真实历史行情。

运行基线与消融实验：

```bash
make experiment
```

运行牛市、熊市、震荡和高波动压力测试：

```bash
make benchmark
```

运行完整结构化研究链（默认 GLM-4.7-Flash dry-run，不联网）：

```bash
make research
```

结果写入 `artifacts/research.json`，调用缓存和审计日志分别写入 `artifacts/llm_cache/` 与 `artifacts/llm_calls.jsonl`。

配置外部数据密钥与检查免费额度预算：

```bash
make secrets-configure   # 静默输入 provider key，并生成本地 API 操作令牌
make secrets-token       # 已有 key 文件时只补生成 API 操作令牌
make secrets-check       # 只检查权限和变量名，不显示值
make api-budget          # 验证五标的调用计划没有超过免费额度
```

真实密钥永远不进入仓库；详细额度和调度方案见 `docs/13_API_QUOTA_AND_SECRET_PLAN.md`。

查看 Provider Graph 能力、健康与数据语义：

```bash
make provider-capabilities  # 查看主源、备用源、缓存、额度与节流组
make provider-health        # 查看持久健康、失败次数与冷却时间
make provider-events        # 查看 fallback、限流和冲突事件
make market-calendar        # 导出 XNYS 会话和提前收盘摘要
make market-validate        # 验证同步行情、节假日、提前收盘与公司行动
```

运行统一工作流：

```bash
make workflow-plan      # 查看数据源、GLM 次数、日历和安全状态
make workflow-dry-run   # 完整执行本地研究链，外部请求 0，账户不变
make workflow-offline   # 本地数据 + Mock 研究，可推进内部模拟账户
```

Live 数据刷新首先进入 `<run-id>:DATA_PROVIDER`；成功资源不会因另一个资源失败而重复调用，Provider 注册表和质量阈值也进入输入 SHA-256。随后五标的先做 Quant 筛选，Research Parent 使用 LangGraph `Send` 动态派发 Top-K；默认 Top-2 各执行 13 次单标的研究，共 26 次，再执行 3 次组合监督，标准上限仍为 29 次。研究准备由 `<run-id>:WORKFLOW_CORE` 包装，内部包含 `<run-id>:RESEARCH_PARENT`、`<run-id>:<symbol>` 和 `<run-id>:PORTFOLIO`。Paper Session 在订单持久化前启动 `<daily-run-id>:DECISION`；账户与订单副作用仍在所有研究和数据图之外。

真实调用前先逐个冒烟：

```bash
scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli provider-smoke \
  --provider twelve_data --symbol AAPL --confirm-live

scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli provider-smoke \
  --provider zhipu --confirm-live
```

查看调用、健康和冲突状态：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-usage
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-health
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-events
```

Live 工作流默认使用 Provider Graph。联网前置检查未通过时，密钥不会发出；共享日额度在请求前查询持久账本，额度不足时外部 fetcher 不执行。Twelve Data 行情可依次回退到 Alpha Vantage 日线和本地缓存；缓存或其他 fallback 一律标记为 `DEGRADED` 并禁止新增风险。当前真实 Provider 尚未完成逐项 smoke test，发布验收全部为离线故障注入。

导出并查看真实 LangGraph 拓扑：

```bash
make data-provider-graph
make workflow-core-graph
make research-parent-graph
make research-graph
make portfolio-graph
make decision-graph
# 输出：artifacts/data_provider_graph.mmd
# 输出：artifacts/workflow_core_graph.mmd
# 输出：artifacts/research_parent_graph.mmd
# 输出：artifacts/research_graph.mmd
# 输出：artifacts/portfolio_supervisor_graph.mmd
# 输出：artifacts/decision_graph.mmd
```

查看某个持久研究线程：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli   research-thread-status '<workflow-run-id>:WORKFLOW_CORE'
# 父图：research-thread-status '<workflow-run-id>:RESEARCH_PARENT'
# 单标的：research-thread-status '<workflow-run-id>:SPY'
# 组合图：research-thread-status '<workflow-run-id>:PORTFOLIO'
# 确定性决策：research-thread-status '<paper-daily-run-id>:DECISION'
```

研究层可选原生人工中断，不连接券商：

```bash
# 首次运行会在方向性结论处暂停并返回 thread_id
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research   --csv data/sample/demo.csv --news data/sample/demo_news.jsonl   --config config/default.yaml --human-review-mode interrupt_directional

# 之后可审批、拒绝或降低 BUY 仓位
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research   --csv data/sample/demo.csv --news data/sample/demo_news.jsonl   --config config/default.yaml --thread-id '<thread-id>'   --resume-decision reject
```

运行 P3 持久模拟盘闭环：

```bash
make paper-init       # 首次创建账户
make paper-next       # 收盘生成下一开盘订单
make paper-orders     # 查看审批队列
make paper-approve-all
make paper-next       # 下一交易日开盘执行已批准订单
make paper-account
make paper-dashboard
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-memories --account demo-paper
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-actions --account demo-paper
```

默认审批策略为 `ALL`。未批准订单错过计划开盘后自动过期；所有账户、订单和成交仅写入 `artifacts/paper_trading.db`，项目没有真实券商连接。

查看已存档实验：

```bash
make runs
```

## 4. CLI

安装后可使用 `tradinglab`；源码模式统一通过项目 `.venv` 执行，避免系统 Python 漂移。

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli experiment \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml
```

多资产回测：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli multi-backtest \
  --data-dir data/multi_sample \
  --symbols SPY QQQ AAPL MSFT NVDA \
  --config config/default.yaml
```

持久模拟盘命令：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-init \
  --account demo-paper --name "TradeLab Demo Paper" \
  --symbols SPY QQQ AAPL MSFT NVDA --config config/default.yaml

PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-next \
  --account demo-paper --config config/default.yaml

PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-approve-all \
  --account demo-paper --reviewer local-user --config config/default.yaml
```

订单还可以通过 `paper-approve`、`paper-reject` 和 `paper-cancel` 单独处理。`paper-run --session YYYY-MM-DD` 可运行指定离线交易日；API、CLI 和调度器使用同一个按账户锁。`paper-memories` 可查看原始决策与收益归因，`paper-actions` 可查看已幂等入账的拆股、现金替代和分红事件。

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

### 4.1 GLM 执行模式

默认配置已经选择：

```yaml
llm:
  execution_mode: dry_run
  provider: zhipu
  model: glm-4.7-flash
```

`dry_run` 会执行完整 13 节点研究图、Schema 校验、分层缓存和审计，但由确定性 Mock 生成结果，不读取密钥、不访问网络。正式联调前先轮换所有曾在聊天中出现的密钥，再通过 `make secrets-configure` 静默写入项目外文件。

真实调用入口：

```bash
scripts/with_api_keys.sh \
  env PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli workflow-run \
  --mode live --confirm-live --config config/default.yaml
```

任何模型输出都必须通过 Pydantic JSON Schema 和 Evidence ID 校验。GLM 只能作为保守覆盖层：可以 veto 加仓或触发减仓，但不能绕过量化确认、组合风控和人工审批。

## 5. FastAPI 演示

启动：

```bash
make api
```

除 `/`、`/health` 和 API 文档外，所有接口都要求 `X-API-Key` 或 Bearer Token；令牌保存在 `~/.config/tradinglab/tradinglab.keys` 的 `TRADINGLAB_API_TOKEN` 中，不得放进 URL 或日志。

常用接口：

- `GET /health`：环境和服务状态；
- `GET /workflow/plan`：查看零调用工作流计划；
- `GET /workflow/core-graph`：返回 WORKFLOW_CORE Mermaid、checkpoint 和图外安全边界；
- `GET /workflow/decision-graph`：返回确定性 Quant/Fusion/Regime/PortfolioRisk 图、输入/计划哈希和无副作用边界；
- `POST /workflow/dry-run`：执行或恢复无网络、无账户修改的完整工作流；
- `GET /workflow/runs/{run_id}`：查看节点级 checkpoint 状态；
- `GET /providers/usage`：查看供应商调用状态、额度单位、缓存命中和耗时；
- `GET /providers/capabilities`：查看主备顺序、权威度、新鲜度、节流组和应用额度；
- `GET /providers/health`：查看持久健康状态、失败次数和冷却时间；
- `GET /providers/events`、`GET /providers/conflicts`：查看 bounded fallback/冲突事件；
- `GET /market/data-quality`：查看质量阈值和 DATA_PROVIDER thread 摘要；
- `GET /workflow/data-provider-graph`：查看 Provider Graph Mermaid 与安全边界；
- `GET /market/calendar`：查看 XNYS 会话、节假日和提前收盘；
- `GET /market/semantics`：验证本地同步行情、复权模式和公司行动文件；
- `POST /backtest`：单次回测；
- `GET /research/graph`：返回单标的 LangGraph Mermaid 拓扑与并行/路由元数据；
- `GET /research/parent-graph`：返回动态 Top-K Research Parent 图；
- `GET /research/portfolio-graph`：返回跨标的 Portfolio Supervisor 图与 non-expansion 安全元数据；
- `GET /research/threads/{thread_id}`：查看持久线程 checkpoint、执行路径和人工审阅状态；
- `POST /research`：运行 LangChain/LangGraph 研究图，支持可选 interrupt/resume，只返回模拟意图；
- `POST /paper/accounts`：创建内部持久模拟账户；
- `GET /paper/accounts/{account_id}`：查看现金、持仓、订单、成交和净值；
- `GET /paper/accounts/{account_id}/dashboard`：查看本地 HTML 仪表板；
- `GET /paper/accounts/{account_id}/orders`：查看订单队列；
- `GET /paper/accounts/{account_id}/memories`：查看决策与成熟结果归因；
- `GET /paper/accounts/{account_id}/corporate-actions`：查看已幂等入账的拆股与现金分红；
- `POST /paper/accounts/{account_id}/sessions`：运行指定或下一同步交易日；
- `POST /paper/orders/{order_id}/approve|reject|cancel`：管理人工审批状态；
- `POST /experiments`：基线和消融实验；
- `GET /runs`：历史实验列表；
- `GET /runs/{run_id}`：完整实验结果；
- `GET /reports/latest`：最近一次 HTML 报告；
- `GET /docs`：Swagger 文档。

持久化实验示例：

```bash
scripts/with_api_keys.sh bash -c '
  curl --noproxy "*" -X POST http://127.0.0.1:8000/experiments \
    -H "X-API-Key: $TRADINGLAB_API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"csv_path\":\"data/sample/demo.csv\",\"news_path\":\"data/sample/demo_news.jsonl\",\"symbol\":\"DEMO\",\"config_path\":\"config/default.yaml\",\"persist\":true}"
'
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
5. 多资产组合只在所有标的都存在的公共时间戳上决策和成交；
6. 组合估值必须包含每个非零持仓的有效正价格；
7. 输入文件、配置、Git 版本和 Python 环境写入 `manifest.json`；
8. 工作流节点通过原子 JSON checkpoint 保存，恢复时强制校验配置哈希；
9. 原始决策记忆不可变，收益归因和反思只能追加到结果字段。

## 7. 当前实验结论

在主合成数据上，完整智能体相较买入持有降低了收益，但显著降低了最大回撤。四场景压力测试中，完整智能体在熊市和高波动场景控制损失明显优于买入持有。

加入 Regime Guard 后：

- 四场景最差回撤从移除该模块时的约 **4.29%** 降至约 **2.60%**；
- 震荡场景收益从约 **-3.03%** 改善到约 **-1.13%**；
- 熊市场景仅损失约 **0.70%**，而买入持有约损失 **40.71%**；
- 牛市场景因仓位受限，收益明显低于买入持有，体现了低风险暴露的代价。

这些结果只说明系统逻辑与风险权衡在确定性场景中可验证，不证明真实市场盈利能力。详细结果见 `docs/05_EXPERIMENT_REPORT.md`。

P2 的五标的离线组合验收使用 SPY、QQQ、AAPL、MSFT、NVDA 的合成数据覆盖 2018-01-02 至 2025-12-31，共 2087 个同步交易日。当前固定配置下，成交后最大总暴露约为 89.83%，最大单标的成交后权重约为 20.00%，负现金次数为 0；回撤触发后组合完整进入 `LIQUIDATING → HALTED`。这些数字用于验证软件约束，不代表真实策略表现。

P3 CLI 闭环已验证：首日生成 5 个待审批订单，批量批准后下一同步开盘成交 5 笔；重新创建服务实例后现金和五标的持仓完整恢复，同一交易日重复运行不产生重复成交。另有故障注入测试验证：开盘成交已提交但收盘规划前崩溃时，重启从 `OPEN_EXECUTED` 阶段继续。

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
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli experiment \
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
├── data/sample/            # 单标的主示例数据
├── data/multi_sample/      # 可重建的五标的离线合成数据
├── data/scenarios/         # 四种确定性市场场景
├── docs/                   # 设计、实验、部署和答辩材料
├── scripts/                # 数据生成、转换和环境诊断
├── src/tradinglab_agents/
│   ├── agents/             # Quant / Context / Critic / Regime
│   ├── api/                # FastAPI
│   ├── broker/             # 模拟成交
│   ├── data/               # 点时数据 Provider
│   ├── engine/             # 特征、同步市场快照、单/多标的回测编排
│   ├── evaluation/         # 指标、基线、消融、审计和压力测试
│   ├── reporting/          # Manifest 与 HTML 报告
│   ├── risk/               # 单标的与组合级确定性风控
│   ├── storage/            # SQLite 运行存档
│   └── workflows/          # 数据、GLM 研究和模拟盘统一编排
├── tests/
├── compose.yaml            # API、dry-run 与显式 live profiles
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
- `docs/08_COURSE_DEMO.md`：课程答辩演示流程；
- `docs/09_FREE_DATA_APIS.md`：免费真实数据接口；
- `docs/10_P0_P1_IMPLEMENTATION.md`：P0/P1 实施、验收与后续边界；
- `docs/11_P2_MULTI_ASSET_IMPLEMENTATION.md`：P2 多资产设计、验收与 P3 边界。
- `docs/12_P3_PAPER_TRADING_IMPLEMENTATION.md`：P3 持久模拟盘、审批、恢复、调度和 API。
- `docs/13_API_QUOTA_AND_SECRET_PLAN.md`：API 密钥保存、免费额度、刷新日程和降级策略。
- `docs/14_GLM_WORKFLOW_AND_PROJECT_REVIEW.md`：GLM 工作流、权限边界和全项目改进审查；
- `docs/15_V09_RUNTIME_MEMORY_SECURITY.md`：节点恢复、决策记忆、数据库迁移、API 认证和统一账户锁；
- `docs/16_V10_LIVE_PROVIDER_RUNTIME.md`：校园网门户检测、TLS 安全、供应商账本、冒烟测试和显式降级；
- `docs/17_V11_TIERED_RESEARCH_GRAPH.md`：quick/deep 分层研究图、多轮辩论、风险委员会和点时记忆反馈；
- `docs/18_V12_LANGCHAIN_LANGGRAPH_RUNTIME.md`：LangChain Runnable、原生 LangGraph 并行/循环/恢复/中断与双层审计；
- `docs/19_V13_PORTFOLIO_SUPERVISOR_GRAPH.md`：跨标的相关性/集中度评审、组合监督、non-expansion guard 与 PORTFOLIO thread；
- `docs/20_V14_RESEARCH_PARENT_GRAPH.md`：动态 Send fan-out、顶层 RESEARCH_PARENT thread、父级恢复与分阶段子图迁移；
- `docs/21_V15_WORKFLOW_CORE_GRAPH.md`：数据验证、研究父图、Overlay 组装、Paper 输入安全契约与 WORKFLOW_CORE thread；
- `docs/22_V16_DETERMINISTIC_DECISION_GRAPH.md`：确定性子图、PortfolioRiskGovernor、输入/计划哈希、DECISION thread 与 Paper 交接；
- `docs/23_V17_MARKET_CALENDAR_AND_CORPORATE_ACTIONS.md`：XNYS 会话、提前收盘、同步校验、拆股/分红复权与 Paper 公司行动账本；
- `docs/24_V18_PROVIDER_GRAPH_AND_DATA_QUALITY.md`：能力注册、健康状态、Fallback、冲突解析、质量门控、额度预检与 DATA_PROVIDER 恢复；
- `CODEX_HANDOFF.md`：本地环境、真实 API 验证顺序、安全边界和验收标准；
- `CODEX_TASK_PROMPT.md`：可直接复制给本地 Codex 的执行提示词。

## 11. 参考与声明

- 参考仓库：TauricResearch/TradingAgents；
- 参考论文：TradingAgents: Multi-Agents LLM Financial Trading Framework；
- 详细原创性边界见 `NOTICE.md`。
