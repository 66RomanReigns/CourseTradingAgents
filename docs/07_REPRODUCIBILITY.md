# 可复现性与审计说明

## 1. 目标

TradeLab-Agent 将“能再次运行得到相同逻辑结果”作为课程项目的核心验收项。系统不依赖实时行情、付费新闻接口或随机 LLM 输出即可完成完整演示。

## 2. 固定输入

默认输入：

```text
data/sample/demo.csv
data/sample/demo_news.jsonl
config/default.yaml
```

压力测试输入：

```text
data/scenarios/bull.csv
data/scenarios/bear.csv
data/scenarios/sideways.csv
data/scenarios/volatile.csv
```

所有样例由固定随机种子生成。重新执行：

```bash
make sample
make scenarios
```

会得到相同的数据序列。

## 3. Run Manifest

每次 `make experiment` 都会生成 `manifest.json`，记录：

- Run ID；
- UTC 创建时间；
- 输入文件绝对/相对路径；
- 输入文件字节数和 SHA-256；
- 完整配置和配置哈希；
- Git commit、分支和 dirty 状态；
- Python 版本、可执行文件和操作系统。

Run ID 由时间戳和内容哈希前缀组成，防止实验目录覆盖。

## 4. 运行包

```text
artifacts/runs/<run_id>/
├── experiment.json   # 完整原始结果
├── report.md         # 便于课程报告引用
├── report.html       # 可直接浏览的可视化页面
├── manifest.json     # 输入与环境追踪
└── audit.json        # 自动审计结果
```

`artifacts/latest/` 始终保存最近一次运行包的副本，`artifacts/LATEST_RUN.txt` 记录最近 Run ID。

## 5. SQLite 实验索引

正式实验写入：

```text
artifacts/tradinglab.db
```

数据库包含：

- 运行元数据；
- Git 与内容哈希；
- 审计是否通过；
- 每个变体的收益、Sharpe、回撤、换手、成交数和费用；
- 完整 JSON 结果。

查看历史：

```bash
make runs
```

查看单次结果：

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli show-run <run_id>
```

## 6. 时间安全

### 行情

- `open_at`：开盘时间；
- `timestamp`：收盘时间；
- `available_at`：该日完整行情可见时间。

策略在 `decision_time` 只能读取 `available_at <= decision_time` 的记录。

### 新闻

新闻 JSONL 中同时保存 `published_at` 和 `available_at`。回测时只使用已到达系统的新闻，不以文章日期替代实际可见时间。

### 成交

当前收盘数据形成信号后，只允许在下一根 K 线的 `open_at` 成交。

## 7. Evidence Audit

每次决策同时记录：

- `evidence_ids`：实际引用证据；
- `available_evidence_ids`：当时可用证据全集；
- `evidence_count`：证据数量；
- Quant、Context、Fusion 的分数和置信度；
- Critic 与 Regime Guard 的解释；
- Risk Governor 的修改原因。

自动审计检查：

1. 是否引用不存在的 Evidence ID；
2. 行情是否在决策后才可见；
3. 成交是否早于或等于决策时间；
4. 置信度是否在 `[0, 1]`；
5. 目标仓位是否在 `[0, 1]`。

## 8. 环境诊断

```bash
make doctor
```

诊断项目包括：

- Python 版本；
- PyYAML、FastAPI、Uvicorn；
- 默认配置合法性；
- 样例行情与新闻数量；
- artifacts 写权限；
- Docker daemon 状态。

Docker 当前代理错误只作为 warning，不阻止本地 CLI/API 验收。

## 9. 推荐复现实验命令

```bash
make doctor
make test
make experiment
make benchmark
make runs
```

正式提交前再执行：

```bash
git diff --check
```
