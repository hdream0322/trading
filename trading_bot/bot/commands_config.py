"""/config 커맨드 — 실제 로드된 설정 파일(settings.yaml) 확인.

/about 은 요약이지만 /config 는 **실제 디스크 위 파일** 과 런타임 파싱 상태를
확인하기 위한 진단용. NAS bind mount 구조상 이미지 업데이트로는 settings.yaml
이 갱신되지 않아 호스트 파일 내용이 repo 와 어긋날 수 있는데, 이 커맨드로
실제 값/경로/섹션 존재 여부를 텔레그램에서 바로 확인한다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
import yaml

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply
from trading_bot.config import ROOT, load_settings

log = logging.getLogger(__name__)

SETTINGS_PATH = ROOT / "config" / "settings.yaml"

# 이미지에 Dockerfile 이 복사해둔 기본 설정 스냅샷. bind mount 로 덮이지 않음.
DEFAULTS_PATH = ROOT / "config_defaults" / "settings.yaml"

# 폴백용 — config_defaults 가 없는 구버전 이미지에선 github raw 에서 받음
DEFAULTS_URL = (
    "https://raw.githubusercontent.com/hdream0322/trading/main/config/settings.yaml"
)

# 텔레그램 메시지 한도(4096) 안에서 안전하게 보낼 수 있는 최대 크기
_INLINE_LIMIT = 3500


def cmd_config(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """사용법:
      /config             — 핵심 섹션 요약 + 누락 키 체크
      /config file        — settings.yaml 원본을 파일로 전송
      /config raw         — 원본을 텍스트로 전송 (크면 자동 파일 전환)
      /config reset       — 기본값 복원 확인 버튼 표시
      /config reset confirm — 즉시 복원 실행
    """
    sub = args[0].lower() if args else ""

    if sub == "file":
        return _reply_raw_as_file()
    if sub == "raw":
        return _reply_raw_as_text_or_file()
    if sub == "reset":
        confirm = len(args) > 1 and args[1].lower() == "confirm"
        if confirm:
            return _do_reset(ctx)
        return _reset_preview()

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
        lines.append(f"⚠️ 누락된 키 {missing_total}개 — `/config reset` 으로 기본값 복원 가능")
    else:
        lines.append("✅ 필수 키 모두 존재")

    lines.append("")
    lines.append("_원본 보기: `/config raw` (텍스트) · `/config file` (파일 첨부)_")
    lines.append("_기본값으로 되돌리기: `/config reset`_")

    return _reply("\n".join(lines))


# ─────────────────────────────────────────────────────────────
# /config reset — 기본값 복원
# ─────────────────────────────────────────────────────────────

def _reset_confirm_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ 기본값으로 복원", "callback_data": "config:reset:confirm"},
        {"text": "❌ 취소", "callback_data": "cancel"},
    ]]}


def _reset_preview() -> dict[str, Any]:
    source, _ = _read_defaults()
    src_label = (
        f"이미지 번들 기본값 (`{DEFAULTS_PATH.relative_to(ROOT)}`)"
        if source == "image"
        else f"GitHub main 브랜치 ({DEFAULTS_URL})"
        if source == "github"
        else "기본값을 찾을 수 없음"
    )
    warn = ""
    if source is None:
        warn = "\n\n⚠️ 기본값 소스를 찾지 못했습니다. 인터넷 연결 또는 이미지 버전 확인 필요."
    return _reply(
        "*⚠️ 설정 파일을 기본값으로 되돌리기*\n\n"
        "다음 동작이 실행돼요:\n"
        f"1️⃣ 현재 파일 자동 백업 (`settings.yaml.bak.TIMESTAMP`)\n"
        f"2️⃣ {src_label} 으로 덮어쓰기\n"
        f"3️⃣ 파싱 검증 후 런타임 재로드 (재시작 없이 적용)\n\n"
        "*사라지는 것*: 지금까지 `/set` 이나 SSH 로 편집한 모든 커스텀 값.\n"
        "_백업 파일은 `config/` 안에 남아서 언제든 복구 가능._"
        f"{warn}",
        reply_markup=_reset_confirm_keyboard(),
    )


def _read_defaults() -> tuple[str | None, str]:
    """기본 설정 텍스트와 소스 표시를 반환.

    Returns:
      ("image", text) — 이미지 번들 config_defaults/settings.yaml 사용
      ("github", text) — github raw URL 에서 받음
      (None, "") — 둘 다 실패
    """
    if DEFAULTS_PATH.exists():
        try:
            return "image", DEFAULTS_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("config_defaults 읽기 실패: %s", exc)
    try:
        resp = httpx.get(DEFAULTS_URL, timeout=10.0)
        resp.raise_for_status()
        return "github", resp.text
    except Exception as exc:
        log.warning("github raw defaults 가져오기 실패: %s", exc)
        return None, ""


def _do_reset(ctx: BotContext) -> dict[str, Any]:
    source, default_text = _read_defaults()
    if source is None:
        return _reply(
            "❌ 기본값 소스를 찾지 못했습니다\n\n"
            f"- 이미지 번들(`{DEFAULTS_PATH}`) 없음\n"
            f"- GitHub raw ({DEFAULTS_URL}) 접근 실패\n\n"
            "NAS 에서 직접 `curl` 하거나 `scp` 로 파일 덮어쓰기."
        )

    # 검증: 문법 + 필수 섹션 존재
    try:
        parsed = yaml.safe_load(default_text) or {}
    except yaml.YAMLError as exc:
        return _reply(f"❌ 기본값 YAML 파싱 실패\n`{exc}`")
    for required in ("risk", "exit", "llm", "prefilter", "universe"):
        if required not in parsed:
            return _reply(
                f"❌ 기본값 파일이 이상합니다 — `{required}` 섹션 누락\n"
                f"소스: `{source}`"
            )

    # 백업
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SETTINGS_PATH.with_suffix(f".yaml.bak.{ts}")
    try:
        if SETTINGS_PATH.exists():
            backup_path.write_text(
                SETTINGS_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
    except OSError as exc:
        return _reply(f"❌ 백업 실패 (쓰기 권한 문제일 가능성)\n`{exc}`")

    # 덮어쓰기
    try:
        SETTINGS_PATH.write_text(default_text, encoding="utf-8")
    except OSError as exc:
        return _reply(
            f"❌ 파일 쓰기 실패\n`{exc}`\n\n"
            f"💡 docker-compose.yml 의 `config` 볼륨이 `:ro` 로 마운트된 상태일 수 있어요. "
            f"NAS 에서 `:ro` 제거 후 `docker compose up -d --force-recreate trading-bot` 필요."
        )

    # 런타임 재로드
    runtime_note = ""
    try:
        new_settings = load_settings()
        # 변경 가능한 필드들만 ctx.settings 에 반영 (credentials/telegram 은 유지)
        ctx.settings.universe = new_settings.universe
        ctx.settings.cycle_minutes = new_settings.cycle_minutes
        ctx.settings.market_open = new_settings.market_open
        ctx.settings.market_close = new_settings.market_close
        ctx.settings.risk = new_settings.risk
        ctx.settings.llm = new_settings.llm
        ctx.settings.prefilter = new_settings.prefilter
        ctx.settings.exit_rules = new_settings.exit_rules
        ctx.settings.rate_limit = new_settings.rate_limit
        ctx.settings.fundamentals = new_settings.fundamentals
    except Exception as exc:
        log.exception("기본값 복원 후 런타임 재로드 실패")
        runtime_note = (
            f"\n\n⚠️ 파일 저장은 됐지만 런타임 재로드 실패 (`{exc}`). "
            f"`/restart` 로 완전 재기동 권장."
        )

    src_label = "이미지 번들" if source == "image" else "GitHub main"
    return _reply(
        f"✅ *기본값으로 복원 완료*\n\n"
        f"소스: {src_label}\n"
        f"백업: `{backup_path.name}`\n"
        f"파일 크기: `{len(default_text):,}` bytes\n\n"
        f"런타임 반영 완료. 다음 점검부터 기본값으로 동작.{runtime_note}\n\n"
        f"_복구 필요 시 `{backup_path.name}` 참고._"
    )


def handle_config_callback(ctx: BotContext, data: str) -> dict[str, Any]:
    """`config:` prefix 콜백 라우팅."""
    if data == "config:reset:confirm":
        return _do_reset(ctx)
    return _reply(f"❌ 모르는 config 콜백: {data}")
