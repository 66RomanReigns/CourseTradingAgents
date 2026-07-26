# v0.18.0 Provider Graph and Data Quality Gate

版本：v0.18.0  
日期：2026-07-26

## 1. 版本目标

v0.17.0 已经补齐 XNYS 交易日历、提前收盘、拆股、现金分红和总回报复权，但 live 数据刷新仍由四组独立 `try/except` 完成：

```text
Twelve Data market
Alpha Vantage news
FRED macro
SEC EDGAR fundamentals
```

这种结构存在以下问题：

1. 不同 Provider 的重试、缓存、限流和失败状态缺少统一语义；
2. 主源失败后只能读取最近成功文件，无法按能力选择真正的备用源；
3. 多源价格冲突没有确定性解析；
4. 数据质量不会进入 Research Overlay 和最终风险约束；
5. 一个资源失败后，恢复可能重新执行其他已成功的数据请求；
6. 并行资源刷新可能同时冲击同一 Provider 的频率和日额度；
7. 额度不足通常只能等到 429 后才被发现；
8. 缓存文件被复制或重写后，文件修改时间可能掩盖数据本身已经过期。

v0.18.0 新增独立、可恢复的 DATA_PROVIDER LangGraph，并规定：

> 数据质量下降，只能维持或降低风险，不能扩大风险。

## 2. 主流程位置

```text
Credential-free Network Preflight
                 ↓
        DATA_PROVIDER LangGraph
                 ↓
       Data Quality Gate
                 ↓
          WORKFLOW_CORE
                 ↓
        RESEARCH_PARENT
                 ↓
     Symbol Research Graphs
                 ↓
      Portfolio Supervisor
                 ↓
       Decision LangGraph
                 ↓
      Paper Approval / Broker
```

线程层级：

```text
<run-id>:DATA_PROVIDER
<run-id>:WORKFLOW_CORE
  └─ <run-id>:RESEARCH_PARENT
       ├─ <run-id>:SPY
       ├─ <run-id>:QQQ
       └─ <run-id>:PORTFOLIO
<daily-run-id>:DECISION
```

Provider Graph 位于 Workflow Core 之前。它可以写入本地点时数据，但不能修改账户、订单或真实券商。

## 3. Provider Capability Registry

文件：

```text
src/tradinglab_agents/data/provider_registry.py
```

每个能力由以下字段描述：

```text
provider
data_kind
priority
authority_score
freshness_hours
timeout_seconds
max_retries
quota_units
point_in_time
rate_limit_group
min_interval_seconds
max_concurrency
application_daily_quota
supports_live
is_cache
```

数据类型：

```text
MARKET_DAILY
NEWS
MACRO
FUNDAMENTALS
```

默认路由：

```text
MARKET_DAILY
  Twelve Data
    ↓
  Alpha Vantage TIME_SERIES_DAILY
    ↓
  local_market_cache

NEWS
  Alpha Vantage NEWS_SENTIMENT
    ↓
  local_news_cache

MACRO
  FRED initial-release vintages
    ↓
  local_macro_cache

FUNDAMENTALS
  SEC EDGAR company facts
    ↓
  local_fundamentals_cache
```

## 4. 持久 Provider Health 状态机

文件：

```text
src/tradinglab_agents/storage/provider_health.py
```

数据库：

```text
artifacts/provider_health.db
```

状态：

```text
UNKNOWN
HEALTHY
DEGRADED_LATENCY
DEGRADED_STALE
RATE_LIMITED
SCHEMA_CHANGED
AUTH_FAILED
DATA_CONFLICT
OFFLINE
```

每个 `(provider, data_kind)` 保存：

```text
连续成功次数
连续失败次数
最后成功时间
最后失败时间
冷却截止时间
最近延迟
最近质量分
secret-free detail
```

同时写入不可变事件表：

```text
provider_health_events
```

### 恢复规则

- `RATE_LIMITED`、`AUTH_FAILED` 和 `OFFLINE` 在冷却结束后可以自动重试；
- `SCHEMA_CHANGED` 不自动恢复，必须修复适配器或人工确认；
- 成功结果会把瞬时故障状态恢复为 `HEALTHY` 或 `DEGRADED_LATENCY`；
- 重大数据冲突会把参与比较的来源标记为 `DATA_CONFLICT`。

## 5. Provider Graph 拓扑

文件：

```text
src/tradinglab_agents/workflows/data_provider_graph.py
```

图结构：

```text
START
  ↓
prepare
  ↓ Send(request)
route_request × N
  ↓ dynamic fan-in
persist_and_gate
  ↓
END
```

`prepare` 负责：

- 请求列表标准化；
- 重复 request ID 拒绝；
- 输入 SHA-256；
- policy fingerprint；
- fresh run 清理旧线程。

`route_request` 负责：

- 健康状态检查；
- 请求前额度检查；
- Provider 节流；
- 主源与 Fallback 路由；
- Schema 和点时验证；
- 可选 Shadow Validation；
- 单资源质量评分。

`persist_and_gate` 负责：

- 汇合全部资源；
- 聚合质量状态；
- 在任何写盘前执行 BLOCKED 检查；
- 只在质量门允许时原子合并本地文件。

## 6. Checkpoint 与恢复

默认线程：

```text
<workflow-run-id>:DATA_PROVIDER
```

默认数据库：

```text
artifacts/langgraph/data_provider_checkpoints.db
```

输入 SHA-256 覆盖：

```text
request_id
data_kind
resource
provider_order
shadow_validate
required
请求 metadata
完整 Provider Capability Registry
质量阈值
冲突阈值
节流与额度配置
```

若任一策略或请求变化，resume 会 fail closed：

```text
data provider resume input mismatch
```

动态 fan-out 的成功资源会作为 pending writes 保留。某个资源失败后恢复：

```text
SPY success → 不重跑
QQQ failure → 只重跑 QQQ
Macro success → 不重跑
最终 Quality Gate → 在全部资源完成后执行一次
```

## 7. Fallback Router

文件：

```text
src/tradinglab_agents/data/fallback_router.py
```

路由过程：

```text
读取 capability priority
        ↓
检查健康状态和 cooldown
        ↓
检查共享应用额度
        ↓
进入 provider pacing slot
        ↓
调用 fetcher
        ↓
规范化 ProviderCandidate
        ↓
验证 provider / kind / resource / records
        ↓
记录 health 与 usage
        ↓
失败则尝试下一 capability
```

Fallback 深度进入质量评分：

```text
primary depth = 0
secondary depth = 1
local cache depth >= 1
```

任何 fallback 结果，即使内容足够新，也不会允许扩大仓位。

## 8. Provider pacing

动态资源 fan-out 并不等于同一 API 无限并发。

每个能力声明：

```text
rate_limit_group
min_interval_seconds
max_concurrency
```

默认配置：

```text
Twelve Data
  group=twelve_data
  max_concurrency=8

Alpha Vantage market + news
  group=alpha_vantage
  min_interval_seconds=15
  max_concurrency=1

FRED
  group=fred
  min_interval_seconds=1
  max_concurrency=1

SEC EDGAR
  group=sec_edgar
  min_interval_seconds=0.12
  max_concurrency=1

Local cache
  quota=0
  credentials=none
```

因此 Alpha Vantage 的行情备用和新闻请求共享同一节流器，不会分别并发突发。

## 9. 请求前额度门控

调用账本：

```text
artifacts/provider_usage.db
```

路由器在外部 fetcher 执行前：

1. 计算 America/New_York 当日零点；
2. 查询共享 rate-limit group 内所有 Provider 当日已使用 units；
3. 加上当前进程尚未完成的 reserved units；
4. 检查 `used + reserved + requested <= application_daily_quota`；
5. 额度不足时不执行 fetcher，不读取或发送 API Key。

额度不足会记录：

```text
status=RATE_LIMITED
units=0
quota_preflight_blocked=true
credentials_sent=false
```

Provider 冷却到下一个纽约自然日，然后路由到下一来源。

默认应用上限：

```text
Twelve Data: 20 units/day
Alpha Vantage shared market+news: 15 units/day
FRED: 8 units/day
SEC EDGAR: 20 units/day
```

这些是项目应用层保护值，不声称等同于 Provider 控制台的实际账户额度。

## 10. ProviderCandidate

所有来源转换为统一候选：

```text
provider
data_kind
resource
canonical records
observed_at
latest_data_at
schema_valid
point_in_time_valid
completeness
metadata
payload_sha256
```

候选不会把 API Key、Authorization Header 或完整请求 URL写入 Graph state。

### 实际数据时间

新鲜度使用：

```text
latest returned record.available_at
```

而不是：

```text
HTTP request time
cache file modification time
```

因此复制旧文件、重新写入缓存或重新生成索引不会把旧数据伪装成新数据。

## 11. Conflict Resolver

文件：

```text
src/tradinglab_agents/data/provider_quality.py
```

当前对 `MARKET_DAILY` 执行数值比较，使用共同时间戳的 close：

```text
relative difference = abs(primary - secondary) / max(abs(primary), abs(secondary))
```

默认阈值：

```text
<= 0.5%   CONSISTENT
0.5%–2%   MINOR
> 2%      MAJOR
```

`MAJOR`：

```text
quality = BLOCKED
requires_human_review = true
allow_position_increase = false
参与来源 health = DATA_CONFLICT
正式数据文件不写入
Research 不启动
```

`MINOR`：

```text
quality = DEGRADED
允许继续解释
禁止扩大仓位
```

新闻、宏观与基本面当前不做全文或语义冲突合并；它们仍执行 authority、freshness、schema、completeness 和 point-in-time 评分。

## 12. Data Quality Score

质量维度：

```text
freshness_score
authority_score
agreement_score
schema_score
completeness_score
point_in_time_score
fallback_depth penalty
```

默认聚合权重在代码中固定并测试。最终状态：

```text
NORMAL
DEGRADED
BLOCKED
```

### NORMAL

- 主源成功；
- Schema 和点时有效；
- 新鲜度与完整度满足要求；
- 没有明显冲突；
- 允许进入正常研究和风险流程。

### DEGRADED

可能原因：

- 使用备用源；
- 使用本地缓存；
- 数据较旧但未低于 BLOCK 阈值；
- 轻微跨源差异；
- 质量分低于 minimum threshold。

处理：

```text
Research 可以继续解释
requires_human_review = true
allow_position_increase = false
```

### BLOCKED

可能原因：

- 重大跨源冲突；
- Schema 无效；
- point-in-time 无效；
- 质量分低于 block threshold。

处理：

```text
写盘前失败
Workflow Core 不启动
Research 不启动
Paper 不运行
```

## 13. Overlay Non-expansion Gate

Provider 质量摘要进入：

```text
WORKFLOW_CORE data_validation
Overlay Assembly
Decision Preparation
```

若：

```text
allow_position_increase = false
```

则每个研究 Overlay：

```text
BUY → HOLD
target_weight → 0
order_type → NO_ORDER
confidence → min(original confidence, quality mean score)
requires_human_approval → true
```

保护性 SELL 不会被削弱。

Trace 保存：

```text
quality status
minimum / mean score
degraded resources
blocked resources
original action
original target
enforced_hold
cannot_increase_risk
non_expansion_verified
```

Decision Preparation schema 升级为 v3，并把 Provider Quality 摘要加入交接信息。

## 14. Live Adapter

文件：

```text
src/tradinglab_agents/data/provider_adapters.py
```

外部客户端惰性创建：

```text
只有 Provider Router 真正选择外部 capability 时才构造客户端并读取凭据
```

Registry 中的：

```text
timeout_seconds
max_retries
```

会用于构造 `CachedHttpJsonClient`。

真实适配：

```text
TwelveDataClient.fetch_daily_bars
AlphaVantageNewsClient.fetch_daily_bars
AlphaVantageNewsClient.fetch_news
FredClient.fetch_initial_release_records
SecEdgarClient.fetch_fundamental_records
```

本地适配：

```text
LocalCsvProvider
LocalNewsProvider
LocalPointInTimeEvidenceProvider
```

## 15. Alpha Vantage 日线备用

v0.18.0 为 Alpha Vantage 增加：

```text
TIME_SERIES_DAILY
```

解析后仍通过 `ExchangeTradingCalendar`：

- 节假日行拒绝；
- 提前收盘使用真实 13:00；
- 开收盘时间使用 XNYS；
- 原始 OHLCV 转为统一 `Bar`；
- 不绕过 v0.17 市场语义。

## 16. 配置

```yaml
workflow:
  provider_graph_enabled: true
  provider_usage_database: artifacts/provider_usage.db
  provider_health_database: artifacts/provider_health.db
  provider_checkpoint_database: artifacts/langgraph/data_provider_checkpoints.db
  provider_min_quality_score: 0.75
  provider_block_quality_score: 0.45
  provider_conflict_warn_relative_difference: 0.005
  provider_conflict_block_relative_difference: 0.02
  provider_shadow_validation_enabled: false
```

Shadow Validation 默认关闭，避免正常日运行额外消耗备用源额度。可在受控验证环境中开启。

## 17. 回退开关

```yaml
workflow:
  provider_graph_enabled: false
```

会启用 v0.17 顺序刷新路径，但该路径被标记为：

```text
DEGRADED
legacy_unassessed
allow_position_increase=false
requires_human_review=true
```

因此关闭新图不会恢复到“未评估数据可正常加仓”的不安全行为。

## 18. CLI 与 Make

```bash
make provider-capabilities
make provider-health
make provider-events
make data-provider-graph
```

CLI：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-capabilities
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-health
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli provider-events
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli data-provider-graph
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research-thread-status \
  '<run-id>:DATA_PROVIDER'
```

## 19. API

受 Token 保护的只读接口：

```text
GET /providers/capabilities
GET /providers/health
GET /providers/events
GET /providers/conflicts
GET /market/data-quality
GET /workflow/data-provider-graph
GET /research/threads/{run-id}:DATA_PROVIDER
```

这些接口不会触发外部请求，只返回 bounded、secret-free 元数据。

## 20. Doctor

Doctor 使用离线 fetcher 注入：

```text
primary → HTTP 429
fallback → local cache
```

验证：

```text
fallback depth = 1
quality = DEGRADED
allow position increase = false
primary health = RATE_LIMITED
```

另一个场景：

```text
primary close = 100
secondary close = 110
```

验证：

```text
conflict = MAJOR
quality = BLOCKED
persisted rows = 0
checkpoint database exists
external requests = 0
credentials sent = false
```

## 21. 测试覆盖

v0.18 专项测试覆盖：

1. 429 主源自动回退；
2. 缓存 fallback 禁止加仓；
3. 瞬时 OFFLINE 状态恢复；
4. 重大行情冲突在写盘前失败；
5. 动态资源恢复只重跑失败资源；
6. Router policy 变化拒绝 resume；
7. DEGRADED BUY 强制变为 HOLD；
8. BLOCKED 不能进入 Overlay；
9. 共享日额度在 fetcher 前阻断；
10. quota preflight 记录 0 units；
11. 共享 Provider pacing；
12. 无网络 live 编排质量贯穿 Workflow Core；
13. Alpha Vantage 提前收盘和节假日解析。

完整离线套件：

```text
135 / 135 passed
```

## 22. 尚未验证和已知限制

v0.18.0 没有使用当前服务器密钥执行正式真实 API 请求，因此以下内容仍需分阶段 smoke test：

- Twelve Data Authorization Header 和真实响应字段；
- Alpha Vantage 当前账户的 NEWS_SENTIMENT 与 TIME_SERIES_DAILY 可用性；
- FRED 实际账户级限速；
- SEC User-Agent 接受情况；
- Provider 响应头中的实际剩余额度；
- GLM 免费模型的真实并发和稳定性。

当前 Conflict Resolver 只对日行情数值执行跨源比较，尚未实现：

- 新闻事件实体级去重和矛盾检测；
- 宏观不同 vintage 的来源间比较；
- SEC 与其他基本面来源的字段映射冲突；
- 复权口径不一致自动识别；
- 供应商时间戳延迟分布学习；
- 主动告警和通知。

## 23. 安全结论

v0.18.0 的安全边界：

```text
网络不可信 → 凭据不发送
额度不足 → fetcher 不执行
Provider 失败 → 自动尝试下一能力
Fallback/缓存 → 禁止扩大风险
轻微冲突 → DEGRADED + 人工审阅
严重冲突 → 写盘前 BLOCKED
Schema/PIT 违规 → 写盘前 BLOCKED
Graph 失败 → 已成功资源可恢复，不重复消耗
所有 Provider Graph → 不修改账户，不创建订单，不连接真实券商
```
