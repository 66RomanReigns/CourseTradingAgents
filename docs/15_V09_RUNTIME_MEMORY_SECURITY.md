# v0.9.0 节点运行时、决策记忆与安全运维

日期：2026-07-20  
版本：v0.9.0

## 1. 目标

v0.9.0 的目标不是复制 TradingAgents 的 LangGraph 代码，而是补齐同类成熟项目最关键的运行能力，并保留 TradeLab-Agent 的原创约束：

- 外部模型只能提供研究覆盖，不能绕过确定性组合风控；
- 所有真实数据必须先落盘并经过点时约束；
- 模拟订单仍需要审批并在下一同步开盘执行；
- 默认模式仍为 `dry_run`，不会读取 provider key 或发起网络请求；
- 工作流恢复、记忆归因和 API 运维必须可测试、可审计。

## 2. 节点级工作流恢复

每次工作流运行目录：

```text
artifacts/workflows/<run_id>/
├── plan.json
├── result.json
├── state.json
└── nodes/
    ├── preflight.json
    ├── validate_local_data.json
    ├── candidate_screen.json
    ├── research.AAPL.news_analyst.json
    ├── research.AAPL.macro_analyst.json
    ├── research.AAPL.fundamental_analyst.json
    ├── research.AAPL.bull_researcher.json
    ├── research.AAPL.bear_researcher.json
    ├── research.AAPL.research_manager.json
    ├── research.AAPL.trader.json
    └── paper_session.json
```

Live 模式的数据供应商也按独立节点保存：

```text
refresh_market
refresh_news
refresh_macro
refresh_fundamentals
```

节点文件和 `state.json` 都使用临时文件加原子重命名写入，避免进程退出时留下半个 JSON 文件。

### 恢复命令

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli workflow-run \
  --mode dry_run \
  --run-id <run_id> \
  --resume \
  --config config/default.yaml
```

查看状态：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli workflow-status <run_id>
```

恢复约束：

1. 必须显式提供原始 `run_id`；
2. 工作流模式必须一致；
3. 完整 `BacktestSettings` 的 SHA-256 必须一致；
4. checkpoint schema 必须受当前版本支持；
5. 已完成节点直接从本地产物恢复，不再次调用 Agent；
6. 失败节点及之后的节点才重新运行。

故障注入测试证明：Macro Analyst 失败后，恢复过程不会重新执行已经完成的 News Analyst。

## 3. SQLite 迁移

模拟盘数据库开始使用：

```sql
PRAGMA user_version;
```

当前 schema version：

```text
2
```

迁移规则：

- 旧数据库 `user_version=0` 被识别为历史 v1 结构；
- 自动创建 v2 决策记忆表和索引；
- 更新到 `user_version=2`；
- 数据库版本高于程序支持版本时立即拒绝启动；
- 不允许用旧代码静默打开未来数据库。

新增表：

```text
paper_decision_memories
```

原有账户、持仓、订单、成交、日运行和权益表保持不变。

## 4. 决策记忆

每次收盘组合规划会为全部标的保存一条不可变决策事实，包括：

- `account_id`；
- `run_id`；
- 标的与决策时间；
- 最终动作；
- 置信度；
- 当前权重；
- 风控前目标权重；
- 风控后批准目标权重；
- Evidence ID；
- Quant、Fusion、Critic、Regime 和 Research Overlay 摘要；
- 基准标的；
- 结果观察期。

原始决策字段不会在结果成熟时被重写。

### 自动结果成熟

默认观察期为五个同步交易日。到达观察期后，系统仅使用本地同步收盘价计算：

```text
raw_return
benchmark_return
alpha_return
```

并生成确定性失败类型：

```text
UNDERPERFORMED_BENCHMARK
MISSED_UPSIDE
MISSED_MATERIAL_MOVE
```

当前反思由确定性规则生成，并明确记录：

```json
{
  "generator": "deterministic_outcome_attribution_v1",
  "llm_generated": false,
  "source_memory_immutable": true
}
```

以后可以增加 GLM 反思节点，但只能读取不可变事实并追加结构化反思，不能修改历史决策或收益。

查看记忆：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli paper-memories \
  --account demo-paper \
  --outcome-status MATURED
```

## 5. API 认证

公共路径只有：

```text
/
/health
/openapi.json
/docs
/redoc
```

其他接口都需要：

```text
X-API-Key: <TRADINGLAB_API_TOKEN>
```

或：

```text
Authorization: Bearer <TRADINGLAB_API_TOKEN>
```

令牌保存于：

```text
~/.config/tradinglab/tradinglab.keys
```

生成方式：

```bash
make secrets-token
```

认证实现使用常量时间比较，不会把令牌写入响应、SQLite、缓存或调用日志。服务器没有配置令牌时，非公共接口返回 `503`，而不是降级为匿名访问。

启动 API：

```bash
make api
```

`make api` 会通过 `scripts/with_api_keys.sh` 加载外部 chmod-600 文件，并继续只绑定：

```text
127.0.0.1:8000
```

## 6. 统一账户锁

以前只有 `paper-next` 使用单个调度锁，API 的显式 session 入口可能绕过该锁。v0.9.0 使用按账户派生的锁文件：

```text
artifacts/paper_scheduler.<account_id>.lock
```

以下入口共用同一锁：

- CLI `paper-run`；
- CLI `paper-next`；
- FastAPI paper session；
- DailyWorkflow paper session；
- 后续容器调度器。

账户 ID 会被安全规范化，避免路径注入。不同账户可以并行，同一账户不能并发推进。

## 7. 密钥边界

当前密钥文件已经完成以下检查：

- 文件存在；
- 所有 provider 变量已配置；
- `TRADINGLAB_API_TOKEN` 已配置；
- 权限为 `600`；
- 所有值均未输出；
- 仓库密钥扫描不读取该外部文件。

本轮没有发起 Twelve Data、Alpha Vantage、FRED、Google 或智谱 API 请求。

## 8. 验收结果

```text
70 / 70 tests passed
compileall passed
Ruff passed
```

新增覆盖包括：

- API 正确令牌、错误令牌和缺少服务器令牌；
- v1 数据库自动迁移到 v2；
- 未来 schema 拒绝打开；
- 决策记忆保存和五日结果成熟；
- Agent 故障后按节点恢复；
- 已完成 Agent 不重复执行；
- 不同账户锁隔离和账户名安全规范化。

## 9. 仍未完成的事项

v0.9.0 仍没有执行真实 provider 联调。后续真实调用应按以下顺序进行：

1. Twelve Data 单标的最小请求；
2. Alpha Vantage 单标的新闻请求；
3. FRED 单序列请求；
4. SEC EDGAR 用户代理验证；
5. GLM 固定短结构化响应；
6. 单候选完整七节点研究；
7. 五标的完整工作流。

每一步都应先检查缓存、额度日志和输出 Schema，再进入下一步。
