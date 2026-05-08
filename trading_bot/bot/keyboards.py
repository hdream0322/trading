"""텔레그램 응답 빌더 + inline 키보드 정의."""
from __future__ import annotations

from typing import Any


def _reply(
    text: str,
    reply_markup: dict[str, Any] | None = None,
    delete_original: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {"text": text}
    if reply_markup is not None:
        out["reply_markup"] = reply_markup
    if delete_original:
        out["delete_original"] = True
    return out


def cycle_summary_keyboard(kill_active: bool = False) -> dict[str, Any]:
    """점검 결과 메시지 하단 퀵 액션 버튼.

    긴급 정지 상태에 따라 한 버튼만 토글로 표시.
    """
    kill_button = (
        {"text": "✅ 긴급 정지 해제", "callback_data": "resume"}
        if kill_active
        else {"text": "🛑 긴급 정지", "callback_data": "kill"}
    )
    return {
        "inline_keyboard": [
            [
                kill_button,
                {"text": "🤖 오늘 추천", "callback_data": "go:signals"},
            ],
            [
                {"text": "📊 보유 주식", "callback_data": "positions"},
                {"text": "💰 상태", "callback_data": "status"},
            ],
            [
                {"text": "🔄 지금 점검", "callback_data": "cycle_run"},
            ],
        ]
    }


def _sell_picker_keyboard(holdings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """보유 종목을 각각 버튼으로 만들어 판매 대상 선택 화면."""
    rows: list[list[dict[str, str]]] = []
    for code, p in holdings.items():
        qty = int(p["qty"])
        pnl_pct = float(p["pnl_pct"])
        rows.append([{
            "text": f"{p['name']} {qty}주 ({pnl_pct:+.1f}%)",
            "callback_data": f"sell_select:{code}",
        }])
    rows.append([{"text": "❌ 취소", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def _positions_sell_keyboard(holdings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """/positions 응답 하단에 붙는 종목별 판매 버튼."""
    rows: list[list[dict[str, str]]] = []
    for code, p in holdings.items():
        rows.append([{
            "text": f"💸 {p['name']} 판매",
            "callback_data": f"sell_select:{code}",
        }])
    return {"inline_keyboard": rows}


def _universe_remove_picker_keyboard(universe: list[dict[str, str]]) -> dict[str, Any]:
    """/universe remove (인자 없음) → 추적 종목 각각을 제거 버튼으로."""
    rows: list[list[dict[str, str]]] = []
    for item in universe:
        rows.append([{
            "text": f"❌ {item['name']}",
            "callback_data": f"universe_rm_pick:{item['code']}",
        }])
    rows.append([{"text": "취소", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def _mode_switch_keyboard(current_mode: str) -> dict[str, Any]:
    """/mode (인자 없음) → 반대 모드로 전환 버튼."""
    if current_mode == "paper":
        btn = {"text": "🔴 실전으로 전환", "callback_data": "mode_to:live"}
    else:
        btn = {"text": "🟡 모의로 전환", "callback_data": "mode_to:paper"}
    return {"inline_keyboard": [[btn]]}


def _mode_live_confirm_keyboard() -> dict[str, Any]:
    """실전 전환 경고 화면에 붙는 확정/취소 버튼."""
    return {
        "inline_keyboard": [[
            {"text": "🚨 실전 전환 확정", "callback_data": "mode_confirm_live"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


def _menu_keyboard(kill_active: bool) -> dict[str, Any]:
    """/menu 메인 허브 — 5개 카테고리로 묶음."""
    safety_btn = (
        {"text": "🛡️ 안전 (정지 풀기)", "callback_data": "hub:safety"}
        if kill_active
        else {"text": "🛡️ 안전", "callback_data": "hub:safety"}
    )
    return {
        "inline_keyboard": [
            [
                {"text": "📊 현황", "callback_data": "hub:status"},
                {"text": "💸 거래", "callback_data": "hub:trade"},
            ],
            [
                {"text": "⚙️ 설정", "callback_data": "hub:settings"},
                safety_btn,
            ],
            [
                {"text": "🔄 운영·도구", "callback_data": "hub:ops"},
                {"text": "ℹ️ 사용법", "callback_data": "help"},
            ],
        ]
    }


def hub_section_keyboard(section: str, kill_active: bool = False) -> dict[str, Any]:
    """카테고리 sub-menu — 각 항목이 실제 커맨드로 deep-link, 마지막 줄 '🏠 처음으로'."""
    rows: list[list[dict[str, str]]]
    if section == "status":
        rows = [
            [{"text": "💰 계좌 상태", "callback_data": "status"},
             {"text": "📈 보유 종목", "callback_data": "positions"}],
            [{"text": "🤖 오늘 추천", "callback_data": "go:signals"},
             {"text": "✅ AI 적중률", "callback_data": "go:accuracy"}],
            [{"text": "💵 AI 비용", "callback_data": "go:cost"},
             {"text": "📋 봇 정보", "callback_data": "go:about"}],
            [{"text": "🧠 매매 로직", "callback_data": "go:logic"}],
        ]
    elif section == "trade":
        rows = [
            [{"text": "🔄 지금 점검", "callback_data": "cycle_run"}],
            [{"text": "💸 종목 판매", "callback_data": "go:sell"},
             {"text": "🌐 추적 종목", "callback_data": "universe_list"}],
            [{"text": "📊 펀더멘털 게이트", "callback_data": "go:funda"}],
        ]
    elif section == "settings":
        rows = [
            [{"text": "⚡ 거래 스타일", "callback_data": "go:style"},
             {"text": "🟡 거래 모드", "callback_data": "go:mode"}],
            [{"text": "📐 설정 진단", "callback_data": "go:config"},
             {"text": "🛠️ 값 변경", "callback_data": "go:set"}],
        ]
    elif section == "safety":
        toggle_kill = (
            {"text": "✅ 긴급정지 풀기", "callback_data": "resume"}
            if kill_active
            else {"text": "🛑 긴급 정지", "callback_data": "kill"}
        )
        rows = [
            [toggle_kill],
            [{"text": "🔕 조용 모드", "callback_data": "go:quiet"}],
            [{"text": "🔄 컨테이너 재시작", "callback_data": "go:restart"},
             {"text": "🔁 자격증명 재로드", "callback_data": "go:reload"}],
        ]
    elif section == "ops":
        rows = [
            [{"text": "⬆️ 업데이트 확인", "callback_data": "go:update"}],
            [{"text": "📤 내보내기", "callback_data": "go:export"},
             {"text": "📋 로그 보기", "callback_data": "go:logs"}],
            [{"text": "🔑 자격증명 교체", "callback_data": "go:setcreds"}],
            [{"text": "🚀 첫 설치 마법사", "callback_data": "go:init"}],
        ]
    else:
        rows = []
    rows.append([{"text": "🏠 처음으로", "callback_data": "hub:main"}])
    return {"inline_keyboard": rows}


def _sell_confirm_keyboard(code: str, name: str, qty: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": f"✅ {name} {qty}주 판매 확정", "callback_data": f"sell_confirm:{code}"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


def update_action_keyboard() -> dict[str, Any]:
    """/update 응답에서 새 버전이 있을 때 하단에 붙는 액션 버튼."""
    return {
        "inline_keyboard": [[
            {"text": "🔄 업데이트", "callback_data": "update_confirm"},
            {"text": "✋ 현재 버전 유지", "callback_data": "update_skip"},
        ]]
    }


def quiet_toggle_keyboard(active: bool) -> dict[str, Any]:
    """/quiet (인자 없음) 응답 하단 토글 버튼."""
    if active:
        btn = {"text": "🔔 일반 모드로 (10분 요약 받기)", "callback_data": "quiet_off"}
    else:
        btn = {"text": "🔕 조용 모드로 (요약 끄기)", "callback_data": "quiet_on"}
    return {"inline_keyboard": [[btn]]}


def kill_toggle_keyboard(active: bool) -> dict[str, Any]:
    """/stop · /resume 응답 하단 상호 토글 버튼."""
    if active:
        btn = {"text": "✅ 긴급 정지 풀기", "callback_data": "resume"}
    else:
        btn = {"text": "🛑 긴급 정지", "callback_data": "kill"}
    return {"inline_keyboard": [[btn]]}


def funda_toggle_keyboard(active: bool) -> dict[str, Any]:
    """/funda (인자 없음) 응답 하단 토글 버튼."""
    if active:
        btn = {"text": "📊 재무사항 고려하지 않음", "callback_data": "funda_off"}
    else:
        btn = {"text": "📊 재무사항 고려", "callback_data": "funda_on"}
    return {"inline_keyboard": [[btn]]}


def update_auto_toggle_keyboard(enabled: bool) -> dict[str, Any]:
    """/update status 응답 하단 자동 업데이트 토글."""
    if enabled:
        btn = {"text": "🛑 자동 업데이트 끄기", "callback_data": "update_auto_off"}
    else:
        btn = {"text": "✅ 자동 업데이트 켜기", "callback_data": "update_auto_on"}
    return {"inline_keyboard": [[btn]]}


def restart_confirm_keyboard() -> dict[str, Any]:
    """/restart 응답 하단 확정/취소 버튼."""
    return {
        "inline_keyboard": [[
            {"text": "🔄 재시작 확정", "callback_data": "restart_confirm"},
            {"text": "❌ 취소", "callback_data": "cancel"},
        ]]
    }


def export_menu_keyboard() -> dict[str, Any]:
    """/export (인자 없음) 응답 하단의 내보내기 종류 선택 버튼."""
    return {
        "inline_keyboard": [
            [
                {"text": "📋 오늘 점검 전체", "callback_data": "export_signals"},
                {"text": "🔍 1차 미달 근접", "callback_data": "export_nearmiss"},
            ],
            [
                {"text": "🧾 주문 7일", "callback_data": "export_orders"},
                {"text": "🐞 에러 3일", "callback_data": "export_errors"},
            ],
            [
                {"text": "🗄️ DB 통째로", "callback_data": "export_db"},
            ],
        ]
    }


def _universe_confirm_keyboard(action: str, code: str) -> dict[str, Any]:
    """action: 'add' 또는 'remove'."""
    verb = "추가" if action == "add" else "제거"
    return {
        "inline_keyboard": [[
            {"text": f"✅ 예, {verb}할게요", "callback_data": f"universe_{action}:{code}"},
            {"text": "❌ 아니요", "callback_data": "cancel"},
        ]]
    }
