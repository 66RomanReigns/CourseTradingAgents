FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGLAB_ROOT=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data/sample ./data/sample
RUN pip install --no-cache-dir ".[api]"

EXPOSE 8000
CMD ["uvicorn", "tradinglab_agents.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
