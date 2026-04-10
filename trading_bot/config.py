from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class KisConfig:
    mode: str
    app_key: str
    app_secret: str
    account_no: str
    account_product_cd: str

    @property
    def base_url(self) -> str:
        if self.mode == "live":
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass
class Settings:
    kis: KisConfig           # 주문/잔고용. KIS_MODE에 따라 paper 또는 live.
    kis_quote: KisConfig     # 시세용. 항상 live 서버 강제 (모의 서버는 시세 API가 불안정).
    telegram: TelegramConfig
    anthropic_api_key: str
    log_level: str
    universe: list[dict[str, str]]
    cycle_minutes: int
    market_open: str
    market_close: str
    risk: dict[str, Any]
    llm: dict[str, Any]
    prefilter: dict[str, Any]
    exit_rules: dict[str, Any]   # Stage 6: 손절/익절/트레일링 스톱


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"환경변수 {name} 가 설정되지 않았습니다 (.env 확인)")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def load_settings() -> Settings:
    settings_path = ROOT / "config" / "settings.yaml"
    if not settings_path.exists():
        raise RuntimeError(f"설정 파일 없음: {settings_path}")
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    kis_mode = _optional("KIS_MODE", raw.get("mode", "paper"))
    if kis_mode not in {"paper", "live"}:
        raise RuntimeError(f"잘못된 KIS_MODE: {kis_mode!r} (paper 또는 live)")

    # paper/live 두 세트의 키를 .env에 동시에 보관하고 KIS_MODE로 선택.
    # 이렇게 해야 모드 전환할 때마다 키를 갈아끼우다가 실수로 실전 계좌를
    # 건드릴 위험이 사라진다.
    prefix = "KIS_LIVE" if kis_mode == "live" else "KIS_PAPER"

    trade_cfg = KisConfig(
        mode=kis_mode,
        app_key=_require(f"{prefix}_APP_KEY"),
        app_secret=_require(f"{prefix}_APP_SECRET"),
        account_no=_require(f"{prefix}_ACCOUNT_NO"),
        account_product_cd=_optional(f"{prefix}_ACCOUNT_PRODUCT_CD", "01"),
    )

    # 시세 전용 설정: 실전 서버를 강제 사용. 모의 서버의 국내주식 시세 API는
    # 500 에러가 빈번해서 신뢰할 수 없음. 실전 키가 없으면 주문 설정으로 폴백.
    live_key = _optional("KIS_LIVE_APP_KEY")
    live_secret = _optional("KIS_LIVE_APP_SECRET")
    if live_key and live_secret:
        quote_cfg = KisConfig(
            mode="live",
            app_key=live_key,
            app_secret=live_secret,
            account_no=_optional("KIS_LIVE_ACCOUNT_NO"),
            account_product_cd=_optional("KIS_LIVE_ACCOUNT_PRODUCT_CD", "01"),
        )
    else:
        quote_cfg = trade_cfg

    return Settings(
        kis=trade_cfg,
        kis_quote=quote_cfg,
        telegram=TelegramConfig(
            bot_token=_require("TELEGRAM_BOT_TOKEN"),
            chat_id=_require("TELEGRAM_CHAT_ID"),
        ),
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
        universe=raw["universe"],
        cycle_minutes=int(raw.get("cycle_minutes", 10)),
        market_open=raw["market_hours"]["open"],
        market_close=raw["market_hours"]["close"],
        risk=raw.get("risk", {}),
        llm=raw.get("llm", {}),
        prefilter=raw.get("prefilter", {}),
        exit_rules=raw.get("exit", {}),
    )
