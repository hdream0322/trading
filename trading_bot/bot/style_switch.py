"""Stage 11: 거래 스타일 프리셋 (scalp/swing/default).

`data/trade_mode` 파일에 활성 스타일을 영속화. config.load_settings() 에서
파일을 읽고 settings.yaml 의 `trade_modes.<style>` 블록을 prefilter/risk/exit/llm
섹션에 오버레이한다. 컨테이너 재시작·이미지 업데이트에도 상태 유지.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from trading_bot.utils.atomic_io import atomic_write_text

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STYLE_FILE = _PROJECT_ROOT / "data" / "trade_mode"

DEFAULT_STYLE = "default"
VALID_STYLES = frozenset({"default", "scalp", "swing"})

# 프리셋이 오버라이드하는 settings.yaml 섹션 매핑.
# trade_modes.<style>.prefilter → raw["prefilter"] 같은 식으로 키별 덮어쓰기.
# Settings 데이터클래스 필드명과 settings.yaml 섹션명이 다른 경우(`exit` ↔ `exit_rules`)
# 가 있으므로 yaml 섹션명 기준으로 통일.
_OVERLAY_SECTIONS = ("prefilter", "risk", "exit", "llm")


def read_style() -> str:
    """현재 활성 스타일을 반환. 파일 없거나 잘못된 값이면 default."""
    if not STYLE_FILE.exists():
        return DEFAULT_STYLE
    try:
        style = STYLE_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("trade_mode 파일 읽기 실패 (default 폴백): %s", exc)
        return DEFAULT_STYLE
    if style not in VALID_STYLES:
        log.warning("trade_mode 파일에 잘못된 값 %r — default 폴백", style)
        return DEFAULT_STYLE
    return style


def write_style(style: str) -> None:
    if style not in VALID_STYLES:
        raise ValueError(f"잘못된 스타일: {style!r} (default/scalp/swing)")
    if style == DEFAULT_STYLE:
        clear_style()
        return
    atomic_write_text(STYLE_FILE, style)
    log.info("trade_mode 저장: %s", style)


def clear_style() -> None:
    if STYLE_FILE.exists():
        STYLE_FILE.unlink()
        log.info("trade_mode 제거 (default 복귀)")


def apply_style(style: str, raw: dict[str, Any]) -> dict[str, Any]:
    """`raw` (settings.yaml 파싱 결과) 의 prefilter/risk/exit/llm 섹션에
    `trade_modes.<style>` 의 키를 덮어쓴다. raw 자체를 in-place 수정.

    style==default 또는 trade_modes 섹션이 없으면 무변경. 알 수 없는 스타일이면
    경고 후 무변경.
    """
    if style == DEFAULT_STYLE:
        return raw
    presets = raw.get("trade_modes") or {}
    overlay = presets.get(style)
    if not isinstance(overlay, dict):
        log.warning("trade_modes.%s 프리셋이 settings.yaml 에 없음 — default 사용", style)
        return raw
    for section in _OVERLAY_SECTIONS:
        section_overlay = overlay.get(section)
        if not isinstance(section_overlay, dict):
            continue
        target = raw.get(section)
        if not isinstance(target, dict):
            target = {}
            raw[section] = target
        for key, val in section_overlay.items():
            target[key] = val
    return raw


def get_preset(style: str, raw: dict[str, Any]) -> dict[str, Any]:
    """텔레그램 비교 표시용 — 스타일 프리셋 dict 반환 (없으면 빈 dict)."""
    if style == DEFAULT_STYLE:
        return {}
    presets = raw.get("trade_modes") or {}
    overlay = presets.get(style)
    return overlay if isinstance(overlay, dict) else {}
