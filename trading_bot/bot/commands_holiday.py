"""/holiday 커맨드 — 임시 휴장일 수동 추가·조회·제거.

/holiday add YYYY-MM-DD <사유>   config/market_holidays.yaml 에 항목 추가
/holiday list                    수동 추가된 휴장일 목록
/holiday remove YYYY-MM-DD       항목 제거

자동 동기화(holiday_sync_job) 가 덮어쓰는 "연도 블록" 과는 별개로
'manual' 키 아래에 기록해 동기화 영향을 받지 않도록 한다.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply
from trading_bot.utils.calendar_kr import reload_holidays

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
HOLIDAYS_FILE = _PROJECT_ROOT / "config" / "market_holidays.yaml"

# market_holidays.yaml 에서 수동 항목을 저장할 최상위 키
_MANUAL_KEY = "manual"


def _load_yaml() -> dict:
    if not HOLIDAYS_FILE.exists():
        return {}
    try:
        return yaml.safe_load(HOLIDAYS_FILE.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("market_holidays.yaml 읽기 실패: %s", exc)
        return {}


def _save_yaml(data: dict) -> None:
    HOLIDAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOLIDAYS_FILE.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def cmd_holiday(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """/holiday add|list|remove …"""
    if not args:
        return _reply(
            "*휴장일 수동 관리*\n\n"
            "`/holiday add YYYY-MM-DD 사유` — 임시 휴장일 추가\n"
            "`/holiday list` — 수동 추가된 휴장일 목록\n"
            "`/holiday remove YYYY-MM-DD` — 항목 제거\n\n"
            "_자동 동기화(매주 일요일 03:30) 와 별도로 관리됩니다._"
        )

    sub = args[0].lower()

    if sub == "list":
        return _cmd_list()
    if sub == "add":
        return _cmd_add(args[1:])
    if sub == "remove":
        return _cmd_remove(args[1:])

    return _reply(
        f"❌ 모르는 서브커맨드: `{args[0]}`\n"
        "`/holiday add|list|remove`"
    )


def _cmd_list() -> dict[str, Any]:
    data = _load_yaml()
    entries: list[dict] = data.get(_MANUAL_KEY) or []
    if not entries:
        return _reply("수동 등록된 휴장일이 없습니다.")
    lines = ["*수동 등록 휴장일*", ""]
    for e in sorted(entries, key=lambda x: x.get("date", "")):
        d = e.get("date", "?")
        name = e.get("name", "")
        lines.append(f"• `{d}` {name}")
    return _reply("\n".join(lines))


def _cmd_add(args: list[str]) -> dict[str, Any]:
    if not args:
        return _reply("❌ 날짜를 입력하세요.\n`/holiday add YYYY-MM-DD 사유`")

    raw_date = args[0]
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return _reply(f"❌ 날짜 형식이 잘못됐어요: `{raw_date}`\n`YYYY-MM-DD` 형식으로 입력하세요.")

    reason = " ".join(args[1:]).strip() or "수동 등록"

    data = _load_yaml()
    entries: list[dict] = data.get(_MANUAL_KEY) or []

    # 중복 체크
    for e in entries:
        if e.get("date") == str(d):
            return _reply(f"이미 등록된 날짜입니다: `{d}` ({e.get('name', '')})")

    entries.append({"date": str(d), "name": reason})
    data[_MANUAL_KEY] = entries

    try:
        _save_yaml(data)
    except Exception as exc:
        return _reply(f"❌ 파일 저장 실패\n`{exc}`")

    # calendar_kr 캐시 갱신
    try:
        count = reload_holidays()
        log.info("휴장일 추가 후 캐시 갱신: %d개", count)
    except Exception:
        log.exception("휴장일 캐시 갱신 실패")

    return _reply(f"✅ 휴장일 추가됨: `{d}` — {reason}")


def _cmd_remove(args: list[str]) -> dict[str, Any]:
    if not args:
        return _reply("❌ 날짜를 입력하세요.\n`/holiday remove YYYY-MM-DD`")

    raw_date = args[0]
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return _reply(f"❌ 날짜 형식이 잘못됐어요: `{raw_date}`\n`YYYY-MM-DD` 형식으로 입력하세요.")

    data = _load_yaml()
    entries: list[dict] = data.get(_MANUAL_KEY) or []

    before = len(entries)
    entries = [e for e in entries if e.get("date") != str(d)]
    if len(entries) == before:
        return _reply(f"❌ 등록된 날짜가 아닙니다: `{d}`\n`/holiday list` 로 목록 확인")

    data[_MANUAL_KEY] = entries

    try:
        _save_yaml(data)
    except Exception as exc:
        return _reply(f"❌ 파일 저장 실패\n`{exc}`")

    # calendar_kr 캐시 갱신
    try:
        count = reload_holidays()
        log.info("휴장일 제거 후 캐시 갱신: %d개", count)
    except Exception:
        log.exception("휴장일 캐시 갱신 실패")

    return _reply(f"✅ 휴장일 제거됨: `{d}`")
