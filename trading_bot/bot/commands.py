"""텔레그램 커맨드 라우팅 facade.

실제 핸들러는 도메인별 모듈에 분산되어 있고, 이 파일은:
1. 텔레그램 자동완성 메뉴 (TELEGRAM_BOT_COMMANDS)
2. /command → 핸들러 dispatch 테이블 (COMMAND_MAP)
3. inline 버튼 callback → 핸들러 dispatch (handle_callback)
4. 외부 호출 진입점 (handle_command, handle_callback)
5. backwards-compat 재노출 (formatters · keyboards) — cycle.py / briefing.py 등
   기존 코드가 `from trading_bot.bot.commands import fmt_won` 형태로 가져오므로
   리팩터링 후에도 동작하도록 한 줄짜리 re-export 유지.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from trading_bot.bot.commands_core import (
    HELP_TEXT,
    _build_sell_confirm,
    _execute_confirmed_sell,
    _sell_select,
    cmd_about,
    cmd_accuracy,
    cmd_cost,
    cmd_cycle,
    cmd_help,
    cmd_logic,
    cmd_menu,
    cmd_positions,
    cmd_quiet,
    cmd_resume,
    cmd_sell,
    cmd_signals,
    cmd_status,
    cmd_stop,
)
from trading_bot.bot.commands_config import cmd_config, handle_config_callback
from trading_bot.bot.commands_set import cmd_set, handle_set_callback
from trading_bot.bot.commands_creds import cmd_reload, cmd_restart, cmd_setcreds
from trading_bot.bot.commands_export import cmd_export
from trading_bot.bot.commands_funda import cmd_funda
from trading_bot.bot.commands_init import cmd_init, handle_init_callback
from trading_bot.bot.commands_logs import cmd_logs
from trading_bot.bot.commands_mode import cmd_mode
from trading_bot.bot.commands_style import cmd_style, handle_style_callback
from trading_bot.bot.commands_universe import (
    _execute_universe_add,
    _execute_universe_remove,
    _universe_remove_preview,
    cmd_universe,
)
from trading_bot.bot.commands_holiday import cmd_holiday
from trading_bot.bot.commands_update import cmd_notes, cmd_update
from trading_bot.bot.context import BotContext
from trading_bot.notify.markdown_escape import escape_markdown
from trading_bot.bot.formatters import (  # noqa: F401 — 외부에서 commands.* 로 임포트
    confidence_pct,
    decision_ko,
    fmt_pct,
    fmt_uptime,
    fmt_won,
    mode_badge,
)
from trading_bot.bot.keyboards import (  # noqa: F401 — 외부에서 commands.* 로 임포트
    _reply,
    cycle_summary_keyboard,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Telegram / 자동완성 메뉴에 등록될 커맨드 목록
# 봇 기동 시 telegram.set_commands(..., TELEGRAM_BOT_COMMANDS) 호출
# ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_COMMANDS: list[tuple[str, str]] = [
    # 자동완성에는 핵심 9개만 노출 — 나머지는 /menu 의 카테고리 버튼으로 자연스럽게 탐색.
    # 모든 커맨드는 COMMAND_MAP 에 그대로 살아있어서 직접 입력하면 동작함.
    ("menu", "🏠 메인 허브 (모든 기능을 버튼으로)"),
    ("status", "💰 지금 상태 (자산·킬·비용)"),
    ("positions", "📈 갖고 있는 주식"),
    ("cycle", "🔄 지금 바로 점검 실행"),
    ("style", "⚡ 거래 스타일 (단타/장기/기본)"),
    ("mode", "🟡 거래 모드 (모의/실전)"),
    ("stop", "🛑 긴급 정지"),
    ("resume", "✅ 긴급 정지 풀기"),
    ("help", "ℹ️ 사용법 (전체 커맨드)"),
]


COMMAND_MAP: dict[str, Callable[[BotContext, list[str]], dict[str, Any]]] = {
    "/start": cmd_menu,
    "/help": cmd_help,
    "/menu": cmd_menu,
    "/about": cmd_about,
    "/logic": cmd_logic,
    "/mode": cmd_mode,
    "/style": cmd_style,
    "/universe": cmd_universe,
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/signals": cmd_signals,
    "/cost": cmd_cost,
    "/accuracy": cmd_accuracy,
    "/stop": cmd_stop,
    "/kill": cmd_stop,
    "/resume": cmd_resume,
    "/quiet": cmd_quiet,
    "/sell": cmd_sell,
    "/cycle": cmd_cycle,
    "/update": cmd_update,
    "/setcreds": cmd_setcreds,
    "/reload": cmd_reload,
    "/restart": cmd_restart,
    "/funda": cmd_funda,
    "/export": cmd_export,
    "/logs": cmd_logs,
    "/init": cmd_init,
    "/config": cmd_config,
    "/set": cmd_set,
    "/holiday": cmd_holiday,
}


def handle_command(ctx: BotContext, cmd: str, args: list[str]) -> dict[str, Any] | None:
    handler = COMMAND_MAP.get(cmd.lower())
    if handler is None:
        return _reply(f"모르는 명령어: `{escape_markdown(cmd)}`\n`/help` 로 목록 보기")
    try:
        return handler(ctx, args)
    except Exception as exc:
        log.exception("커맨드 %s 처리 중 예외", cmd)
        return _reply(f"❌ 처리 실패\n`{escape_markdown(type(exc).__name__)}: {escape_markdown(str(exc))}`")


# ─────────────────────────────────────────────────────────────
# 콜백 (inline 버튼) 핸들러
# ─────────────────────────────────────────────────────────────

def handle_callback(ctx: BotContext, data: str) -> dict[str, Any] | None:
    if data == "cancel":
        return _reply("취소됐습니다")
    if data == "kill":
        return cmd_stop(ctx, [])
    if data == "resume":
        return cmd_resume(ctx, [])
    if data == "positions":
        return cmd_positions(ctx, [])
    if data == "status":
        return cmd_status(ctx, [])
    if data == "help":
        return cmd_help(ctx, [])
    if data == "universe_list":
        return cmd_universe(ctx, [])
    if data == "cycle_run":
        return cmd_cycle(ctx, [])
    # 메뉴 허브 카테고리 진입
    if data.startswith("hub:"):
        return _show_hub(ctx, data.split(":", 1)[1])
    # 카테고리 안에서 실제 커맨드로 deep-link (인자 없이 기본 응답)
    if data.startswith("go:"):
        target = data.split(":", 1)[1]
        return _go_to_command(ctx, target)
    if data.startswith("sell_select:"):
        code = data.split(":", 1)[1]
        return _sell_select(ctx, code)
    if data.startswith("sell_confirm:"):
        code = data.split(":", 1)[1]
        return _execute_confirmed_sell(ctx, code)
    if data.startswith("universe_add:"):
        code = data.split(":", 1)[1]
        return _execute_universe_add(ctx, code)
    if data.startswith("universe_remove:"):
        code = data.split(":", 1)[1]
        return _execute_universe_remove(ctx, code)
    if data.startswith("universe_rm_pick:"):
        code = data.split(":", 1)[1]
        return _universe_remove_preview(ctx, code)
    if data.startswith("mode_to:"):
        target = data.split(":", 1)[1]
        return cmd_mode(ctx, [target])
    if data == "mode_confirm_live":
        return cmd_mode(ctx, ["live", "confirm"])
    if data == "update_confirm":
        return cmd_update(ctx, ["confirm"])
    if data == "update_skip":
        return _reply("✋ 업데이트를 건너뛰었어요. 나중에 `/update` 로 다시 확인할 수 있어요.")
    if data == "update_auto_on":
        return cmd_update(ctx, ["enable"])
    if data == "update_auto_off":
        return cmd_update(ctx, ["disable"])
    if data == "quiet_on":
        return cmd_quiet(ctx, ["on"])
    if data == "quiet_off":
        return cmd_quiet(ctx, ["off"])
    if data == "funda_on":
        return cmd_funda(ctx, ["enable"])
    if data == "funda_off":
        return cmd_funda(ctx, ["disable"])
    if data == "restart_confirm":
        return cmd_restart(ctx, ["confirm"])
    if data.startswith("export_"):
        sub = data.split("_", 1)[1]
        return cmd_export(ctx, [sub])
    if data.startswith("init:"):
        try:
            chat_id = int(ctx.settings.telegram.chat_id)
        except (TypeError, ValueError):
            chat_id = 0
        return handle_init_callback(ctx, chat_id, data)
    if data.startswith("set:"):
        return handle_set_callback(ctx, data)
    if data.startswith("config:"):
        return handle_config_callback(ctx, data)
    if data.startswith("style_to:"):
        return handle_style_callback(ctx, data)
    return _reply(f"모르는 버튼: `{escape_markdown(data)}`")


# ─────────────────────────────────────────────────────────────
# 메뉴 허브 — 카테고리 5개로 묶어 자연스러운 탐색 제공
# ─────────────────────────────────────────────────────────────

_HUB_SECTIONS: dict[str, tuple[str, str]] = {
    "status": (
        "📊 *현황*",
        "지금 봇이 어떻게 돌아가는지 — 계좌·보유·추천·비용·로직.",
    ),
    "trade": (
        "💸 *거래*",
        "수동 거래 + 추적 종목 + 펀더멘털 게이트.",
    ),
    "settings": (
        "⚙️ *설정*",
        "거래 스타일·모드·임계값. 단타/장기 토글은 여기서.",
    ),
    "safety": (
        "🛡️ *안전*",
        "긴급 정지·조용 모드·재시작·자격증명 재로드.",
    ),
    "ops": (
        "🔄 *운영·도구*",
        "업데이트·내보내기·로그·자격증명 교체·첫 설치 마법사.",
    ),
}


def _show_hub(ctx: BotContext, section: str) -> dict[str, Any]:
    from trading_bot.risk import kill_switch
    from trading_bot.bot.keyboards import hub_section_keyboard

    if section == "main":
        return cmd_menu(ctx, [])
    spec = _HUB_SECTIONS.get(section)
    if spec is None:
        return _reply(f"❌ 모르는 카테고리: `{section}`")
    title, hint = spec
    text = f"{title}\n{hint}\n\n_원하는 항목을 누르세요. '🏠 처음으로' 로 메인으로._"
    return _reply(text, reply_markup=hub_section_keyboard(section, kill_switch.is_active()))


# go:<cmd> → 인자 없이 해당 커맨드 호출 (자연스러운 다음 단계로 이동).
_GO_HANDLERS: dict[str, Callable[[BotContext, list[str]], dict[str, Any]]] = {
    "signals": cmd_signals,
    "accuracy": cmd_accuracy,
    "cost": cmd_cost,
    "about": cmd_about,
    "logic": cmd_logic,
    "sell": cmd_sell,
    "funda": cmd_funda,
    "style": cmd_style,
    "mode": cmd_mode,
    "config": cmd_config,
    "set": cmd_set,
    "quiet": cmd_quiet,
    "restart": cmd_restart,
    "reload": cmd_reload,
    "update": cmd_update,
    "export": cmd_export,
    "logs": cmd_logs,
    "setcreds": cmd_setcreds,
    "init": cmd_init,
}


def _go_to_command(ctx: BotContext, target: str) -> dict[str, Any]:
    handler = _GO_HANDLERS.get(target)
    if handler is None:
        return _reply(f"❌ 연결되지 않은 항목: `{target}`")
    return handler(ctx, [])


# ─────────────────────────────────────────────────────────────
# Backwards-compat re-exports
# 기존 import 경로 유지: scripts/* 와 외부 모듈이 commands.* 로 가져옴
# ─────────────────────────────────────────────────────────────

__all__ = [
    "TELEGRAM_BOT_COMMANDS",
    "HELP_TEXT",
    "COMMAND_MAP",
    "handle_command",
    "handle_callback",
    # formatters
    "fmt_won",
    "fmt_pct",
    "decision_ko",
    "mode_badge",
    "confidence_pct",
    "fmt_uptime",
    # keyboards
    "cycle_summary_keyboard",
    # core handlers (외부 호출용)
    "cmd_help",
    "cmd_menu",
    "cmd_status",
    "cmd_positions",
    "cmd_signals",
    "cmd_cost",
    "cmd_accuracy",
    "cmd_stop",
    "cmd_resume",
    "cmd_quiet",
    "cmd_sell",
    "cmd_cycle",
    "cmd_about",
    "cmd_logic",
    "cmd_mode",
    "cmd_style",
    "cmd_universe",
    "cmd_update",
    "cmd_notes",
    "cmd_setcreds",
    "cmd_reload",
    "cmd_restart",
    "cmd_funda",
    "cmd_export",
    "cmd_init",
    "cmd_config",
    "cmd_set",
]
