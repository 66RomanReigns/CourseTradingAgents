# TradeLab-Agent v0.18.0 项目说明

姓名：黄伟轩  
学号：231220091  
环境：Windows 原生，Python 3.11.5  
分支：`fix/windows-native-runtime`  
基线 commit：`fb47fd45efee1ae2430048372ea9f8f5e8a0dfba`

## 1. 我主要做了什么

这次接手 TradeLab-Agent v0.18.0，主要目标是把项目在 Windows 原生环境下跑起来，并且让它能完成一次受控的 live workflow。

我做的事情可以分成几块：

1. 修复 Windows 兼容问题。
2. 修复网络预检误判。
3. 增加 Windows 版密钥加载脚本。
4. 把默认 LLM 切到 DeepSeek。
5. 在 Twelve Data / FRED 暂时不可用时，让主流程先绕开它们。
6. 修复 Alpha Vantage 免费层不能拉 `outputsize=full` 的问题。
7. 跑离线测试、provider smoke 和一次完整 live workflow。

## 2. Windows 上修了什么

项目原来有一些地方默认更适合 Linux / macOS，在 Windows 下会出问题。

我补了这些：

- 文件锁从 POSIX `fcntl` 兼容到 Windows `msvcrt`。
- SQLite future schema 异常关闭问题做了修复。
- 新增 `scripts/configure_api_keys.ps1`。
- 新增 `scripts/with_api_keys.ps1`。
- `doctor.py` 在 Windows 下不再硬要求 POSIX `600` 权限，而是按 Windows 用户目录和 ACL 思路检查。
- `SEC_USER_AGENT` 加入 required secrets。
- PowerShell 密钥加载时，支持 `SEC_USER_AGENT` 里带空格。
- 密钥加载过程中不打印密钥值、长度、哈希，也不把密钥写入仓库。

密钥文件仍然只放在：

```text
C:\Users\user\.config\tradinglab\tradinglab.keys
```

## 3. 网络预检修了什么

之前网络预检会把 Twelve Data 根路径返回的 HTTPS 404 判成 OFFLINE。

这个判断不对，因为 HTTPS 已经返回了普通 HTTP 状态码，就说明 DNS、TCP、TLS 握手和证书验证都已经完成了。404 只能说明路径不存在，不能说明网络离线。

我改成了：

- HTTPS 普通 4xx / 5xx：允许判定为 ONLINE。
- HTTP 407 / 511：仍然判定为不安全。
- SSL 证书验证错误：仍然判定为 TLS_INTERCEPTED。
- 不关闭 TLS 验证。

对应测试也加了：

- TLS HTTP 404 -> ONLINE。
- TLS HTTP 407 / 511 -> 不允许。
- 证书错误测试继续通过。

## 4. Provider 和 LLM 怎么处理

这次真实检查里，不是所有 provider 都稳定可用：

| 项目 | 实际情况 | 处理 |
|---|---|---|
| Twelve Data | smoke 返回认证失败 | 不继续重试，默认 workflow 暂时禁用 |
| FRED | key 格式/契约检查不通过 | 不继续重试，默认 workflow 暂时禁用 |
| Zhipu | base URL 配置有问题 | 不继续用，改用 DeepSeek |
| Alpha Vantage | 免费层不支持 `outputsize=full`，之后有额度/冷却限制 | 改成 compact，并允许本地缓存 fallback |
| SEC EDGAR | 可以 live 跑通 | 用来拉基本面数据 |
| DeepSeek | 修复 PowerShell loader 后跑通 | 作为默认 LLM，模型 `deepseek-v4-flash` |

DeepSeek 的 smoke test 成功了：

- run id：`smoke-4d6c69c971ee4943bcc5963324cba8ab`
- provider：`deepseek`
- model：`deepseek-v4-flash`
- 结果：`LIVE_OK`
- schema 校验：通过
- 输出动作：`HOLD`

## 5. 数据实际跑了什么

完整 live workflow 只跑了一次，run id 是：

```text
workflow-20260729T134237901352Z
```

这次 workflow 的状态是 `COMPLETE`。

DATA_PROVIDER thread 是：

```text
workflow-20260729T134237901352Z:DATA_PROVIDER
```

它实际经过的数据节点包括：

- market：`SPY`、`QQQ`、`AAPL`、`MSFT`、`NVDA`
- news：`SPY`、`QQQ`、`AAPL`、`MSFT`、`NVDA`
- fundamentals：`AAPL`、`MSFT`、`NVDA`

Provider usage 记录如下：

| Provider | 状态 | Calls | Units |
|---|---:|---:|---:|
| network | LIVE_OK | 1 | 0 |
| sec_edgar | LIVE_OK | 3 | 6 |
| openai_compatible / DeepSeek | LIVE_OK | 29 | 29 |
| alpha_vantage | RATE_LIMITED | 5 | 0 |
| local_market_cache | CACHE_HIT | 5 | 0 |
| local_news_cache | CACHE_HIT | 5 | 0 |

也就是说：

- SEC EDGAR 真实跑了。
- DeepSeek 真实跑了。
- Alpha Vantage 因为额度/健康冷却没有继续硬怼。
- market 和 news 最后走了本地缓存 fallback。

## 6. 数据质量和最后结果

这次数据质量不是完全健康，而是：

```text
quality.status = DEGRADED
```

关键指标：

- request_count：13
- minimum_score：0.514
- mean_score：0.584121
- fallback_depth：0 和 1
- allow_position_increase：false
- requires_human_review：true
- blocked_resources：空

这个结果表示：数据没有 BLOCKED，所以 workflow 可以继续；但数据质量是 DEGRADED，所以不能增加仓位。

最后 Decision Preparation 的结果：

- candidate_symbols：`AAPL`、`QQQ`
- BUY：0
- HOLD：2
- SELL：0
- external_broker：false
- all_require_human_approval：true

所以这次完整流程跑完以后，没有真实下单，也没有接入真实券商。因为数据是 DEGRADED，系统把可能扩张风险的操作压住了，最后是 HOLD。

## 7. 测试结果

最后验证结果：

- `python -m unittest discover -s tests -v`：143/143 通过
- `python -m compileall -q src tests scripts`：通过
- `ruff check src tests scripts`：通过
- `python scripts/doctor.py`：通过，网络预检 ONLINE，external_secrets 通过
- `git diff --check`：通过
- 密钥文件没有被 Git 跟踪
- 没有把 `data/sample`、`data/multi_sample`、`data/scenarios` 的生成数据混进提交

## 8. 还没完全验证的地方

没有说成跑通的地方：

- Twelve Data 还没 live 成功。
- FRED 还没 live 成功。
- Zhipu 没继续用，改成了 DeepSeek。
- Alpha Vantage compact 修复有测试覆盖，但受额度/冷却影响，没有继续重复 live 验证。
- Paper account `demo-paper` 不存在，所以 paper session 被跳过。
- 没有接入真实券商。

## 9. 总结

这次主要不是做一个“看起来很厉害的交易结论”，而是把 TradeLab-Agent 在 Windows 上跑稳，并且让它在真实 provider 部分失败、额度限制、本地缓存 fallback 的情况下，仍然能给出可审计、安全的结果。

最终 workflow 跑通了，但因为数据质量是 DEGRADED，所以系统没有给 BUY，而是输出 HOLD。这正好说明项目里的质量门禁和风控逻辑是生效的。
