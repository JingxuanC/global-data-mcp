FROM python:3.11-slim

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
