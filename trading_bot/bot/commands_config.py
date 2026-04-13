"""/config 커맨드 — 실제 로드된 설정 파일(settings.yaml) 확인.

/about 은 요약이지만 /config 는 **실제 디스크 위 파일** 과 런타임 파싱 상태를
확인하기 위한 진단용. NAS bind mount 구조상 이미지 업데이트로는 settings.yaml
이 갱신되지 않아 호스트 파일 내용이 repo 와 어긋날 수 있는데, 이 커맨드로
실제 값/경로/섹션 존재 여부를 텔레그램에서 바로 확인한다.
"""
from __future__ import annotations

import logging
from typing import Any

import yaml

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply
from trading_bot.config import ROOT

log = logging.getLogger(__name__)

SETTINGS_PATH = ROOT / "config" / "settings.yaml"

# 텔레그램 메시지 한도(4096) 안에서 안전하게 보낼 수 있는 최대 크기
_INLINE_LIMIT = 3500


def cmd_config(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """사용법:
      /config          — 핵심 섹션 요약 + 누락 키 체크
      /config file     — settings.yaml 원본을 파일로 전송
      /config raw      — 원본을 텍스트로 전송 (크면 자동 파일 전환)
    """
    sub = args[0].lower() if args else ""

    if sub == "file":
        return _reply_raw_as_file()
    if sub == "raw":
        return _reply_raw_as_text_or_file()

    return _reply_summary(ctx)


def _read_raw() -> tuple[str, str | None]:
    """raw 텍스트와 에러 메시지(None 이면 성공) 반환."""
    try:
        return SETTINGS_PATH.read_text(encoding="utf-8"), None
    except Exception as exc:
        return "", f"파일 읽기 실패: {exc}"


def _reply_raw_as_file() -> dict[str, Any]:
    text, err = _read_raw()
    if err:
        return _reply(f"❌ {err}\n경로: `{SETTINGS_PATH}`")
    return {
        "text": f"📄 `{SETTINGS_PATH}`\n({len(text):,} bytes)",
        "document": ("settings.yaml", text.encode("utf-8")),
    }


def _reply_raw_as_text_or_file() -> dict[str, Any]:
    text, err = _read_raw()
    if err:
        return _reply(f"❌ {err}\n경로: `{SETTINGS_PATH}`")
    if len(text) <= _INLINE_LIMIT:
        return _reply(
            f"📄 `{SETTINGS_PATH}`\n"
            f"```yaml\n{text}\n```"
        )
    # 너무 크면 파일 전환
    return _reply_raw_as_file()


def _reply_summary(ctx: BotContext) -> dict[str, Any]:
    """파일 경로 + 주요 섹션의 키 존재 여부 + 값 요약."""
    # 런타임 파싱 대신 파일을 직접 다시 파싱 — 실제 디스크 상태 반영
    raw_text, err = _read_raw()
    if err:
        return _reply(f"❌ {err}\n경로: `{SETTINGS_PATH}`")

    try:
        raw = yaml.safe_load(raw_text) or {}
    except Exception as exc:
        return _reply(
            f"❌ YAML 파싱 실패: `{exc}`\n"
            f"경로: `{SETTINGS_PATH}`\n\n"
            f"파일 원본은 `/config file` 로 받아보세요."
        )

    # 섹션별 필수 키 (누락 시 경고)
    required_sections: dict[str, list[str]] = {
        "risk": [
            "max_position_per_symbol_pct",
            "max_concurrent_positions",
            "daily_loss_limit_pct",
            "max_orders_per_day",
            "cooldown_minutes",
        ],
        "exit": [
            "stop_loss_pct",
            "take_profit_pct",
            "trailing_activation_pct",
            "trailing_distance_pct",
        ],
        "llm": [
            "model",
            "confidence_threshold",
            "daily_cost_limit_usd",
        ],
        "prefilter": [
            "rsi_buy_below",
            "rsi_sell_above",
            "min_volume_ratio",
        ],
    }

    lines = [
        "*⚙️ 설정 파일 진단*",
        "",
        f"📄 경로: `{SETTINGS_PATH}`",
        f"📏 크기: `{len(raw_text):,}` bytes",
        "",
    ]

    missing_total = 0
    for section, keys in required_sections.items():
        sub = raw.get(section)
        if not isinstance(sub, dict):
            lines.append(f"*[{section}]* ❌ 섹션 자체가 없거나 비어있음")
            lines.append("")
            missing_total += len(keys)
            continue
        sub_lines = [f"*[{section}]*"]
        for key in keys:
            val = sub.get(key)
            if val is None:
                sub_lines.append(f"- `{key}`: ❌ _미설정_")
                missing_total += 1
            else:
                sub_lines.append(f"- `{key}`: `{val}`")
        lines.extend(sub_lines)
        lines.append("")

    # 유니버스 (배열) 개수만
    universe = raw.get("universe")
    if isinstance(universe, list):
        lines.append(f"*[universe]* {len(universe)}개 종목")
    else:
        lines.append("*[universe]* ❌ 섹션 없음")
    lines.append("")

    if missing_total:
        lines.append(f"⚠️ 누락된 키 {missing_total}개 — repo 기본값 복원을 권장")
        lines.append(
            "NAS 에서 `curl -fsSL -o /volume1/docker/trading/config/settings.yaml "
            "https://raw.githubusercontent.com/hdream0322/trading/main/config/settings.yaml` "
            "후 `/restart`."
        )
    else:
        lines.append("✅ 필수 키 모두 존재")

    lines.append("")
    lines.append("_원본 보기: `/config raw` (텍스트) · `/config file` (파일 첨부)_")

    return _reply("\n".join(lines))
