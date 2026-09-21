FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CASE_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080

# 单条命令启动：uvicorn 在固定端口 8080 提供服务与 OpenAPI(/docs, /openapi.json)
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${CASE_PORT:-8080}"]
