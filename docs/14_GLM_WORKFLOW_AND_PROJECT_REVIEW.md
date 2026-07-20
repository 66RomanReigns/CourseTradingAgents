# GLM-4.7-Flash 工作流与项目审查

版本：v0.8.0  
日期：2026-07-20

## 1. 本轮定位

本轮没有发起任何真实外部 API 请求，也没有将用户提供的密钥写入仓库、日志、SQLite 或运行产物。

已完成的框架目标：

```text
外部数据计划
  → 本地缓存/增量合并
  → 全标的量化筛选
  → Top-2 GLM 结构化研究
  → 保守研究覆盖层
  → 组合级确定性风控
  → 人工审批订单
  → 下一同步开盘内部模拟成交
```

默认配置：

```text
provider: zhipu
model: glm-4.7-flash
execution_mode: dry_run
workflow.mode: dry_run
maximum LLM calls/workflow: 14
real broker: disabled
```

`dry_run` 会完整执行本地候选筛选、七角色研究 Schema、缓存和审计，但结果由确定性 Mock 产生，不访问网络，也不修改模拟账户。

## 2. GLM 接入方式

远程端点：

```text
https://open.bigmodel.cn/api/paas/v4
```

模型：

```text
glm-4.7-flash
```

认证变量：

```text
ZHIPU_API_KEY
ZHIPU_BASE_URL
ZHIPU_MODEL
```

只有以下条件同时满足时才允许真实请求：

1. 工作流模式为 `live`；
2. LLM execution mode 被工作流切换为 `live`；
3. CLI 包含 `--confirm-live`；
4. `ZHIPU_API_KEY` 已在进程环境中；
5. 计划调用数没有超过 `llm.max_calls_per_run`。

管理 API 只开放 `workflow-plan` 和 `workflow/dry-run`，不提供远程 live 开关。

## 3. 研究调用预算

每个候选标的完整研究链包含：

1. News Analyst；
2. Macro Analyst；
3. Fundamental Analyst；
4. Bull Researcher；
5. Bear Researcher；
6. Research Manager；
7. Trader。

当前五标的先执行本地 Quant 筛选，仅研究优先级最高的两个标的：

```text
2 candidates × 7 calls = 14 planned GLM calls
```

该值同时是当前单工作流硬上限。若增加候选数量，配置加载或计划阶段会先失败，而不是在运行中超额调用。

## 4. GLM 对组合决策的权限边界

模型输出不会直接提交订单。

研究覆盖规则：

- `SELL`：可以把该标的目标降到零，属于保护性减仓；
- `HOLD` 或低置信度：阻止确定性策略增加风险暴露；
- `BUY`：只有 Quant 同样为 BUY 时，才与确定性目标按 50/50 混合；
- Quant 未确认 BUY 时，模型不能独立建立新仓位；
- 最终目标仍需经过最大单仓、总暴露、持仓数量和组合回撤风控；
- 方向性订单仍需按照账户审批策略进入人工队列。

因此权限顺序为：

```text
GLM research < deterministic portfolio risk < human approval < paper broker
```

## 5. 三种执行模式

### dry_run

- 外部请求：0；
- 使用本地合成数据；
- 执行完整结构化研究链；
- 不修改模拟账户；
- 生成 `plan.json` 和 `result.json`。

### offline

- 外部请求：0；
- 使用本地数据；
- 使用确定性 Mock 模型；
- 可推进内部模拟账户；
- 适合集成测试和演示。

### live

- 刷新 Twelve Data、Alpha Vantage、FRED、SEC；
- 运行 GLM-4.7-Flash；
- 研究结果进入组合覆盖层；
- 可推进内部模拟账户；
- 必须显式 `--confirm-live`；
- 仍然不连接真实券商。

## 6. 数据刷新改进

旧实现会用 `w` 模式覆盖整个数据文件。v0.8.0 新增增量合并：

- OHLCV 按 `timestamp` 合并；
- 新闻按 `event_id` 合并；
- 宏观和基本面按 `evidence_id` 合并；
- 相同 ID 的新记录覆盖旧记录；
- 历史记录按点时顺序重新排序。

实时模式还包含：

- Alpha Vantage 请求间隔 15 秒；
- FRED 请求间隔 1 秒；
- API 响应继续经过原有缓存、超时、重试、大小限制和 URL 脱敏。

## 7. 部署入口

源码模式：

```bash
make workflow-plan
make workflow-dry-run
make workflow-offline
```

真实模式只保留显式 CLI：

```bash
scripts/with_api_keys.sh \
  env PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli workflow-run \
  --mode live --confirm-live --config config/default.yaml
```

Docker Compose：

```bash
docker compose up api

docker compose --profile workflow run --rm workflow-dry-run

docker compose --profile live run --rm workflow-live
```

API 端口默认只绑定宿主机 `127.0.0.1:8000`。当前服务器 Docker daemon 代理仍可能阻止镜像构建，因此源码模式是已验证的部署路径。

## 8. 全项目改进审查

### P0：上线前必须处理

#### 8.1 轮换全部已暴露密钥

密钥已经出现在聊天消息中，即使没有写入服务器，也应视为已暴露。正式联调前应在各供应商控制台撤销并重新生成，然后只通过服务器终端的静默安装器输入。

#### 8.2 管理 API 身份认证

当前通过 localhost 绑定降低暴露面，但 FastAPI 的账户创建、会话推进和订单审批接口仍没有令牌认证。容器或反向代理对外开放前必须加入：

- 管理员 Bearer/API token；
- 审批操作审计主体；
- CSRF/来源限制（若增加浏览器表单）；
- 反向代理 TLS。

#### 8.3 SQLite Schema Migration

当前数据库使用 `CREATE TABLE IF NOT EXISTS`，没有 `PRAGMA user_version` 或迁移脚本。后续字段变化可能导致旧库无法安全升级。应增加：

- schema version；
- 顺序迁移；
- 迁移前备份；
- 启动时兼容性检查。

#### 8.4 API 与调度并发锁统一

CLI 调度使用文件锁，但 FastAPI 的 `/paper/accounts/{id}/sessions` 没有复用同一把锁。API 与 scheduler 同时触发时主要依赖数据库幂等，仍可能产生竞争和失败噪声。应将所有会话推进统一经过账户级锁。

### P1：真实联调前完成

#### 8.5 供应商额度账本

当前有静态预算，但没有持久记录实际请求数、429 次数和响应头额度。应增加每日 provider ledger，并在发请求前做硬拦截。

#### 8.6 正式交易日历

当前同步时间来自 CSV 公共日期，没有显式使用 NYSE 节假日、提前收盘和特殊停牌日历。应引入正式交易日历，并区分：

- 普通交易日；
- 节假日；
- 提前收盘；
- 单标的停牌或数据缺失。

#### 8.7 原子数据发布

增量合并已避免历史覆盖，但刷新多个标的时仍可能出现部分成功。应采用 staging directory：全部下载和审计通过后，再原子切换 `current` 数据版本。

#### 8.8 GLM 降级状态显式化

模型失败时不能悄悄伪装成正常结果。应定义：

```text
LIVE_OK
CACHE_HIT
DRY_RUN
DEGRADED_MOCK
FAILED
```

并写入决策、订单理由和仪表板。

#### 8.9 SEC 与 ETF 基本面映射

当前跳过 SPY、QQQ 的 SEC 公司事实是合理的，但 ETF 仍需要持仓结构、行业暴露和基金基本信息。应增加 ETF 专用数据源，而不是将其视为无基本面。

### P2：实验质量提升

#### 8.10 Walk-forward 验证

现有合成场景主要验证软件正确性，不能证明策略有效。真实数据阶段应采用滚动训练、验证、测试窗口，参数只能在过去窗口确定。

#### 8.11 组合基线

需要补充：

- 等权五标；
- 风险平价；
- SPY buy-and-hold；
- 60/40 或股债基准；
- 无 LLM、LLM veto-only、完整 overlay 三组消融。

#### 8.12 交易成本模型

当前使用固定手续费和滑点。后续可按成交额、波动率、ADV 和开盘跳空动态估算，并增加不可成交/部分成交情形。

#### 8.13 LLM 质量评估

除收益外，应记录：

- JSON 修复率；
- Evidence ID 拒绝率；
- 缓存命中率；
- 请求延迟和 token；
- 模型 veto 后避免的损失；
- 不同模型的一致性。

### P3：长期运维

#### 8.14 结构化日志和监控

当前主要使用 JSON 产物和 SQLite。应增加：

- 结构化运行日志；
- 日志轮转；
- `/metrics`；
- 工作流成功率；
- 数据新鲜度；
- 待审批订单数量；
- 风险状态告警。

#### 8.15 备份与恢复演练

需要定期备份：

- `paper_trading.db`；
- 真实数据版本；
- 配置和运行 Manifest；
- 审计日志。

并实际执行恢复测试，而不是只验证单进程重启。

## 9. 当前验收结论

框架已经具备：

- 完整的远程 API 调用位置；
- 默认零请求的安全模式；
- GLM 结构化请求契约；
- 调用预算；
- 全数据刷新计划；
- 候选筛选；
- 研究到组合决策的受限连接；
- 人工审批和内部成交；
- CLI、FastAPI、Compose 和运行产物。

尚未完成的关键事项是：真实密钥轮换与安装、最小真实请求验证、API 认证、数据库迁移、交易日历、配额账本和 Docker daemon 代理修复。
