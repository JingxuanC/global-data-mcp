FROM python:3.11-slim

# 国内构建加速：--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

RUN useradd --uid 10001 --no-create-home --home-dir /app appuser \
    && mkdir -p /app/.data_cache \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 50058

CMD ["python3", "server.py", "--host", "0.0.0.0", "--port", "50058"]
