from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from trading_bot.config import KisConfig

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
TOKEN_DIR = Path(__file__).resolve().parent.parent.parent / "tokens"


def _token_path(mode: str) -> Path:
    return TOKEN_DIR / f"kis_token_{mode}.json"


def _load_cached(mode: str) -> dict | None:
    path = _token_path(mode)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(data["expires_at"])
        # 만료 1시간 이상 남았을 때만 재사용
        if expires_at - datetime.now() > timedelta(hours=1):
            return data
        log.info("토큰 캐시 만료 임박, 재발급 진행")
    except Exception as exc:
        log.warning("토큰 캐시 읽기 실패: %s", exc)
    return None


def _save_cache(mode: str, token: str, expires_at: datetime) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    path = _token_path(mode)
    path.write_text(
        json.dumps({"access_token": token, "expires_at": expires_at.isoformat()}),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def get_access_token(cfg: KisConfig) -> str:
    with _LOCK:
        cached = _load_cached(cfg.mode)
        if cached:
            return cached["access_token"]

        log.info("KIS 토큰 신규 발급 (mode=%s)", cfg.mode)
        url = f"{cfg.base_url}/oauth2/tokenP"
        resp = httpx.post(
            url,
            json={
                "grant_type": "client_credentials",
                "appkey": cfg.app_key,
                "appsecret": cfg.app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        expires_at = datetime.now() + timedelta(seconds=expires_in)
        _save_cache(cfg.mode, token, expires_at)
        log.info("토큰 발급 완료, 만료: %s", expires_at.strftime("%Y-%m-%d %H:%M:%S"))
        return token
