# v0.10.0 安全 Live Provider 运行时

版本：v0.10.0

## 1. 本次真实联调发现的问题

第一次使用用户已配置的 Twelve Data Key 发起最小请求时，请求没有到达供应商业务接口，而是在 TLS 握手阶段失败。进一步检查发现：

- 多个外部域名返回同一个未知自签名证书；
- 忽略证书验证后，返回内容要求跳转到 `https://p.nju.edu.cn/`；
- 服务器所在南京大学校园网当前没有完成外网接入认证；
- 这不是 API Key 无效，也不是 Twelve Data 返回限流；
- 在此状态下继续发送 API Key 会把密钥暴露给认证网关或中间设备，因此系统必须停止。

项目没有通过关闭 TLS 验证来绕过问题。

## 2. 无凭据联网前置检查

Live 工作流现在先访问：

```text
http://connectivitycheck.gstatic.com/generate_204
```

正常网络应返回 HTTP 204。系统会识别：

```text
ONLINE
CAPTIVE_PORTAL
TLS_INTERCEPTED
OFFLINE
```

其中：

- HTTP 状态不为 204；
- 域名被重定向；
- HTML 中出现 JavaScript `location.href`；
- 页面出现认证提示；

都会被判定为 `CAPTIVE_PORTAL`。

只有 HTTP 探针通过后，系统才会验证普通外部 HTTPS 证书。任何证书验证失败都会标记为 `TLS_INTERCEPTED`，不会自动信任未知证书。

## 3. 密钥发送边界

顺序固定为：

```text
确认 --confirm-live
→ 检查密钥变量是否存在
→ 无凭据网络探针
→ TLS 验证
→ network_gate
→ 数据与模型供应商调用
```

因此校园网未认证时：

```text
preflight          COMPLETE
network_probe      COMPLETE
network_gate       FAILED
provider calls     0
provider key sent  false
```

不能通过配置关闭 TLS 验证。

## 4. 可恢复的网络节点

网络状态属于易变状态，不能像历史数据分析节点一样永久复用。

恢复同一个 `run_id` 时：

- 配置哈希仍必须一致；
- `preflight` 等稳定节点可以复用；
- `network_probe` 和 `network_gate` 会重新执行；
- 校园网认证完成后可以从原运行继续；
- 已完成的稳定研究节点不会重复运行。

当前被校园网门户阻断的真实运行目录为：

```text
artifacts/workflows/workflow-20260720T110620478923Z
```

认证完成后可以使用：

```bash
scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli workflow-run \
  --mode live \
  --confirm-live \
  --run-id workflow-20260720T110620478923Z \
  --resume \
  --config config/default.yaml
```

## 5. 最小供应商冒烟

完整工作流可能计划 33 个外部请求，因此先提供单供应商最小验证：

```bash
scripts/with_api_keys.sh env PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli provider-smoke \
  --provider twelve_data \
  --symbol AAPL \
  --confirm-live
```

支持：

```text
twelve_data
alpha_vantage
fred
sec_edgar
zhipu
all
```

最小请求设计：

- Twelve Data：AAPL 两根日线；
- Alpha Vantage：AAPL 最新一条新闻；
- FRED：DGS10 最近约三周；
- SEC EDGAR：AAPL 的一个 US-GAAP concept；
- Zhipu：一个要求 HOLD 的最小结构化 TradePlan。

冒烟输出不包含 API Key、原始 Prompt 或完整模型输出。

## 6. 供应商状态与调用账本

数据库：

```text
artifacts/provider_usage.db
```

记录：

- run ID；
- provider；
- operation；
- resource；
- 状态；
- 额度单位；
- 开始与结束时间；
- 延迟；
- 经过截断和脱敏的错误；
- token 等安全元数据。

状态包括：

```text
LIVE_OK
CACHE_HIT
DEGRADED_STALE
NETWORK_BLOCKED
RATE_LIMITED
AUTH_FAILED
PROVIDER_ERROR
FAILED
DRY_RUN
SKIPPED
```

查看：

```bash
PYTHONPATH=src .venv/bin/python \
  -m tradinglab_agents.cli provider-usage
```

或通过受认证 API：

```text
GET /providers/usage
GET /providers/usage?run_id=<run_id>
```

## 7. GLM 遥测

每个结构化角色调用单独记录：

- task 名称；
- Schema 名称；
- `LIVE_OK` 或 `CACHE_HIT`；
- response ID；
- 模型返回名称；
- finish reason；
- prompt/completion/total tokens；
- 调用耗时。

不会记录：

- API Key；
- Authorization header；
- 完整 system prompt；
- 完整 user payload；
- 完整模型回答。

## 8. HTTP 层修复

### 8.1 Twelve Data 密钥

旧实现将 Key 放在查询参数：

```text
?apikey=...
```

v0.10.0 改为：

```text
Authorization: apikey <key>
```

HTTP 缓存身份只保存 Authorization 的 SHA-256，不保存原值。

### 8.2 压缩响应

新增：

```text
gzip
deflate
```

解压后仍执行响应大小上限，防止压缩炸弹或异常大响应。

### 8.3 增量刷新

市场数据：

- 首次请求完整历史；
- 后续从最后交易日向前重叠 7 天；
- 使用合并写入消除重复并允许供应商修订。

新闻：

- 首次使用 `LATEST`；
- 后续从最后一条新闻前一分钟开始重叠请求；
- 不再在无时间范围时使用 `EARLIEST`，避免首次获取最旧新闻。

## 9. 降级策略

默认：

```yaml
provider_failure_policy: fail_closed
```

供应商失败时停止本次 Live 工作流。

可选：

```yaml
provider_failure_policy: last_known_good
stale_data_max_hours: 48
```

只有同时满足以下条件才使用旧数据：

- 文件存在；
- 文件非空；
- 数据年龄没有超过限制；
- 配置显式允许。

结果会标记：

```text
DEGRADED_STALE
```

不会标记为 `LIVE_OK`。

## 10. 与 TradingAgents 成熟能力的对齐

本版本主要对齐了以下工程能力，而不是复制其源码：

- 真实数据供应商故障分类；
- 调用与模型遥测；
- 断点恢复；
- 供应商级运行状态；
- 缓存命中可见性；
- 限额与失败审计；
- 真实运行前的小流量验证；
- 数据更新的增量化。

TradeLab-Agent 继续保留自己的边界：

- 点时 Evidence；
- 硬组合风控；
- 人工审批；
- 下一开盘模拟成交；
- 不连接真实券商；
- 不允许 LLM 绕过风险状态机。
