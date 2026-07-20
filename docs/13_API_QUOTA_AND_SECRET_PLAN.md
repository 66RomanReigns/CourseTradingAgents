# API 密钥保存与免费额度规划

版本：v0.9.0  
日期：2026-07-20

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

当前服务器已由用户在该外部文件中配置 provider key；系统只验证文件存在、权限为 `600` 和变量非空，不输出值。v0.9.0 另通过 `make secrets-token` 生成 `TRADINGLAB_API_TOKEN`，用于保护所有非公共 FastAPI 接口。

所有在聊天消息中出现过的密钥都应视为已暴露。正式联调前应在 Twelve Data、Alpha Vantage、FRED、Google 和智谱控制台撤销旧密钥、重新生成，再通过静默安装器输入新值。

## 2. 当前五标的范围

```text
SPY, QQQ, AAPL, MSFT, NVDA
```

所有外部数据先落盘并缓存，策略、回测和智能体只读本地文件。测试和回测默认离线，外部调用次数为零。

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
- 每个标的一次 `NEWS_SENTIMENT`，共 5 次；
- 请求之间至少间隔 15 秒，兼容旧的分钟频率限制；
- 每日预计占用 5/25；
- 应用层每日硬上限 10 次，保留至少 15 次人工调试余量；
- 新闻缓存至少 6 小时；
- 回测和多智能体重复运行只读同一份新闻缓存。

Alpha Vantage 是当前方案的主要瓶颈。若标的增加到 10 个，仍可每天刷新一次；若增加到 20 个，几乎没有重试和人工调试余量，应先实现最多五标的批量新闻请求，或降低刷新频率。

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
- 每个候选执行七个结构化角色调用，单工作流最多 14 次；
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
17:10 ET  Twelve Data：5 个日线请求
17:25 ET  Alpha Vantage：5 个新闻请求，每次间隔至少 15 秒
18:00 ET  FRED：最多 8 个序列，每次间隔至少 1 秒
18:15 ET  数据完整性和时间戳审计
18:20 ET  全标的 Quant 筛选
18:22 ET  GLM-4.7-Flash：Top-2 × 7，最多 14 次，默认 dry-run
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
Zhipu: glm-4.7-flash, maximum 14 requests/workflow, default dry_run
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
