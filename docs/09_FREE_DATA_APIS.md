# 免费真实金融数据 API 接入

TradeLab-Agent 自 v0.4.0 起提供四个可替换、可缓存的数据客户端。所有远程响应都会先转换成项目内部的点时数据格式，再进入回测；API Key 不会写入结果、缓存元数据或运行清单。

## 1. 环境变量

在 shell 中设置：

```bash
export TWELVE_DATA_API_KEY="..."
export ALPHA_VANTAGE_API_KEY="..."
export FRED_API_KEY="..."
export SEC_USER_AGENT="TradeLab-Agent your_email@example.com"
```

也可以参考 `config/env.example`。项目不会自动读取 `.env`，避免无意加载或提交密钥；使用 shell、IDE 或部署平台注入环境变量即可。

## 2. Twelve Data：日线 OHLCV

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli fetch-market \
  --symbol SPY \
  --start 2018-01-01 \
  --end 2025-12-31 \
  --output data/real/SPY.csv
```

输出列与 `LocalCsvProvider` 完全一致：

```text
timestamp,open_at,open,high,low,close,volume,available_at
```

日线默认按美东交易时间处理：开盘 09:30、收盘和可用时间 16:00。源数据按时间升序保存，并拒绝重复日期和非法 OHLC 值。

## 3. Alpha Vantage：新闻与情绪

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli fetch-news \
  --symbol SPY \
  --limit 200 \
  --output data/real/SPY_news.jsonl
```

每条新闻都保存：

- `event_id`；
- `published_at`；
- `available_at`；
- 标题、摘要和来源；
- Alpha Vantage 对该 ticker 的情绪分数。

回测只会看到 `available_at <= decision_time` 的新闻。

## 4. FRED：宏观首次发布数据

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli fetch-fred \
  --series DFF CPIAUCSL UNRATE DGS10 \
  --start 2018-01-01 \
  --end 2025-12-31 \
  --output data/real/macro.jsonl
```

默认调用 `output_type=4`，获取 observation 的 vintage 记录，并为每个 observation 选择最早的 `realtime_start` 作为 `available_at`。这比直接使用今天看到的修订后宏观序列更接近严格的 point-in-time 回测。

注意：FRED 的首次 vintage 日期是一个日期而非精确发布时间，因此项目保守地按当天 23:59:59 UTC 设为可见，避免在发布日期当天过早使用数据。

## 5. SEC EDGAR：基本面 Company Facts

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli fetch-sec \
  --ticker AAPL \
  --start 2018-01-01 \
  --end 2025-12-31 \
  --output data/real/AAPL_fundamentals.jsonl
```

默认下载以下 US-GAAP concepts：

- Revenues；
- NetIncomeLoss；
- Assets；
- Liabilities；
- StockholdersEquity；
- CashAndCashEquivalentsAtCarryingValue；
- OperatingIncomeLoss；
- EarningsPerShareDiluted。

SEC 数据使用 `filed` 日期作为可用时间，并保守设为当天 23:59:59 UTC。`SEC_USER_AGENT` 必须包含项目名和真实联系地址，以符合 SEC 公平访问要求。

## 6. 使用外部证据回测

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli experiment \
  --csv data/real/SPY.csv \
  --news data/real/SPY_news.jsonl \
  --evidence data/real/macro.jsonl \
  --evidence data/real/SPY_fundamentals.jsonl \
  --symbol SPY \
  --config config/default.yaml
```

宏观和基本面记录会进入 `EvidencePack`，并由 Context Agent、Critic Agent 和自动审计模块共同检查。消融实验中的 `quant_only` 与 `quant_plus_critic` 不读取外部上下文证据，确保比较公平。

## 7. 缓存、重试和安全

统一 HTTP 客户端提供：

- JSON 文件缓存；
- 可配置 TTL；
- HTTP 429/5xx 指数退避；
- `Retry-After` 支持；
- API Key 查询参数脱敏；
- 限定为 HTTPS；
- 最大响应体限制；
- 原子写缓存。

默认缓存目录：

```text
data/cache/http/
```

该目录已被 `.gitignore` 排除。

强制刷新远程数据：

```bash
... fetch-market --force
```

## 8. 免费额度使用建议

- Twelve Data：一次拉取足够长的日线并缓存，避免逐日请求；
- Alpha Vantage：免费额度较低，只在需要更新新闻时调用；
- FRED：批量按序列下载并长期缓存；
- SEC EDGAR：限制请求频率，保留规范 User-Agent，不并发轰炸接口。

这些 API 的免费额度、许可与限流可能变化，正式运行前应重新查看各官方文档。