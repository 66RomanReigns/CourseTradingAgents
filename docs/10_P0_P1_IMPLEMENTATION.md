# P0 + P1 实施记录

版本：v0.5.0  
目标：持续模拟盘增强版的可靠基础与离线可测试 LLM 多智能体研究链。

## 1. P0：可靠性修复

### 1.1 回撤熔断

风险模块使用显式状态机：

```text
ACTIVE → LIQUIDATING → HALTED
```

- 最大回撤触发后，风险决策返回目标权重 0；
- `force_execution=true` 使保护性清仓绕过普通信号批准、再平衡阈值和冷却期；
- 持仓归零后进入粘性的 `HALTED` 状态；
- 价格反弹不会自动恢复交易，恢复机制留给后续模拟账户控制层。

### 1.2 严格配置

`config/default.yaml` 只保留当前真正实现且被运行时读取的字段。
配置加载器递归校验完整键路径，未知或尚未实现的配置会直接失败，例如：

```text
unsupported configuration key(s): risk.max_daily_turnover
```

这避免了“配置看起来启用、运行时实际静默忽略”的问题。

### 1.3 统一环境

- Python 固定为 3.11；
- `.python-version` 记录解释器版本；
- `requirements.lock` 锁定运行、API、LLM 和开发依赖；
- Makefile、Dockerfile 和 doctor 使用同一版本边界；
- `make check` 统一执行测试、编译和 Ruff 静态检查。

## 2. P1：结构化 LLM 多智能体

### 2.1 LLM Client

系统提供统一的结构化客户端接口：

- `MockLLM`：确定性、离线、可重复；
- `OpenAICompatibleClient`：支持 DeepSeek 和通用 OpenAI-compatible 服务；
- `CachedLLMClient`：内容寻址缓存与 JSONL 调用日志。

真实密钥只从环境变量读取，不写入日志、缓存或实验结果。

### 2.2 输出校验

所有角色输出都经过 Pydantic Schema 校验：

- 禁止额外字段；
- score、confidence、target_weight 范围检查；
- `SELL` 必须目标权重为 0；
- `HOLD` 必须使用 `NO_ORDER`；
- 方向性订单必须使用 `MARKET_NEXT_OPEN`；
- Trader 必须要求人工审批；
- 所有 Evidence ID 必须来自当前点时 EvidencePack。

### 2.3 角色链

```text
News Analyst ───────┐
Macro Analyst ──────┼→ Bull Researcher ─┐
Fundamental Analyst ┘                    ├→ Research Manager → Trader
                     → Bear Researcher ──┘
```

新闻、宏观和基本面拥有独立输入结构、提示词和输出 Schema，不再统一进入 `analyze_news()`。

P1 的 Research Manager 和 Trader 均受 20% 目标权重上限约束。Trader 只产生模拟意图，不连接真实券商。

## 3. 使用方式

```bash
make sync
make check
make research
```

默认 `provider: mock`，无需 API Key。真实联调时使用：

```bash
export DEEPSEEK_API_KEY="..."
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
```

然后将配置中的 provider 改为：

```yaml
llm:
  provider: openai_compatible
```

CLI：

```bash
PYTHONPATH=src .venv/bin/python -m tradinglab_agents.cli research \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --config config/default.yaml
```

API：

```text
POST /research
```

## 4. 验收结果

- 单元与集成测试：36/36 通过；
- Ruff：通过；
- Python 编译检查：通过；
- doctor：通过，Python 3.11.14；
- 密钥扫描：未发现误提交密钥；
- 离线 research CLI：成功生成结构化、人工审批门控的下一开盘模拟意图；
- 无真实订单被提交。

## 5. 当前环境限制

服务器访问 PyPI 时被南京大学网络认证页拦截，因此本次无法从公共索引重新下载全部依赖。`requirements.lock` 已根据服务器上实际可运行的 Python 3.11 环境生成并用于版本统一；网络认证恢复后应执行一次全新 `make sync` 复验。

Docker daemon 仍配置无效代理：

```text
http://127.0.0.1:17890
```

因此本次未完成镜像构建。源代码、测试、CLI 与 API 均已在本地 Python 3.11 环境验证。

## 6. 后续边界

以下属于 P2/P3，不在本次实现范围：

- 多标的统一市场快照和组合估值；
- 持久化模拟账户、订单队列和仓位恢复；
- 交易日调度与人工审批工作流；
- 第三方模拟券商；
- 通知和长期服务监控。
