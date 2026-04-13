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
    cmd_menu,
    cmd_positions,
    cmd_quiet,
    cmd_resume,
    cmd_sell,
    cmd_signals,
    cmd_status,
    cmd_stop,
)
from trading_bot.bot.commands_config import cmd_config
from trading_bot.bot.commands_creds import cmd_reload, cmd_restart, cmd_setcreds
from trading_bot.bot.commands_export import cmd_export
from trading_bot.bot.commands_funda import cmd_funda
from trading_bot.bot.commands_init import cmd_init, handle_init_callback
from trading_bot.bot.commands_logs import cmd_logs
from trading_bot.bot.commands_mode import cmd_mode
from trading_bot.bot.commands_universe import (
    _execute_universe_add,
    _execute_universe_remove,
    _universe_remove_preview,
    cmd_universe,
)
from trading_bot.bot.commands_update import cmd_notes, cmd_update
from trading_bot.bot.context import BotContext
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
    ("menu", "메인 메뉴 (버튼 허브)"),
    ("help", "사용법 보기"),
    ("status", "지금 상태 (자산·킬스위치·비용)"),
    ("positions", "갖고 있는 주식"),
    ("signals", "오늘 매매 추천 (최근 10개)"),
    ("accuracy", "AI 판단 적중률 (사후 검증)"),
    ("cost", "오늘 AI 분석 비용"),
    ("mode", "거래 모드 조회/전환 (실전/모의)"),
    ("universe", "추적 종목 목록/추가/제거"),
    ("about", "봇 버전 및 전체 설정"),
    ("stop", "🛑 긴급 정지 (새로 구매 차단)"),
    ("resume", "✅ 긴급 정지 풀기"),
    ("quiet", "🔕 조용 모드 토글 (10분 사이클 요약 끔)"),
    ("sell", "특정 종목 전부 팔기 (확인 필요)"),
    ("cycle", "지금 바로 점검 실행"),
    ("update", "최신 버전 확인"),
    ("setcreds", "텔레그램으로 앱키 직접 교체"),
    ("reload", "자격증명 재로드 (파일 수정 후)"),
    ("restart", "컨테이너 완전 재시작"),
    ("funda", "종목 재무지표 조회/게이트 켜기·끄기"),
    ("export", "📤 데이터 내보내기 (CSV/DB 파일 전송)"),
    ("logs", "📋 서버 로그 조회 (텍스트/파일)"),
    ("init", "🚀 첫 설치자용 설정 마법사"),
    ("config", "⚙️ 설정 파일 진단/확인"),
]


COMMAND_MAP: dict[str, Callable[[BotContext, list[str]], dict[str, Any]]] = {
    "/start": cmd_menu,
    "/help": cmd_help,
    "/menu": cmd_menu,
    "/about": cmd_about,
    "/mode": cmd_mode,
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
}


def handle_command(ctx: BotContext, cmd: str, args: list[str]) -> dict[str, Any] | None:
    handler = COMMAND_MAP.get(cmd.lower())
    if handler is None:
        return _reply(f"모르는 명령어: `{cmd}`\n`/help` 로 목록 보기")
    try:
        return handler(ctx, args)
    except Exception as exc:
        log.exception("커맨드 %s 처리 중 예외", cmd)
        return _reply(f"❌ 처리 실패\n`{type(exc).__name__}: {exc}`")


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
    return _reply(f"모르는 버튼: `{data}`")


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
    "cmd_mode",
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
]
