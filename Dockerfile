FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGLAB_ROOT=/app

WORKDIR /app
COPY requirements.lock pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY config ./config
COPY scripts ./scripts
COPY data/sample ./data/sample
COPY data/multi_sample ./data/multi_sample
RUN mkdir -p artifacts data/real && pip install --no-cache-dir --no-deps .

EXPOSE 8000
CMD ["uvicorn", "tradinglab_agents.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
