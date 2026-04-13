from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# bot/mode_switch.py 의 MODE_OVERRIDE_FILE 와 같은 경로를 여기서도 참조한다.
# 순환 import 방지를 위해 Path 만 다시 계산 (같은 값).
_MODE_OVERRIDE_FILE = ROOT / "data" / "kis_mode_override"

# KIS 자격증명 런타임 오버라이드 파일.
# 존재하면 .env 의 KIS_* 값을 덮어씀. 3개월 주기 앱키 갱신 시 이 파일만 수정하고
# 텔레그램 /reload 하면 Docker 재시작 없이 새 키가 반영됨.
CREDENTIALS_OVERRIDE_FILE = ROOT / "data" / "credentials.env"

# 추적 종목 런타임 오버라이드 파일.
# 존재하면 settings.yaml 의 universe 블록을 덮어씀. 텔레그램 /universe add|remove
# 커맨드가 이 파일을 갱신하며, 변경은 메모리(ctx.settings.universe)에도 즉시 반영됨.
UNIVERSE_OVERRIDE_FILE = ROOT / "data" / "universe.json"

# /init 마법사 완료 플래그. 파일이 존재하면 첫 설치 안내를 더 이상 보내지 않음.
INIT_COMPLETED_FILE = ROOT / "data" / "init_completed"


def is_init_completed() -> bool:
    return INIT_COMPLETED_FILE.exists()


def mark_init_completed() -> None:
    from datetime import datetime, timezone
    INIT_COMPLETED_FILE.parent.mkdir(parents=True, exist_ok=True)
    INIT_COMPLETED_FILE.write_text(
        datetime.now(timezone.utc).isoformat() + "\n",
        encoding="utf-8",
    )


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
    watchtower_http_token: str   # Watchtower HTTP API 인증 토큰
    log_level: str
    universe: list[dict[str, str]]
    cycle_minutes: int
    market_open: str
    market_close: str
    risk: dict[str, Any]
    llm: dict[str, Any]
    prefilter: dict[str, Any]
    exit_rules: dict[str, Any]   # Stage 6: 손절/익절/트레일링 스톱
    rate_limit: dict[str, Any]   # KIS API throttle 간격 (초) — live/paper 별
    fundamentals: dict[str, Any]  # Stage 10: 펀더멘털 리스크 게이트 설정


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"환경변수 {name} 가 설정되지 않았습니다 (.env 확인)")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _read_mode_override() -> str | None:
    """data/kis_mode_override 파일이 있으면 그 값을, 없으면 None."""
    if not _MODE_OVERRIDE_FILE.exists():
        return None
    try:
        mode = _MODE_OVERRIDE_FILE.read_text(encoding="utf-8").strip()
        return mode if mode in ("paper", "live") else None
    except OSError:
        return None


def load_universe_override() -> list[dict[str, str]] | None:
    """data/universe.json 이 있으면 읽어서 리스트로 반환, 없으면 None.

    포맷은 settings.yaml 의 universe 블록과 동일한 `[{code, name, sector?}, ...]`.
    sector 는 선택 필드 (기동 시 자동 백필 대상).
    파손된 파일은 경고 로그 후 None 을 리턴해 settings.yaml 기본값으로 폴백.
    """
    if not UNIVERSE_OVERRIDE_FILE.exists():
        return None
    try:
        data = json.loads(UNIVERSE_OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("universe.json 파싱 실패, settings.yaml 기본값 사용: %s", exc)
        return None
    if not isinstance(data, list):
        log.warning("universe.json 형식 이상 (list 가 아님), 기본값 사용")
        return None
    result: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if not (code and name):
            continue
        entry: dict[str, str] = {"code": code, "name": name}
        sector = str(item.get("sector", "")).strip()
        if sector:
            entry["sector"] = sector
        result.append(entry)
    return result or None


def save_universe_override(universe: list[dict[str, str]]) -> None:
    """universe 리스트를 data/universe.json 에 저장 (파일 덮어쓰기).

    sector 필드가 있으면 그대로 보존.
    """
    UNIVERSE_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_OVERRIDE_FILE.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("universe override 저장: %d개 종목", len(universe))


def load_credentials_override() -> bool:
    """data/credentials.env 가 있으면 그 값으로 os.environ 을 덮어쓴다.

    런타임 호출 (텔레그램 /reload) 에도 동일하게 동작하므로 새 앱키/시크릿을 적용할 때
    Docker 재시작이 필요 없다.

    Returns True if file loaded, False if not found.
    """
    if not CREDENTIALS_OVERRIDE_FILE.exists():
        return False
    load_dotenv(CREDENTIALS_OVERRIDE_FILE, override=True)
    log.info("credentials override 로드: %s", CREDENTIALS_OVERRIDE_FILE)
    return True


def build_trade_cfg(mode: str) -> KisConfig:
    """KIS 모드에 맞는 거래용 KisConfig 를 환경변수에서 조립.

    런타임 모드 전환 시에도 호출 가능 — 새 모드의 키가 .env 에 있으면 성공,
    없으면 RuntimeError.
    """
    if mode not in ("paper", "live"):
        raise RuntimeError(f"잘못된 모드: {mode!r} (paper 또는 live)")
    prefix = "KIS_LIVE" if mode == "live" else "KIS_PAPER"
    return KisConfig(
        mode=mode,
        app_key=_require(f"{prefix}_APP_KEY"),
        app_secret=_require(f"{prefix}_APP_SECRET"),
        account_no=_require(f"{prefix}_ACCOUNT_NO"),
        account_product_cd=_optional(f"{prefix}_ACCOUNT_PRODUCT_CD", "01"),
    )


def load_settings() -> Settings:
    settings_path = ROOT / "config" / "settings.yaml"
    if not settings_path.exists():
        raise RuntimeError(f"설정 파일 없음: {settings_path}")
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    # 자격증명 오버라이드 파일이 있으면 먼저 로드 (docker env_file 값 덮어쓰기)
    load_credentials_override()

    # Mode 결정 우선순위:
    #   1. data/kis_mode_override 파일 (텔레그램 /mode 커맨드로 설정)
    #   2. .env 의 KIS_MODE
    #   3. config/settings.yaml 의 mode
    override = _read_mode_override()
    kis_mode = override or _optional("KIS_MODE", raw.get("mode", "paper"))
    if kis_mode not in {"paper", "live"}:
        raise RuntimeError(f"잘못된 KIS_MODE: {kis_mode!r} (paper 또는 live)")

    # paper/live 두 세트의 키를 .env에 동시에 보관하고 KIS_MODE로 선택.
    # 이렇게 해야 모드 전환할 때마다 키를 갈아끼우다가 실수로 실전 계좌를
    # 건드릴 위험이 사라진다.
    try:
        trade_cfg = build_trade_cfg(kis_mode)
    except RuntimeError as exc:
        if override == "live":
            # 오버라이드가 live 를 요구하는데 키가 없음 → paper 로 fallback +
            # 오버라이드 파일 삭제. 이 상황은 .env 가 재설정됐거나 키 파기된 경우.
            log.warning(
                "mode override=live 이지만 KIS_LIVE_* 키가 없음, paper 로 fallback: %s",
                exc,
            )
            try:
                _MODE_OVERRIDE_FILE.unlink()
            except OSError:
                pass
            kis_mode = "paper"
            trade_cfg = build_trade_cfg("paper")
        else:
            raise

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

    # universe 는 settings.yaml 이 기본값이고 data/universe.json 이 있으면 덮어씀.
    # 텔레그램 /universe add|remove 로 런타임 편집되는 목록.
    universe_override = load_universe_override()
    universe = universe_override if universe_override is not None else raw["universe"]

    return Settings(
        kis=trade_cfg,
        kis_quote=quote_cfg,
        telegram=TelegramConfig(
            bot_token=_require("TELEGRAM_BOT_TOKEN"),
            chat_id=_require("TELEGRAM_CHAT_ID"),
        ),
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        watchtower_http_token=_optional("WATCHTOWER_HTTP_TOKEN"),
        log_level=_optional("LOG_LEVEL", "INFO").upper(),
        universe=universe,
        cycle_minutes=int(raw.get("cycle_minutes", 10)),
        market_open=raw["market_hours"]["open"],
        market_close=raw["market_hours"]["close"],
        risk=raw.get("risk", {}),
        llm=raw.get("llm", {}),
        prefilter=raw.get("prefilter", {}),
        exit_rules=raw.get("exit", {}),
        rate_limit=raw.get("rate_limit", {}),
        fundamentals=raw.get("fundamentals", {}),
    )
