# API 密钥保存与免费额度规划

版本：v0.18.0
日期：2026-07-26

## 1. 密钥保存方案

真实密钥不得进入 Git、日志、SQLite、缓存文件名或报告。

推荐位置：

```text
~/.config/tradinglab/tradinglab.keys
```

创建方式：

```bash
make secrets-configure
```

脚本使用静默输入，不回显密钥，并以 `chmod 600` 写入项目目录之外。检查配置：

```bash
make secrets-check
```

加载密钥运行命令：

```bash
scripts/with_api_keys.sh make doctor
scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli fetch-market ...
```

当前服务器已由用户在该外部文件中配置 provider key；系统只验证文件存在、权限为 `600` 和变量非空，不输出值。系统另通过 `make secrets-token` 生成 `TRADINGLAB_API_TOKEN`，用于保护所有非公共 FastAPI 接口。v0.18.0 会在发送任何 provider key 前执行无凭据联网检查，并在 DATA_PROVIDER Graph 内执行共享额度、健康状态和冷却时间预检，识别校园网门户、TLS 拦截和离线状态。

所有在聊天消息中出现过的密钥都应视为已暴露。正式联调前应在 Twelve Data、Alpha Vantage、FRED、Google 和智谱控制台撤销旧密钥、重新生成，再通过静默安装器输入新值。

## 2. 当前五标的范围

```text
SPY, QQQ, AAPL, MSFT, NVDA
```

所有外部数据先经过 `<run-id>:DATA_PROVIDER` 路由、质量门控，再落盘供策略和智能体读取。测试和回测默认离线，外部调用次数为零。调用状态和额度单位写入 `artifacts/provider_usage.db`，健康、冷却和冲突事件写入 `artifacts/provider_health.db`；两者均不保存密钥、Prompt、完整响应或原始模型输出。

## 3. Twelve Data

官方免费 Basic 计划：

- 每分钟 8 API credits；
- 每日 800 credits；
- 分钟额度每分钟重置；
- 免费计划每日额度在 00:00 UTC 重置；
- `/time_series` 标准调用通常按每个标的 1 credit 计算；
- 响应头可读取 `api-credits-used` 和 `api-credits-left`。

项目规划：

- 每个工作日美东时间 17:10 更新；
- 五个标的各请求一次日线，共约 5 credits；
- 单次批次占分钟额度 5/8；
- 每日预计占用 5/800；
- 应用层每日硬上限 20 credits；
- 缓存至少 12 小时；
- 同一天禁止重复 `force-refresh`。

历史初始化也只需每个标的一次 `outputsize=5000` 请求，五标合计约 5 credits。

## 4. Alpha Vantage

官方免费服务当前明确限制为：

- 每日最多 25 次请求；
- 多数数据集可免费访问；
- 美国股票实时和 15 分钟延迟行情属于付费数据；
- 文档显示部分免费接口单次可带最多 5 个 symbols，但当前项目的新闻客户端仍按单标的一次请求处理。

项目规划：

- 每个工作日美东时间 17:25 更新；
- 正常负载为每个标的一次 `NEWS_SENTIMENT`，共 5 次；
- 当 Twelve Data 日线失败时，Provider Graph 可按标的调用 `TIME_SERIES_DAILY`，最坏再增加 5 次；
- 行情备用和新闻共享 `alpha_vantage` 节流组，全局串行且请求起点至少间隔 15 秒；
- 单次工作流最坏使用 10/25；
- 应用层每日硬上限 15 次，保留至少 10 次人工调试或恢复余量；
- 达到应用上限时，在读取或发送 API Key 前直接跳过外部 fetcher，记录零单位 `RATE_LIMITED` 事件，并回退到本地缓存；
- 新闻缓存至少 6 小时，行情缓存以最后一条 Bar 的 `available_at` 判断新鲜度；
- 回测和多智能体重复运行只读同一份本地点时数据。

Alpha Vantage 是当前方案的主要瓶颈。五标的范围为新闻和行情备用预留了完整空间；扩大标的池前应先完成批量接口评估或降低新闻刷新频率。

## 5. FRED

FRED API 免费使用并要求 API key。官方 v1 文档确认存在限流，但未公开固定的 v1 数字；v2 错误文档明确说明最多约 2 requests/second，超限返回 429，持续不遵守可能被临时封锁。

项目规划：

- 每个工作日美东时间 18:00 更新；
- 最多刷新 8 个宏观序列；
- 每次请求至少间隔 1 秒，保守低于 2 requests/second；
- 缓存 24 小时；
- 月度或季度数据可以进一步改为按发布日更新；
- 保留最早 vintage 日期，继续执行 point-in-time 防未来函数约束。

建议首批序列控制在 6–8 个，例如利率、失业率、通胀和信用环境指标，不应一次抓取大量未被策略使用的序列。

## 6. 智谱 GLM-4.7-Flash

官方模型目录当前将 `GLM-4.7-Flash` 标记为免费模型，支持 200K 上下文、最大 128K 输出、结构化输出和 Function Call。通用 API 使用 Bearer 认证，基础端点为：

```text
https://open.bigmodel.cn/api/paas/v4
```

项目规划：

- 远程 provider 设为 `zhipu`；
- 模型设为 `glm-4.7-flash`；
- 默认 `execution_mode: dry_run`，不会读取密钥或发送请求；
- 全五标先做本地 Quant 筛选，只研究 Top-2；
- 每个候选执行 13 个结构化节点：三分析师、两轮多空辩论、研究经理、初步交易员、三类风险委员和最终组合经理；
- Top-2 单标的研究为 26 次，之后增加相关性审查、集中度审查和 Portfolio Supervisor 共 3 次，标准工作流最多 29 次结构化调用；
- live 模式必须显式 `--confirm-live`；
- 默认关闭 thinking，以减少延迟和输出不确定性；
- 模型只能作为保守覆盖层，不能绕过组合风控、人工审批或直接提交订单。

智谱免费模型仍可能存在账户级并发、速率或公平使用限制，正式使用时以开放平台控制台为准。

## 7. Google / Gemini

用户提供的 Google 密钥以 `AQ.` 开头。结合 Google 2026 年的密钥迁移说明，这很可能是 Google AI Studio 新生成的 Gemini authorization key；这是基于前缀和官方迁移文档的推断，尚未发起真实请求验证。

Gemini 免费层特点：

- 仅部分模型支持免费层；
- 支持模型的输入和输出 token 可以免费；
- RPM、TPM 和 RPD 按模型、项目和账户动态变化；
- 实际生效额度必须在 Google AI Studio 的 Active rate limits 中查看；
- 免费层提交的内容可能被用于改进 Google 产品；
- Google 正在迁移到 authorization keys，2026 年 9 月后标准密钥将被拒绝；
- `gemini-2.5-flash` 和 `gemini-2.5-flash-lite` 都支持结构化输出。

项目规划：

- 默认禁用，不直接替换当前 Mock/DeepSeek 链；
- 首选 `gemini-2.5-flash-lite`；
- 复杂的最终组合审查可回退到 `gemini-2.5-flash`；
- 每日应用层最多 10 次请求；
- 每次组合运行最多 2 次：一次汇总、一次可选 Critic；
- 禁止对五个标的分别运行完整七角色 LLM 链，否则单次日运行可能超过 35 次请求；
- 不向免费层发送 API key、账户隐私、个人信息或未公开研究材料。

在接入前必须先在 AI Studio 核对该项目的 RPM、TPM、RPD 和可用模型，再做一次只返回固定短文本的验证请求。

## 8. 推荐日程

```text
17:10 ET  Provider Graph：Twelve Data 五标日线；失败标的按需走 Alpha Vantage
17:25 ET  Alpha Vantage：五个新闻请求，与行情备用共享 15 秒节流组
18:00 ET  FRED：最多 8 个序列，每次间隔至少 1 秒
18:15 ET  Conflict Resolver、Data Quality Gate 与本地数据审计
18:20 ET  全标的 Quant 筛选
18:22 ET  GLM：Top-2 × 13 + 3 次组合监督，最多 29 次，默认 dry-run
18:30 ET  持久模拟盘收盘决策与审批队列
按需       Gemini：最多 2 次组合级摘要/复核，默认关闭
```

网络失败时：

1. 优先使用仍在 TTL 内的缓存；
2. 429 时停止该供应商当前批次，不进行密集重试；
3. 当天数据不完整时不生成新方向性订单；
4. 已批准的风险清仓订单不依赖新闻或 LLM；
5. 保留上一次成功数据和失败原因供审计。

## 9. 预算配置

机器可读配置：

```text
config/api_budget.yaml
```

查看并验证：

```bash
make api-budget
```

当前五标预算应显示：

```text
Twelve Data: 5/800 daily credits, 5/8 scheduled-minute credits
Alpha Vantage: 5/25 daily requests
FRED: up to 8 series, 1-second spacing
Zhipu: configurable quick/deep models, maximum 29 requests/workflow, default dry_run
Gemini: disabled, app cap 10 requests/day
```

## 10. 后续开发顺序

1. 用户在服务器执行 `make secrets-configure`；
2. 运行 `make secrets-check`，只检查变量名和 600 权限；
3. 使用最小请求分别验证 Twelve Data、Alpha Vantage、FRED；
4. 将 API 返回的额度信息写入不含密钥的审计日志；
5. 增加统一 `data-refresh` 命令和供应商级节流器；
6. 完成增量合并，避免每次覆盖整个历史文件；
7. 用新生成的智谱密钥执行一次固定短 JSON 的最小验证请求；
8. 验证 GLM 后再决定是否保留 Gemini 作为独立复核模型；
9. 调度上线前增加失败降级、通知和每日额度统计。

## 11. 官方资料核对日期

以下结论于 2026-07-20 核对：

- Twelve Data Individual Pricing、Credits、Control over API usage；
- Alpha Vantage Customer Support、API Documentation、Premium API Key；
- FRED API Errors、FRED v2 Errors、API Terms；
- Google Gemini API Billing、Pricing、Rate Limits、API Key、Models；
- 智谱 GLM-4.7-Flash、模型概览与 HTTP API 快速开始。

供应商可能调整额度，正式长期运行时应每月复核一次，并以账户控制台显示的当前额度为最终依据。


## 11. v0.18 Provider Graph 额度与安全边界

Provider Capability Registry 为每个来源声明 `rate_limit_group`、`min_interval_seconds`、`max_concurrency` 和 `application_daily_quota`。路由器在调用前按美东自然日查询调用账本，并将同一进程内尚未完成的请求作为预留额度计算，避免并行分支同时穿透上限。

```text
Twelve Data：daily quota 20 units，最大并发 8
Alpha Vantage：行情与新闻共享 daily quota 15 units、15 秒间隔、并发 1
FRED：daily quota 8 units、1 秒间隔、并发 1
SEC EDGAR：daily quota 20 units、0.12 秒间隔、并发 1
本地缓存：0 units，不读取凭据
```

外部请求失败或额度不足时，Fallback 只允许降低风险。`DEGRADED` 数据会把所有新增 BUY 转为 HOLD；`BLOCKED` 数据不会写入正式文件，也不会进入 Research。Provider 注册表、质量阈值和请求清单共同进入 DATA_PROVIDER 输入 SHA-256，修改策略后不能复用旧 checkpoint。

本版本只完成了真实客户端适配契约和离线故障注入，尚未使用当前服务器密钥完成 Twelve Data、Alpha Vantage、FRED、SEC EDGAR 或 GLM 的正式 smoke test。首次真实联调必须逐个 Provider 执行最小请求，并核对控制台和响应头中的实际额度规则。
