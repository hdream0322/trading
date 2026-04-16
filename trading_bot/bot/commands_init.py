"""/init 커맨드 — 첫 설치자용 대화형 설정 마법사.

인라인 키보드 + 세션 상태로 자격증명 → 모드 → 유니버스 → 주요 수치 요약 → 완료
까지 단계별로 안내. 자격증명 입력은 poller 가 일반 텍스트를 감지해
`handle_init_text` 로 위임하는 구조 (다른 스텝은 모두 콜백 버튼으로 진행).

세션 상태(INIT_SESSIONS)는 프로세스 메모리에만 유지. 재시작 시 소실되지만
/init 을 다시 시작하면 되므로 파일 영속화는 불필요.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_bot.bot import expiry, mode_switch, runtime_state
from trading_bot.bot.context import BotContext
from trading_bot.bot.formatters import mode_badge
from trading_bot.bot.keyboards import _reply
from trading_bot.config import (
    CREDENTIALS_OVERRIDE_FILE,
    build_trade_cfg,
    is_init_completed,
    load_credentials_override,
    mark_init_completed,
    save_universe_override,
)
from trading_bot.kis.client import KisClient

log = logging.getLogger(__name__)

# chat_id → 세션 dict {"step": str, "collected": {...}, "updated_at": float}
# 자격증명/유니버스 입력 단계에서만 값이 있음.
INIT_SESSIONS: dict[int, dict[str, Any]] = {}

_SESSION_TTL_SEC = 600  # 10분 idle 시 폐기

# 자격증명 수집 스텝 순서
_CRED_STEPS = ("app_key", "app_secret", "account_no")

# 안내/진단 문구에 쓰이는 readable label
_CRED_LABELS = {
    "app_key": "앱키 (APP KEY)",
    "app_secret": "앱 시크릿 (APP SECRET)",
    "account_no": "계좌번호 (8자리 숫자)",
}

# 한 번만 전송하는 첫 사이클 안내 플래그 (data/ 에 파일로 영속화)
_INIT_NOTICE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "init_notice_sent"


def notice_sent_flag() -> bool:
    return _INIT_NOTICE_FILE.exists()


def mark_notice_sent() -> None:
    try:
        _INIT_NOTICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INIT_NOTICE_FILE.write_text(
            datetime.now().isoformat(), encoding="utf-8"
        )
    except Exception:
        log.warning("init_notice_sent 플래그 저장 실패", exc_info=True)


# ─────────────────────────────────────────────────────────────
# 세션 유틸
# ─────────────────────────────────────────────────────────────

def _get_session(chat_id: int) -> dict[str, Any] | None:
    sess = INIT_SESSIONS.get(chat_id)
    if not sess:
        return None
    if time.time() - sess.get("updated_at", 0) > _SESSION_TTL_SEC:
        INIT_SESSIONS.pop(chat_id, None)
        return None
    return sess


def _set_session(chat_id: int, step: str, collected: dict[str, Any] | None = None) -> None:
    existing = INIT_SESSIONS.get(chat_id) or {}
    merged = dict(existing.get("collected") or {})
    if collected:
        merged.update(collected)
    INIT_SESSIONS[chat_id] = {
        "step": step,
        "collected": merged,
        "updated_at": time.time(),
    }


def _clear_session(chat_id: int) -> None:
    INIT_SESSIONS.pop(chat_id, None)


def has_active_session(chat_id: int) -> bool:
    return _get_session(chat_id) is not None


# ─────────────────────────────────────────────────────────────
# 키보드
# ─────────────────────────────────────────────────────────────

def _welcome_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "🚀 설정 시작", "callback_data": "init:start"},
        {"text": "❌ 나중에", "callback_data": "cancel"},
    ]]}


def _creds_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🟡 모의(paper) 자격증명 입력", "callback_data": "init:creds:paper"}],
        [{"text": "🔴 실전(live) 자격증명 입력", "callback_data": "init:creds:live"}],
        [{"text": "⏭️ 건너뛰기 (이미 .env 에 있음)", "callback_data": "init:creds:skip"}],
    ]}


def _mode_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "🟡 모의 모드로 운영 (Recommended)", "callback_data": "init:mode:paper"}],
        [{"text": "🔴 실전 모드로 운영", "callback_data": "init:mode:live"}],
    ]}


def _universe_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "✅ 기본 목록 그대로", "callback_data": "init:universe:keep"}],
        [{"text": "✏️ 내가 직접 고르기", "callback_data": "init:universe:custom"}],
    ]}


def _summary_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ 다음", "callback_data": "init:summary"},
    ]]}


def _finish_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "🏁 마침 (설정 완료)", "callback_data": "init:finish"},
    ]]}


# ─────────────────────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────────────────────

def cmd_init(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    done_line = ""
    if is_init_completed():
        done_line = "\n\n_이미 한 번 완료됐어요. 다시 돌려도 되고, 필요한 스텝만 건너뛰어도 됩니다._"
    text = (
        "👋 *자동매매 봇 설치 마법사*\n\n"
        "처음 설치하셨군요! 5분 안에 설정 마칠게요.\n\n"
        "*순서*\n"
        "1️⃣ KIS 자격증명 (앱키·시크릿·계좌번호)\n"
        "2️⃣ 운영 모드 선택 (모의 / 실전)\n"
        "3️⃣ 추적 종목 (기본 10개 or 커스텀)\n"
        "4️⃣ 주요 수치 확인 (손절/익절/AI 한도)\n"
        "5️⃣ 완료\n\n"
        "중간에 멈춰도 괜찮아요 — `/init` 으로 언제든 다시 시작 가능."
        f"{done_line}"
    )
    return _reply(text, reply_markup=_welcome_keyboard())


# ─────────────────────────────────────────────────────────────
# 콜백 라우터
# ─────────────────────────────────────────────────────────────

def handle_init_callback(ctx: BotContext, chat_id: int, data: str) -> dict[str, Any]:
    """`init:` prefix 콜백 처리. chat_id 는 세션 키로 사용."""
    parts = data.split(":")
    # parts[0] == "init"
    if len(parts) < 2:
        return _reply("❌ 잘못된 콜백")

    sub = parts[1]

    if sub == "start":
        _clear_session(chat_id)
        return _step_creds_intro()

    if sub == "creds":
        if len(parts) < 3:
            return _reply("❌ 잘못된 콜백")
        choice = parts[2]
        if choice == "skip":
            _clear_session(chat_id)
            return _step_mode_intro(ctx)
        if choice in ("paper", "live"):
            _set_session(chat_id, step=f"creds:{choice}:app_key", collected={"creds_mode": choice})
            label = "🟡 모의" if choice == "paper" else "🔴 실전"
            return _reply(
                f"*{label} 자격증명 입력 — 1/3*\n\n"
                f"먼저 *{_CRED_LABELS['app_key']}* 를 보내주세요.\n\n"
                f"_받은 메시지는 보안을 위해 즉시 삭제됩니다._\n"
                f"_취소하려면 아무 버튼 눌러도 되고, 10분 지나면 자동 종료._"
            )
        return _reply(f"❌ 모르는 선택: {choice}")

    if sub == "mode":
        if len(parts) < 3:
            return _reply("❌ 잘못된 콜백")
        target = parts[2]
        if target not in ("paper", "live"):
            return _reply(f"❌ 모르는 모드: {target}")
        return _apply_mode_choice(ctx, target)

    if sub == "universe":
        if len(parts) < 3:
            return _reply("❌ 잘못된 콜백")
        choice = parts[2]
        if choice == "keep":
            _clear_session(chat_id)
            return _step_summary(ctx)
        if choice == "custom":
            _set_session(chat_id, step="universe:custom", collected={})
            return _reply(
                "*추적 종목 커스터마이즈*\n\n"
                "추적할 종목코드를 *공백으로 구분해서* 한 줄로 보내세요.\n\n"
                "예시:\n"
                "`005930 000660 035720 051910`\n\n"
                "_(삼성전자·SK하이닉스·카카오·LG화학)_\n\n"
                "6자리 숫자여야 하고, KIS 에서 이름 조회가 되는 종목만 저장돼요.\n"
                "_취소하려면 다른 커맨드 입력._"
            )
        return _reply(f"❌ 모르는 선택: {choice}")

    if sub == "summary":
        return _step_summary(ctx)

    if sub == "finish":
        return _step_finish()

    return _reply(f"❌ 모르는 콜백: {data}")


# ─────────────────────────────────────────────────────────────
# 텍스트 라우터 (poller 에서 위임)
# ─────────────────────────────────────────────────────────────

def handle_init_text(ctx: BotContext, chat_id: int, text: str) -> dict[str, Any] | None:
    """자격증명/유니버스 입력 대기 중일 때만 응답. 세션이 없으면 None.

    반환값이 None 이면 poller 는 평소처럼 해당 텍스트를 무시하면 됨.
    """
    sess = _get_session(chat_id)
    if not sess:
        return None

    step = sess["step"]

    # 자격증명 수집: "creds:<mode>:<field>"
    if step.startswith("creds:"):
        return _handle_creds_text(ctx, chat_id, sess, text)

    # 유니버스 커스텀 입력
    if step == "universe:custom":
        return _handle_universe_custom_text(ctx, chat_id, text)

    # 모르는 스텝
    return None


def _handle_creds_text(
    ctx: BotContext, chat_id: int, sess: dict[str, Any], text: str
) -> dict[str, Any]:
    _, mode, field = sess["step"].split(":", 2)
    value = text.strip()

    # 간단한 형식 검증 (setcreds 와 동일 기준)
    err = _validate_cred_field(field, value)
    if err:
        return _reply(
            f"❌ {err}\n\n"
            f"다시 *{_CRED_LABELS[field]}* 를 보내주세요.\n"
            f"_원본 메시지는 삭제됐습니다._",
            delete_original=True,
        )

    collected = dict(sess.get("collected") or {})
    collected[field] = value

    idx = _CRED_STEPS.index(field)
    if idx + 1 < len(_CRED_STEPS):
        next_field = _CRED_STEPS[idx + 1]
        _set_session(chat_id, step=f"creds:{mode}:{next_field}", collected=collected)
        label = "🟡 모의" if mode == "paper" else "🔴 실전"
        return _reply(
            f"*{label} 자격증명 입력 — {idx + 2}/3*\n\n"
            f"이제 *{_CRED_LABELS[next_field]}* 를 보내주세요.",
            delete_original=True,
        )

    # 마지막 필드 수집 완료 → 저장
    _clear_session(chat_id)
    try:
        result_msg = _apply_credentials(ctx, mode, collected)
    except Exception as exc:
        log.exception("init 자격증명 저장 실패")
        return _reply(
            f"❌ *자격증명 저장 실패*\n`{type(exc).__name__}: {exc}`\n\n"
            f"`/init` 다시 실행하거나 `/setcreds` 로 재시도하세요.",
            delete_original=True,
        )

    return _reply(
        result_msg + "\n\n다음 스텝: *운영 모드 선택*",
        reply_markup=_mode_keyboard(),
        delete_original=True,
    )


def _handle_universe_custom_text(
    ctx: BotContext, chat_id: int, text: str
) -> dict[str, Any]:
    codes = [c.strip() for c in text.split() if c.strip()]
    if not codes:
        return _reply("❌ 종목코드가 비어있어요. 공백으로 구분된 6자리 숫자를 보내주세요.")

    # 다른 슬래시 커맨드로 취소 가능 — 슬래시 시작이면 poller 가 이미 라우팅했을 것이므로 여기선 무시
    valid: list[dict[str, str]] = []
    invalid: list[str] = []
    failed: list[tuple[str, str]] = []

    for code in codes:
        if not (len(code) == 6 and code.isdigit()):
            invalid.append(code)
            continue
        try:
            name = ctx.kis.get_stock_name(code)
        except Exception as exc:
            failed.append((code, str(exc)[:60]))
            continue
        entry: dict[str, str] = {"code": code, "name": name}
        try:
            sector = ctx.kis.get_stock_sector(code)
            if sector:
                entry["sector"] = sector
        except Exception:
            pass
        valid.append(entry)

    if not valid:
        _clear_session(chat_id)
        reasons = []
        if invalid:
            reasons.append(f"형식 오류: {', '.join(invalid)}")
        if failed:
            reasons.append(f"조회 실패: {', '.join(c for c, _ in failed)}")
        return _reply(
            "❌ 저장할 종목이 하나도 없어요.\n\n"
            + ("\n".join(f"• {r}" for r in reasons) if reasons else "")
            + "\n\n`/init` 으로 다시 시도하거나, `/universe add 005490` 처럼 하나씩 추가도 가능."
        )

    try:
        save_universe_override(valid)
    except Exception as exc:
        log.exception("init universe 저장 실패")
        return _reply(f"❌ 저장 실패\n`{exc}`")

    ctx.settings.universe = valid
    _clear_session(chat_id)

    lines = [f"✅ *추적 종목 {len(valid)}개 저장됨*", ""]
    for item in valid:
        sec = f" · _{item['sector']}_" if item.get("sector") else ""
        lines.append(f"- {item['name']} (`{item['code']}`){sec}")
    if invalid:
        lines.append("")
        lines.append(f"⚠️ 형식 오류 무시: {', '.join(invalid)}")
    if failed:
        lines.append(f"⚠️ 조회 실패 무시: {', '.join(c for c, _ in failed)}")
    lines.append("")
    lines.append("다음 스텝: *주요 수치 확인*")

    return _reply("\n".join(lines), reply_markup=_summary_keyboard())


# ─────────────────────────────────────────────────────────────
# 각 스텝 구현
# ─────────────────────────────────────────────────────────────

def _step_creds_intro() -> dict[str, Any]:
    has_override = CREDENTIALS_OVERRIDE_FILE.exists()
    status_line = (
        "_(현재 `data/credentials.env` 가 이미 존재 — 덮어쓰려면 새로 입력)_"
        if has_override
        else "_(아직 자격증명 오버라이드 없음)_"
    )
    text = (
        "*1️⃣ KIS 자격증명*\n\n"
        f"{status_line}\n\n"
        "이미 `.env` 또는 `data/credentials.env` 에 키를 넣어뒀으면 *건너뛰기*.\n"
        "텔레그램으로 입력하면 `data/credentials.env` 에 자동 저장돼요.\n\n"
        "_실전 키는 실제 돈이 움직이니 신중히._"
    )
    return _reply(text, reply_markup=_creds_keyboard())


def _validate_cred_field(field: str, value: str) -> str | None:
    if field == "app_key":
        if not (10 <= len(value) <= 64):
            return f"앱키 길이 이상: {len(value)}자 (보통 36자)"
        return None
    if field == "app_secret":
        if not (40 <= len(value) <= 256):
            return f"시크릿 길이 이상: {len(value)}자 (보통 180자)"
        return None
    if field == "account_no":
        if not value.isdigit() or not (8 <= len(value) <= 10):
            return f"계좌번호 형식 이상: `{value}` (8자리 숫자 예상)"
        return None
    return "알 수 없는 필드"


def _apply_credentials(ctx: BotContext, mode: str, collected: dict[str, str]) -> str:
    """commands_creds.cmd_setcreds 의 저장/반영 로직을 재사용 (인자 재조립).

    중요한 부작용은 cmd_setcreds 와 동일:
      - credentials.env 에 병합 쓰기 (다른 모드 키 보존)
      - 현재 활성 모드와 일치하면 KisClient 교체 + 토큰 캐시 삭제
      - paper 면 expiry 카운트다운 리셋
      - runtime_state.credentials_last_mtime 갱신 (watcher 중복 방지)
    """
    app_key = collected["app_key"]
    app_secret = collected["app_secret"]
    account = collected["account_no"]

    # 기존 파일 병합
    existing: dict[str, str] = {}
    if CREDENTIALS_OVERRIDE_FILE.exists():
        for line in CREDENTIALS_OVERRIDE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v

    prefix = "KIS_LIVE" if mode == "live" else "KIS_PAPER"
    existing[f"{prefix}_APP_KEY"] = app_key
    existing[f"{prefix}_APP_SECRET"] = app_secret
    existing[f"{prefix}_ACCOUNT_NO"] = account
    existing.setdefault(f"{prefix}_ACCOUNT_PRODUCT_CD", "01")

    CREDENTIALS_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CREDENTIALS_OVERRIDE_FILE.open("w", encoding="utf-8") as f:
        f.write("# /init 또는 /setcreds 로 관리되는 자격증명 오버라이드\n")
        f.write("# 이 파일이 있으면 .env 의 KIS_* 값을 덮어씀\n\n")
        for k in sorted(existing.keys()):
            f.write(f"{k}={existing[k]}\n")
    try:
        CREDENTIALS_OVERRIDE_FILE.chmod(0o600)
    except OSError:
        pass
    try:
        runtime_state.credentials_last_mtime = CREDENTIALS_OVERRIDE_FILE.stat().st_mtime
    except OSError:
        pass

    applied_now = False
    load_credentials_override()
    if ctx.settings.kis.mode == mode:
        new_cfg = build_trade_cfg(mode)
        with ctx.trading_lock:
            old_kis = ctx.kis
            new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
            ctx.settings.kis = new_cfg
            ctx.kis = new_kis
            try:
                old_kis.close()
            except Exception:
                pass
        # 토큰 캐시 삭제
        from pathlib import Path
        tokens_dir = Path(__file__).resolve().parent.parent.parent / "tokens"
        try:
            token_file = tokens_dir / f"kis_token_{mode}.json"
            if token_file.exists():
                token_file.unlink()
        except OSError:
            pass
        if mode == "paper":
            expiry.mark_updated()
        applied_now = True

    badge = mode_badge(mode)
    status_line = (
        "✅ 즉시 반영됨"
        if applied_now
        else f"💾 파일에 저장됨 (현재 활성 모드는 `{ctx.settings.kis.mode}` 이라 대기)"
    )
    return (
        f"✅ *자격증명 저장 완료* {badge}\n\n"
        f"계좌: `{account}`\n"
        f"앱키 앞 12자: `{app_key[:12]}...`\n"
        f"{status_line}"
    )


def _step_mode_intro(ctx: BotContext) -> dict[str, Any]:
    current = ctx.settings.kis.mode
    badge = mode_badge(current)
    text = (
        "*2️⃣ 운영 모드 선택*\n\n"
        f"지금 모드: {badge}\n\n"
        "🟡 *모의(paper)* — 가상 거래, 실제 돈 안 움직임. 처음엔 꼭 여기서 시작.\n"
        "🔴 *실전(live)* — 실제 돈이 움직임. 리스크 안전장치 있어도 신중히.\n\n"
        "_나중에 `/mode` 로 언제든 바꿀 수 있어요._"
    )
    return _reply(text, reply_markup=_mode_keyboard())


def _apply_mode_choice(ctx: BotContext, target: str) -> dict[str, Any]:
    current = ctx.settings.kis.mode
    if target == current:
        mode_switch.write_override(target)  # 오버라이드 영속화 (재시작 보존)
        return _reply(
            f"ℹ️ 이미 `{target}` 모드로 동작 중 _(오버라이드 파일에 명시 저장)_\n\n"
            + _step_universe_intro_text(ctx),
            reply_markup=_universe_keyboard(),
        )

    # 대상 모드 키 확인
    try:
        new_cfg = build_trade_cfg(target)
    except RuntimeError as exc:
        prefix = "KIS_LIVE" if target == "live" else "KIS_PAPER"
        return _reply(
            f"❌ `{target}` 모드 키가 없습니다\n`{exc}`\n\n"
            f"`/init` 다시 실행해서 먼저 *{target}* 자격증명을 입력하거나,\n"
            f"`.env` 에 `{prefix}_APP_KEY` 등을 직접 넣어주세요."
        )

    try:
        with ctx.trading_lock:
            old_kis = ctx.kis
            new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
            ctx.settings.kis = new_cfg
            ctx.kis = new_kis
            try:
                old_kis.close()
            except Exception:
                pass
        mode_switch.write_override(target)
    except Exception as exc:
        log.exception("init 모드 전환 실패")
        return _reply(f"❌ 모드 전환 실패\n`{exc}`")

    badge = mode_badge(target)
    warn = ""
    if target == "live":
        warn = (
            "\n\n⚠️ 지금부터 *실전* 입니다. 다음 점검부터 실제 주문이 들어가요.\n"
            "불안하면 즉시 `/stop` 으로 긴급 정지 가능."
        )
    return _reply(
        f"✅ *모드 설정 완료*\n\n"
        f"거래: {badge}\n"
        f"계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`{warn}\n\n"
        + _step_universe_intro_text(ctx),
        reply_markup=_universe_keyboard(),
    )


def _step_universe_intro_text(ctx: BotContext) -> str:
    universe = ctx.settings.universe or []
    count = len(universe)
    preview = ", ".join(item["name"] for item in universe[:5])
    if count > 5:
        preview += f" 외 {count - 5}개"
    return (
        "*3️⃣ 추적 종목*\n\n"
        f"지금 기본 목록: *{count}개*\n"
        f"_{preview or '(비어있음)'}_\n\n"
        "그대로 둘지, 직접 커스터마이즈할지 고르세요.\n\n"
        "_`/universe add 005490` 또는 `/universe remove` 로 나중에 개별 수정도 가능._"
    )


def _step_summary(ctx: BotContext) -> dict[str, Any]:
    s = ctx.settings
    risk = s.risk or {}
    llm = s.llm or {}
    exit_rules = s.exit_rules or {}

    def _pct(d: dict, key: str, sign: str = "") -> str:
        """dict 에서 값을 꺼내 '±N%' 형태로. 없으면 '미설정'."""
        v = d.get(key)
        if v is None:
            return "_미설정_"
        return f"`{sign}{v}%`"

    def _val(d: dict, key: str, suffix: str = "") -> str:
        v = d.get(key)
        if v is None:
            return "_미설정_"
        return f"`{v}{suffix}`"

    try:
        conf_pct = f"`{int(float(llm.get('confidence_threshold', 0)) * 100)}%`"
    except (TypeError, ValueError):
        conf_pct = "_미설정_"

    cost_limit = llm.get("daily_cost_limit_usd")
    cost_line = f"`${cost_limit}`" if cost_limit is not None else "_미설정_"

    lines = [
        "*4️⃣ 주요 수치 확인*",
        "",
        "_현재 `config/settings.yaml` 값입니다._",
        "",
        "*리스크*",
        f"- 1일 손실 한도: {_pct(risk, 'daily_loss_limit_pct')}",
        f"- 동시 보유 최대 종목수: {_val(risk, 'max_concurrent_positions')}",
        f"- 종목당 비중 상한: {_pct(risk, 'max_position_per_symbol_pct')}",
        "",
        "*청산 규칙*",
        f"- 손절: {_pct(exit_rules, 'stop_loss_pct', sign='-')}",
        f"- 익절: {_pct(exit_rules, 'take_profit_pct', sign='+')}",
        f"- 트레일링 발동: {_pct(exit_rules, 'trailing_activation_pct', sign='+')}",
        "",
        "*AI*",
        f"- 모델: `{llm.get('model', '?')}`",
        f"- 확신도 임계값: {conf_pct}",
        f"- 일일 비용 한도: {cost_line}",
        "",
        "_값이 `미설정` 으로 보이면 `config/settings.yaml` 의 해당 섹션을 확인하세요._",
        "_수치 변경은 파일 수정 후 `/restart` 또는 `/reload`._",
    ]
    return _reply("\n".join(lines), reply_markup=_finish_keyboard())


def _step_finish() -> dict[str, Any]:
    try:
        mark_init_completed()
        mark_notice_sent()
    except Exception as exc:
        log.warning("init_completed 플래그 저장 실패: %s", exc)

    return _reply(
        "🎉 *설치 마법사 완료*\n\n"
        "이제 봇이 10분 주기로 자동 점검을 돕기 시작해요.\n\n"
        "*자주 쓰는 커맨드*\n"
        "• `/status` — 지금 상태\n"
        "• `/positions` — 내 주식\n"
        "• `/signals` — 오늘 판단 결과\n"
        "• `/stop` — 🛑 긴급 정지\n"
        "• `/menu` — 버튼 허브\n\n"
        "전체 커맨드는 `/help`.\n\n"
        "_설정을 다시 하려면 언제든 `/init`._"
    )


# /init 콜백이 universe:keep 을 처리할 때 summary 로 바로 가도록 라우팅은 위에서 처리.
# 추적 종목 스텝 진입은 _apply_mode_choice 의 응답 reply_markup 으로 연결됨.
# 자격증명 → 모드 전환 메시지도 _handle_creds_text 에서 _mode_keyboard 붙여 연결.
