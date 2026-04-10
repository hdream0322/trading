from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# data/ 볼륨에 모드 오버라이드 상태 저장. 재시작 / 컨테이너 업데이트 후에도 유지된다.
# 이 파일이 있으면 config.load_settings 가 .env KIS_MODE 대신 이 값을 우선 사용.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MODE_OVERRIDE_FILE = _PROJECT_ROOT / "data" / "kis_mode_override"

_VALID_MODES = frozenset({"paper", "live"})


def read_override() -> str | None:
    if not MODE_OVERRIDE_FILE.exists():
        return None
    try:
        mode = MODE_OVERRIDE_FILE.read_text(encoding="utf-8").strip()
        return mode if mode in _VALID_MODES else None
    except OSError as exc:
        log.warning("mode override 파일 읽기 실패: %s", exc)
        return None


def write_override(mode: str) -> None:
    if mode not in _VALID_MODES:
        raise ValueError(f"잘못된 모드: {mode!r} (paper 또는 live)")
    MODE_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODE_OVERRIDE_FILE.write_text(mode, encoding="utf-8")
    log.info("mode override 저장: %s", mode)


def clear_override() -> None:
    if MODE_OVERRIDE_FILE.exists():
        MODE_OVERRIDE_FILE.unlink()
        log.info("mode override 제거")
