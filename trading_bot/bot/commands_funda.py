"""텔레그램 /funda 커맨드 — 종목 재무지표 조회 + 게이트 활성화 토글."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply

log = logging.getLogger(__name__)

# data/ 볼륨 안에 파일이 존재하면 펀더멘털 게이트 활성.
# settings.yaml fundamentals.enabled 의 런타임 오버라이드.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FUNDA_ENABLED_FILE = _PROJECT_ROOT / "data" / "FUNDA_ENABLED"


def is_enabled(settings_cfg: dict[str, Any]) -> bool:
    """펀더멘털 게이트가 활성 상태인지.

    우선순위: data/FUNDA_ENABLED 파일 > settings.yaml fundamentals.enabled
    """
    if FUNDA_ENABLED_FILE.exists():
        return True
    return bool(settings_cfg.get("enabled", False))


def _activate() -> None:
    FUNDA_ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    FUNDA_ENABLED_FILE.write_text(
        f"active since {datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    log.info("펀더멘털 게이트 활성화 (파일)")


def _deactivate() -> None:
    if FUNDA_ENABLED_FILE.exists():
        FUNDA_ENABLED_FILE.unlink()
        log.info("펀더멘털 게이트 비활성화 (파일 삭제)")


def cmd_funda(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """/funda — 종목 재무지표 조회 또는 게이트 활성화 토글.

    /funda 005930 — 종목 재무지표 조회
    /funda enable  — 펀더멘털 게이트 활성화
    /funda disable — 펀더멘털 게이트 비활성화
    """
    if not args:
        funda_cfg = getattr(ctx.settings, "fundamentals", None) or {}
        enabled = is_enabled(funda_cfg)
        status = "활성 ✅" if enabled else "비활성 ❌"
        return _reply(
            f"📊 *펀더멘털 게이트*: {status}\n\n"
            f"사용법:\n"
            f"`/funda 005930` — 종목 재무지표 조회\n"
            f"`/funda enable` — 게이트 켜기\n"
            f"`/funda disable` — 게이트 끄기"
        )

    sub = args[0].strip().lower()

    if sub == "enable":
        _activate()
        # 런타임 설정에도 즉시 반영
        if hasattr(ctx.settings, "fundamentals") and isinstance(ctx.settings.fundamentals, dict):
            ctx.settings.fundamentals["enabled"] = True
        # 리스크 매니저에도 반영
        if hasattr(ctx, "risk") and ctx.risk is not None:
            ctx.risk.funda_enabled = True
        return _reply("📊 펀더멘털 게이트를 *켰어요*.\n재무지표 비정상 종목 매수를 자동 차단합니다.")

    if sub == "disable":
        _deactivate()
        if hasattr(ctx.settings, "fundamentals") and isinstance(ctx.settings.fundamentals, dict):
            ctx.settings.fundamentals["enabled"] = False
        if hasattr(ctx, "risk") and ctx.risk is not None:
            ctx.risk.funda_enabled = False
        return _reply("📊 펀더멘털 게이트를 *껐어요*.\n재무지표 게이트 없이 기존 로직대로 동작합니다.")

    # 종목코드로 간주
    code = sub
    if not code.isdigit() or len(code) != 6:
        return _reply(f"❌ 종목코드는 숫자 6자리여야 합니다: `{code}`")

    from trading_bot.signals import fundamentals

    name = None
    for item in ctx.settings.universe:
        if item["code"] == code:
            name = item.get("name")
            break

    data = fundamentals.get_or_fetch(code, name, ctx.kis)
    if data is None:
        return _reply(f"❌ `{code}` 재무지표 조회 실패\n(KIS API 에러 또는 데이터 없음)")

    return _reply(fundamentals.format_for_display(data))
