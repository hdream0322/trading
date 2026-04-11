"""/universe 커맨드 — 추적 종목 목록/추가/제거."""
from __future__ import annotations

import logging
from typing import Any

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import (
    _reply,
    _universe_confirm_keyboard,
    _universe_remove_picker_keyboard,
)
from trading_bot.config import save_universe_override

log = logging.getLogger(__name__)


def cmd_universe(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """추적 종목 목록 조회 및 add/remove 서브커맨드.

    사용법:
      /universe                    — 현재 목록
      /universe add 005490         — 종목 추가 (KIS 로 이름 확인 후 예/아니오)
      /universe remove 005490      — 종목 제거 (예/아니오 확인)
    """
    if not args:
        return _universe_list(ctx)
    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return _reply("사용법: `/universe add 005490`\n(종목코드 6자리)")
        return _universe_add_preview(ctx, args[1].strip())
    if sub in ("remove", "rm", "del", "delete"):
        if len(args) < 2:
            return _universe_remove_picker(ctx)
        return _universe_remove_preview(ctx, args[1].strip())
    return _reply(
        "사용법:\n"
        "`/universe` — 목록 보기\n"
        "`/universe add 005490` — 종목 추가\n"
        "`/universe remove 005490` — 종목 제거"
    )


def _universe_list(ctx: BotContext) -> dict[str, Any]:
    universe = ctx.settings.universe
    total = len(universe)
    lines = [f"*추적 중인 종목 {total}개*"]
    if not universe:
        lines.append("")
        lines.append("_(없음)_")
    else:
        groups: dict[str, list[tuple[str, str, str]]] = {}
        order: list[str] = []
        for item in universe:
            code = item["code"]
            name = item["name"]
            sector = str(item.get("sector", "")).strip() or "기타"
            price_str = "?"
            try:
                price_output = ctx.kis.get_price(code)
                price = int(price_output.get("stck_prpr") or 0)
                if price > 0:
                    price_str = f"{price:,}원"
            except Exception:
                pass
            if sector not in groups:
                groups[sector] = []
                order.append(sector)
            groups[sector].append((name, code, price_str))

        for sector in order:
            lines.append("")
            lines.append(f"*[{sector}]*")
            for name, code, price_str in groups[sector]:
                lines.append(f"- {name} (`{code}`) · {price_str}")

    lines.append(f"\n점검 주기: {ctx.settings.cycle_minutes}분")
    lines.append(
        "\n추가: `/universe add 005490`\n"
        "제거: `/universe remove 005490`"
    )
    return _reply("\n".join(lines))


def _is_valid_stock_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def _universe_remove_picker(ctx: BotContext) -> dict[str, Any]:
    """/universe remove 인자 없음 → 추적 종목을 버튼으로 나열."""
    if not ctx.settings.universe:
        return _reply("추적 중인 종목이 없습니다")
    lines = ["*제거할 종목을 고르세요*", ""]
    for item in ctx.settings.universe:
        lines.append(f"- {item['name']} (`{item['code']}`)")
    return _reply(
        "\n".join(lines),
        reply_markup=_universe_remove_picker_keyboard(ctx.settings.universe),
    )


def _universe_add_preview(ctx: BotContext, code: str) -> dict[str, Any]:
    """종목코드를 받아 KIS 로 이름을 조회하고 예/아니오 확인 버튼을 띄운다."""
    if not _is_valid_stock_code(code):
        return _reply(
            f"❌ 잘못된 종목코드: `{code}`\n"
            f"6자리 숫자여야 합니다 (예: `005930`)"
        )
    for item in ctx.settings.universe:
        if item["code"] == code:
            return _reply(
                f"ℹ️ *{item['name']}* (`{code}`) 는 이미 추적 중입니다\n\n"
                f"`/universe` 로 전체 목록을 볼 수 있어요."
            )
    try:
        name = ctx.kis.get_stock_name(code)
    except Exception as exc:
        return _reply(
            f"❌ 종목 조회 실패\n`{exc}`\n\n"
            f"종목코드 `{code}` 가 정확한지 확인하세요."
        )
    price_line = ""
    try:
        price_output = ctx.kis.get_price(code)
        price = int(price_output.get("stck_prpr") or 0)
        if price > 0:
            price_line = f"\n지금 가격: `{price:,}원`"
    except Exception:
        pass
    text = (
        f"*종목 추가 확인*\n\n"
        f"종목명: *{name}*\n"
        f"종목코드: `{code}`"
        f"{price_line}\n\n"
        f"이 종목을 추적 목록에 추가할까요?"
    )
    return _reply(text, reply_markup=_universe_confirm_keyboard("add", code))


def _universe_remove_preview(ctx: BotContext, code: str) -> dict[str, Any]:
    if not _is_valid_stock_code(code):
        return _reply(
            f"❌ 잘못된 종목코드: `{code}`\n"
            f"6자리 숫자여야 합니다"
        )
    target: dict[str, str] | None = None
    for item in ctx.settings.universe:
        if item["code"] == code:
            target = item
            break
    if target is None:
        return _reply(
            f"❌ `{code}` 는 추적 목록에 없습니다\n"
            f"`/universe` 로 현재 목록을 확인하세요."
        )
    text = (
        f"*종목 제거 확인*\n\n"
        f"종목명: *{target['name']}*\n"
        f"종목코드: `{code}`\n\n"
        f"이 종목을 추적 목록에서 제거할까요?\n\n"
        f"_이미 갖고 있는 주식이라면 그대로 남으며,_\n"
        f"_자동 손절/익절 규칙은 계속 적용됩니다._"
    )
    return _reply(text, reply_markup=_universe_confirm_keyboard("remove", code))


def _execute_universe_add(ctx: BotContext, code: str) -> dict[str, Any]:
    """확인 버튼 탭 → 실제 추가. 중복/오류는 여기서도 방어.

    추가 시 KIS inquire-price 응답의 업종명(bstp_kor_isnm) 을 sector 필드로
    즉시 박아둔다 — 섹터 분산 게이트에서 바로 사용 가능.
    """
    for item in ctx.settings.universe:
        if item["code"] == code:
            return _reply(f"ℹ️ *{item['name']}* (`{code}`) 는 이미 추적 중입니다")
    try:
        name = ctx.kis.get_stock_name(code)
    except Exception as exc:
        return _reply(f"❌ 종목 조회 실패\n`{exc}`")
    sector = ctx.kis.get_stock_sector(code)
    entry: dict[str, str] = {"code": code, "name": name}
    if sector:
        entry["sector"] = sector
    new_universe = list(ctx.settings.universe) + [entry]
    try:
        save_universe_override(new_universe)
    except Exception as exc:
        log.exception("universe.json 저장 실패")
        return _reply(f"❌ 저장 실패\n`{exc}`")
    ctx.settings.universe = new_universe
    sector_line = f"\n업종: _{sector}_" if sector else ""
    return _reply(
        f"✅ *추적 목록에 추가됨*\n\n"
        f"*{name}* (`{code}`){sector_line}\n\n"
        f"이제 총 {len(new_universe)}개 종목을 추적합니다.\n"
        f"다음 점검부터 이 종목도 함께 봅니다."
    )


def _execute_universe_remove(ctx: BotContext, code: str) -> dict[str, Any]:
    target: dict[str, str] | None = None
    new_universe: list[dict[str, str]] = []
    for item in ctx.settings.universe:
        if item["code"] == code:
            target = item
        else:
            new_universe.append(item)
    if target is None:
        return _reply(f"❌ `{code}` 는 이미 추적 목록에 없습니다")
    try:
        save_universe_override(new_universe)
    except Exception as exc:
        log.exception("universe.json 저장 실패")
        return _reply(f"❌ 저장 실패\n`{exc}`")
    ctx.settings.universe = new_universe
    return _reply(
        f"✅ *추적 목록에서 제거됨*\n\n"
        f"*{target['name']}* (`{code}`)\n\n"
        f"이제 총 {len(new_universe)}개 종목을 추적합니다.\n"
        f"_이미 갖고 있는 주식이라면 자동 손절/익절은 계속 적용됩니다._"
    )
