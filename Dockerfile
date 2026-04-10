FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

WORKDIR /app

RUN pip install --no-cache-dir \
        "httpx[http2]>=0.27" \
        "pyyaml>=6.0" \
        "python-dotenv>=1.0" \
        "anthropic>=0.40" \
        "apscheduler>=3.10"

COPY trading_bot/ ./trading_bot/
COPY config/ ./config/
COPY scripts/ ./scripts/

CMD ["python", "-m", "trading_bot.main"]
