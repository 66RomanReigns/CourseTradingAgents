# 部署与运行说明

## 1. 本地直接运行

进入项目目录：

```bash
cd /home/amax/mcp-workspace/projects/trading-agent-course/CourseTradingAgents
```

环境最低要求：

- Python 3.10+
- PyYAML 6+
- API 模式额外需要 FastAPI 与 Uvicorn

服务器当前已经具备这些运行依赖。

## 2. 一键命令

```bash
make sample       # 生成固定行情和新闻
make test         # 运行 12 项测试并做编译检查
make backtest     # 运行完整智能体
make experiment   # 运行基线与消融
make api          # 启动 FastAPI
```

## 3. CLI

### 单次回测

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli backtest \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --symbol DEMO \
  --config config/default.yaml \
  --output artifacts/backtest.json
```

### 实验套件

```bash
PYTHONPATH=src python3 -m tradinglab_agents.cli experiment \
  --csv data/sample/demo.csv \
  --news data/sample/demo_news.jsonl \
  --symbol DEMO \
  --config config/default.yaml \
  --output artifacts/experiment.json \
  --markdown artifacts/experiment.md
```

## 4. API

启动：

```bash
PYTHONPATH=src TRADINGLAB_ROOT=$(pwd) \
  uvicorn tradinglab_agents.api.app:app --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

回测：

```bash
curl -X POST http://127.0.0.1:8000/backtest \
  -H 'Content-Type: application/json' \
  -d '{
    "csv_path": "data/sample/demo.csv",
    "news_path": "data/sample/demo_news.jsonl",
    "symbol": "DEMO",
    "config_path": "config/default.yaml"
  }'
```

消融实验：

```bash
curl -X POST http://127.0.0.1:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{
    "csv_path": "data/sample/demo.csv",
    "news_path": "data/sample/demo_news.jsonl",
    "symbol": "DEMO",
    "config_path": "config/default.yaml"
  }'
```

Swagger 页面：`http://127.0.0.1:8000/docs`。

## 5. API 安全边界

- 只允许读取 `TRADINGLAB_ROOT` 内的文件；
- `../` 路径逃逸会返回 HTTP 400；
- 无真实券商接口；
- 无买卖实盘端点；
- 无密钥写入源码；
- 当前 API 是同步课程演示接口，不面向高并发生产环境。

## 6. Docker

> 当前服务器的 Docker daemon 指向未运行的 `127.0.0.1:17890` 代理，因此无法拉取基础镜像。以下配置已完成，但需要先修复 daemon 代理或导入 `python:3.11-slim`。

构建：

```bash
docker build -t tradinglab-agent:course .
```

运行：

```bash
docker run --rm -p 8000:8000 tradinglab-agent:course
```

或者：

```bash
docker compose up --build
```

容器内：

- 工作目录 `/app`；
- `TRADINGLAB_ROOT=/app`；
- 端口 `8000`；
- 默认数据位于 `/app/data/sample`；
- `artifacts` 可通过 Compose 挂载到宿主机。

## 7. 替换真实数据

行情 CSV 至少需要：

```text
timestamp,open,high,low,close,volume
```

推荐同时提供：

```text
open_at,available_at
```

约束：

- 时间戳严格递增；
- `open_at <= timestamp <= available_at`；
- 日线推荐将 `timestamp` 和 `available_at` 设为收盘时间，将 `open_at` 设为开盘时间；
- 新闻必须记录实际可获得时间，而不只是事件发生时间。

## 8. 生产化仍需补充

当前项目适合课程验收和研究原型。若进入生产环境，还需：

- 身份认证和请求限流；
- 异步任务队列；
- 数据库迁移与持久化；
- 多标的组合账本；
- 行情交易日历；
- 真实券商沙盒适配；
- 更严格的审计、监控和密钥管理。
