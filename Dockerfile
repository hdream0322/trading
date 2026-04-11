FROM python:3.11-slim

# GitHub Actions가 빌드 시 git tag 또는 branch 이름을 전달.
# 로컬 `docker build` 에선 기본값 dev 사용.
ARG BOT_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul \
    PIP_NO_CACHE_DIR=1 \
    BOT_VERSION=${BOT_VERSION}

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
        "apscheduler>=3.10,<4"

COPY trading_bot/ ./trading_bot/
COPY config/ ./config/
COPY scripts/ ./scripts/

CMD ["python", "-m", "trading_bot.main"]
