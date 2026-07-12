# 服务器环境与约束记录

记录时间：2026-07-12

## 1. 系统资源

- Ubuntu 22.04.5 LTS
- Linux 6.8.0-40-generic
- x86_64
- CPU：2 × Intel Xeon Silver 4110，合计 16 核 / 32 线程
- 内存：125 GiB
- 磁盘：约 1.8 TiB，当前可用约 1.4 TiB
- GPU：2 × NVIDIA GeForce RTX 2080 Ti，每张约 11 GiB
- NVIDIA Driver：550.107.02
- Docker：28.5.1
- Git：2.34.1
- uv：0.11.28

## 2. 当前 Python 情况

系统当前 `python3` 指向 local-shell-mcp 自身虚拟环境：

- Python 3.14.6
- 当前没有 pip 命令暴露在 PATH；
- 当前环境没有 PyTorch；
- 没有 nvcc；
- 不应直接在 local-shell-mcp 的运行虚拟环境中安装课程依赖。

项目建议使用以下任一方式隔离：

1. Docker：`python:3.11-slim`；
2. `uv python install 3.11` 后创建项目 `.venv`。

优先 Docker，以保证课程验收环境一致。

## 3. GPU 使用建议

MVP 不需要 GPU。只有在部署本地 LLM 时才使用 GPU。

两张 2080 Ti 更适合运行量化后的 7B/14B 级模型，不建议将本项目变成模型训练作业。可选方案：

- Ollama / llama.cpp；
- Qwen 系列量化模型；
- 通过 OpenAI-compatible HTTP 接口与主程序解耦。

## 4. 网络约束

本次测试发现：

- 普通 DNS 和部分 HTTPS 可访问；
- 服务器访问 GitHub/codeload 时被南京大学网络认证页面重定向；
- GitHub API 请求也可能超时；
- 直接 `git clone` 和下载 tarball 当前不能稳定完成。

因此系统设计必须保证：

- 课程 Demo 不依赖实时下载；
- 依赖通过 Docker 镜像缓存或可访问的软件源准备；
- 行情数据提供本地 CSV 快照；
- LLM 提供 Mock 回放；
- 所有外部数据适配器都允许关闭；
- 网络失败时返回明确状态，而不是生成虚假数据。

## 5. 工作路径

项目根目录：

```text
/home/amax/mcp-workspace/projects/trading-agent-course/CourseTradingAgents
```

参考资料预留目录：

```text
/home/amax/mcp-workspace/projects/trading-agent-course/reference
```

由于 GitHub 网络认证限制，参考仓库当前未成功下载到服务器；设计分析来自公开仓库页面和源码页面。后续可以在网络认证完成后重新拉取，但本项目不会以复制该仓库为起点。