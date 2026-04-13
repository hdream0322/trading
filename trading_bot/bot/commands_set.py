"""/set 커맨드 — 화이트리스트 키에 한해 settings.yaml 값을 텔레그램에서 수정.

원칙:
- 편집 가능한 키만 WHITELIST 에 정의 (타입·범위·섹션·설명).
- YAML 전체 편집은 허용하지 않음 (문법 깨짐·주석 소실 방지).
- 파일은 line 단위 정밀 치환 — 주석·공백·나머지 값 보존.
- 쓰기 전 자동 백업(`.bak.TIMESTAMP`), 쓰기 후 재파싱으로 검증.
- 확정 버튼(2단계) 후에만 실제 적용. 현재 실행 중 ctx.settings 에도 즉시 반영.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply
from trading_bot.config import ROOT

log = logging.getLogger(__name__)

SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def _to_bool(s: str) -> bool:
    low = s.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    raise ValueError("true/false 형식 필요")


class Spec:
    __slots__ = ("section", "key", "cast", "type_label", "min", "max", "desc", "runtime_field")

    def __init__(
        self,
        section: str | None,
        key: str,
        cast: Callable[[str], Any],
        type_label: str,
        desc: str,
        runtime_field: str,
        min_: float | None = None,
        max_: float | None = None,
    ):
        self.section = section  # None 이면 top-level
        self.key = key
        self.cast = cast
        self.type_label = type_label
        self.min = min_
        self.max = max_
        self.desc = desc
        # ctx.settings 에 반영할 필드명 (dict 섹션명 또는 dataclass 필드명)
        self.runtime_field = runtime_field


# path 형식: "section.key" 또는 "key" (top-level)
WHITELIST: dict[str, Spec] = {
    # 리스크
    "risk.daily_loss_limit_pct": Spec("risk", "daily_loss_limit_pct", float, "숫자(%)", "1일 손실 한도", "risk", 0.5, 20),
    "risk.max_concurrent_positions": Spec("risk", "max_concurrent_positions", int, "정수", "동시 보유 최대 종목", "risk", 1, 20),
    "risk.max_position_per_symbol_pct": Spec("risk", "max_position_per_symbol_pct", float, "숫자(%)", "종목당 비중 상한", "risk", 1, 100),
    "risk.max_orders_per_day": Spec("risk", "max_orders_per_day", int, "정수", "일일 주문 건수 한도", "risk", 1, 50),
    "risk.cooldown_minutes": Spec("risk", "cooldown_minutes", int, "정수(분)", "재진입 대기", "risk", 0, 1440),
    "risk.max_per_sector": Spec("risk", "max_per_sector", int, "정수", "섹터당 최대 보유", "risk", 1, 20),
    # 청산
    "exit.stop_loss_pct": Spec("exit", "stop_loss_pct", float, "숫자(%)", "손절", "exit_rules", 0.5, 50),
    "exit.take_profit_pct": Spec("exit", "take_profit_pct", float, "숫자(%)", "익절", "exit_rules", 0.5, 200),
    "exit.trailing_activation_pct": Spec("exit", "trailing_activation_pct", float, "숫자(%)", "트레일링 발동", "exit_rules", 0, 100),
    "exit.trailing_distance_pct": Spec("exit", "trailing_distance_pct", float, "숫자(%)", "트레일링 낙폭", "exit_rules", 0.5, 50),
    "exit.atr_enabled": Spec("exit", "atr_enabled", _to_bool, "true/false", "ATR 동적 손절", "exit_rules"),
    "exit.atr_multiplier": Spec("exit", "atr_multiplier", float, "숫자", "ATR 배수", "exit_rules", 0.5, 5),
    # AI
    "llm.confidence_threshold": Spec("llm", "confidence_threshold", float, "0~1 실수", "AI 확신도 임계값", "llm", 0.0, 1.0),
    "llm.daily_cost_limit_usd": Spec("llm", "daily_cost_limit_usd", float, "숫자($)", "일일 AI 비용 한도", "llm", 0.0, 100.0),
    "llm.daily_cost_warn_usd": Spec("llm", "daily_cost_warn_usd", float, "숫자($)", "AI 비용 조기 경고", "llm", 0.0, 100.0),
    # 프리필터
    "prefilter.rsi_buy_below": Spec("prefilter", "rsi_buy_below", int, "정수", "RSI 과매도 기준", "prefilter", 5, 50),
    "prefilter.rsi_sell_above": Spec("prefilter", "rsi_sell_above", int, "정수", "RSI 과매수 기준", "prefilter", 50, 95),
    "prefilter.min_volume_ratio": Spec("prefilter", "min_volume_ratio", float, "숫자", "거래량 최소 배수", "prefilter", 0.5, 10.0),
    "prefilter.trend_filter_enabled": Spec("prefilter", "trend_filter_enabled", _to_bool, "true/false", "추세 필터 사용", "prefilter"),
    # 펀더멘털
    "fundamentals.enabled": Spec("fundamentals", "enabled", _to_bool, "true/false", "펀더멘털 게이트", "fundamentals"),
    "fundamentals.max_per": Spec("fundamentals", "max_per", float, "숫자", "PER 상한", "fundamentals", 0, 1000),
    "fundamentals.max_pbr": Spec("fundamentals", "max_pbr", float, "숫자", "PBR 상한", "fundamentals", 0, 100),
    "fundamentals.max_debt_ratio": Spec("fundamentals", "max_debt_ratio", float, "숫자(%)", "부채비율 상한", "fundamentals", 0, 2000),
    "fundamentals.min_roe": Spec("fundamentals", "min_roe", float, "숫자(%)", "ROE 하한", "fundamentals", -100, 100),
    # Top-level
    "cycle_minutes": Spec(None, "cycle_minutes", int, "정수(분)", "점검 주기", "cycle_minutes", 1, 60),
}


def cmd_set(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """사용법:
      /set                           — 편집 가능한 키 목록 + 현재값
      /set <key> <value>             — 확정 버튼 표시
      /set <key> <value> confirm     — 즉시 적용
    """
    if not args:
        return _list_keys(ctx)

    if len(args) < 2:
        return _reply(
            "사용법:\n"
            "`/set <key> <value>` — 확정 버튼 표시\n"
            "`/set <key> <value> confirm` — 즉시 적용\n\n"
            "편집 가능한 키는 `/set` (인자 없이) 참고."
        )

    key_path = args[0]
    value_str = args[1]
    confirm = len(args) > 2 and args[2].lower() == "confirm"

    spec = WHITELIST.get(key_path)
    if spec is None:
        return _reply(
            f"❌ 편집 가능한 키가 아닙니다: `{key_path}`\n\n"
            f"편집 가능 목록은 `/set` (인자 없이) 로 확인."
        )

    try:
        new_value = spec.cast(value_str)
    except (ValueError, TypeError) as exc:
        return _reply(
            f"❌ 값 형식 오류\n"
            f"필요: {spec.type_label}\n"
            f"입력: `{value_str}`\n"
            f"(`{exc}`)"
        )

    if isinstance(new_value, (int, float)) and not isinstance(new_value, bool):
        if spec.min is not None and new_value < spec.min:
            return _reply(f"❌ 최소값 {spec.min} 이상이어야 합니다")
        if spec.max is not None and new_value > spec.max:
            return _reply(f"❌ 최대값 {spec.max} 이하여야 합니다")

    current = _get_current_runtime(ctx, spec)
    display_new = _format_yaml_value(new_value)

    if not confirm:
        return _reply(
            f"*⚙️ 설정 변경 확인*\n\n"
            f"{spec.desc}\n"
            f"`{key_path}`\n\n"
            f"현재: `{current}`\n"
            f"변경: `{display_new}`\n\n"
            f"파일: `{SETTINGS_PATH.name}` 저장 + 런타임 즉시 반영",
            reply_markup=_confirm_keyboard(key_path, value_str),
        )

    # 실제 적용
    try:
        backup_path = _write_yaml_value(spec, new_value)
    except Exception as exc:
        log.exception("settings.yaml 쓰기 실패")
        return _reply(f"❌ 파일 쓰기 실패\n`{exc}`")

    try:
        _apply_to_runtime(ctx, spec, new_value)
    except Exception as exc:
        log.exception("런타임 반영 실패")
        return _reply(
            f"⚠️ 파일 저장은 됐지만 런타임 반영 실패\n`{exc}`\n\n"
            f"백업: `{backup_path.name}`\n"
            f"`/restart` 로 재시도."
        )

    return _reply(
        f"✅ *설정 변경 완료*\n\n"
        f"{spec.desc}\n"
        f"`{key_path}`: `{current}` → `{_format_yaml_value(new_value)}`\n\n"
        f"백업: `{backup_path.name}`\n"
        f"런타임 반영 즉시 완료. 다음 점검부터 새 값 적용."
    )


def handle_set_callback(ctx: BotContext, data: str) -> dict[str, Any]:
    """콜백 `set:apply:<keypath>:<value>` 처리."""
    # data 예: "set:apply:risk.daily_loss_limit_pct:5"
    parts = data.split(":", 3)
    if len(parts) < 4 or parts[1] != "apply":
        return _reply(f"❌ 잘못된 set 콜백: {data}")
    key_path = parts[2]
    value_str = parts[3]
    return cmd_set(ctx, [key_path, value_str, "confirm"])


# ─────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────

def _confirm_keyboard(key_path: str, value_str: str) -> dict[str, Any]:
    return {"inline_keyboard": [[
        {"text": "✅ 변경 확정", "callback_data": f"set:apply:{key_path}:{value_str}"},
        {"text": "❌ 취소", "callback_data": "cancel"},
    ]]}


def _list_keys(ctx: BotContext) -> dict[str, Any]:
    """편집 가능한 키 목록 + 현재값. 섹션별 그룹핑."""
    groups: dict[str, list[str]] = {}
    for path, spec in WHITELIST.items():
        sec = spec.section or "_top"
        if sec not in groups:
            groups[sec] = []
        current = _get_current_runtime(ctx, spec)
        range_hint = ""
        if spec.min is not None and spec.max is not None:
            range_hint = f" _({spec.min}~{spec.max})_"
        groups[sec].append(f"- `{path}` = `{current}`{range_hint}")

    display_order = ["_top", "risk", "exit", "llm", "prefilter", "fundamentals"]
    section_titles = {
        "_top": "🔧 기본",
        "risk": "🛡️ 리스크",
        "exit": "💸 청산",
        "llm": "🤖 AI",
        "prefilter": "📋 프리필터",
        "fundamentals": "📊 펀더멘털",
    }

    lines = [
        "*⚙️ 편집 가능한 설정 키*",
        "",
        "_사용법: `/set <key> <value>`_",
        "_예: `/set risk.daily_loss_limit_pct 5`_",
        "",
    ]
    for sec in display_order:
        if sec not in groups:
            continue
        lines.append(f"*{section_titles[sec]}*")
        lines.extend(groups[sec])
        lines.append("")
    lines.append("_확정 버튼 후에만 적용됩니다 (2단계)._")
    lines.append("_파일 전체 보기: `/config raw`_")
    return _reply("\n".join(lines))


def _get_current_runtime(ctx: BotContext, spec: Spec) -> Any:
    """현재 실행 중 ctx.settings 에서 값 조회."""
    if spec.section is None:
        return getattr(ctx.settings, spec.runtime_field, None)
    section = getattr(ctx.settings, spec.runtime_field, None) or {}
    return section.get(spec.key, "_미설정_")


def _apply_to_runtime(ctx: BotContext, spec: Spec, new_value: Any) -> None:
    """런타임 ctx.settings 에 즉시 반영."""
    if spec.section is None:
        setattr(ctx.settings, spec.runtime_field, new_value)
        return
    section = getattr(ctx.settings, spec.runtime_field, None)
    if not isinstance(section, dict):
        section = {}
        setattr(ctx.settings, spec.runtime_field, section)
    section[spec.key] = new_value


def _format_yaml_value(v: Any) -> str:
    """값을 yaml 라인에 쓸 문자열로. 문자열은 따옴표 유지를 위해 단순 처리 제외 (화이트리스트엔 문자열 없음)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        # 5.0 → 5 로 (YAML 호환), 정수는 그대로
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return str(v)


def _write_yaml_value(spec: Spec, new_value: Any) -> Path:
    """settings.yaml 에서 해당 key 라인만 교체.

    동작:
      1. 원본 백업 (settings.yaml.bak.TIMESTAMP)
      2. 섹션 탐색 후 해당 키 라인을 regex 로 교체 (값 부분만, 주석 보존)
      3. 결과를 yaml.safe_load 로 재파싱해 문법/해당 키 값 검증
      4. 원본 파일 덮어쓰기
    """
    original = SETTINGS_PATH.read_text(encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SETTINGS_PATH.with_suffix(f".yaml.bak.{ts}")
    backup_path.write_text(original, encoding="utf-8")

    new_text = _replace_yaml_line(original, spec.section, spec.key, _format_yaml_value(new_value))

    # 검증: 재파싱
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"치환 후 YAML 파싱 실패: {exc}") from exc

    # 검증: 값이 실제로 바뀌었는지
    actual = parsed.get(spec.section, {}).get(spec.key) if spec.section else parsed.get(spec.key)
    if actual != new_value and not (
        isinstance(actual, float) and isinstance(new_value, (int, float)) and float(actual) == float(new_value)
    ):
        raise RuntimeError(
            f"치환이 적용되지 않음 (actual={actual!r}, expected={new_value!r}). "
            f"키가 여러 섹션에 있거나 파일 형식이 예상과 다릅니다."
        )

    SETTINGS_PATH.write_text(new_text, encoding="utf-8")
    log.info("settings.yaml 수정: %s.%s = %s (백업 %s)",
             spec.section or "_top", spec.key, new_value, backup_path.name)
    return backup_path


def _replace_yaml_line(text: str, section: str | None, key: str, new_value_str: str) -> str:
    """YAML 에서 `section.key` 라인의 값 부분만 교체. 주석 보존.

    - section=None 이면 들여쓰기 없는 top-level 키만 대상.
    - section 이 있으면 `^section:` 헤더 이후 같은 들여쓰기 블록 안에서 첫 `  key:` 라인.
    - 값 라인 형식: `<indent>key: <value>[  # comment]`
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_target_section = section is None  # top-level 이면 항상 "in section"
    target_indent: int | None = 0 if section is None else None
    done = False

    key_pattern = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<val>[^#\n]*?)(?P<tail>\s*#.*)?$")

    for line in lines:
        if done:
            out.append(line)
            continue

        stripped = line.rstrip("\n\r")
        # 섹션 감지
        if section is not None:
            # 섹션 시작: `^section:` (들여쓰기 없음)
            m_sec = re.match(rf"^{re.escape(section)}\s*:\s*(#.*)?$", stripped)
            if m_sec:
                in_target_section = True
                target_indent = None  # 첫 key 라인에서 확정
                out.append(line)
                continue
            # 다른 top-level 섹션 시작 → 대상 섹션 종료
            if in_target_section and re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", stripped):
                in_target_section = False

        if in_target_section:
            m = key_pattern.match(stripped)
            if m:
                indent = m.group("indent")
                if section is None:
                    # top-level 키는 들여쓰기 0
                    if indent != "":
                        out.append(line)
                        continue
                else:
                    # 섹션 하위 키 — 들여쓰기 확정 (첫 키로 판정)
                    if target_indent is None:
                        target_indent = len(indent)
                    if len(indent) != target_indent:
                        out.append(line)
                        continue

                if m.group("key") == key:
                    tail = m.group("tail") or ""
                    newline_tail = line[len(stripped):]
                    out.append(f"{indent}{key}: {new_value_str}{tail}{newline_tail}")
                    done = True
                    continue

        out.append(line)

    if not done:
        raise RuntimeError(
            f"키 라인을 찾지 못했습니다: section={section!r}, key={key!r}. "
            f"settings.yaml 에 해당 키가 없거나 형식이 예상과 다릅니다."
        )

    return "".join(out)
