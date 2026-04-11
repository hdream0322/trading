from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# data/ 볼륨 안에 QUIET_MODE 파일이 존재하면 "조용 모드" 활성.
# - 조용 모드 OFF (기본): 10분 사이클마다 hold 여도 텔레그램 요약이 전송된다.
# - 조용 모드 ON: hold-only 사이클 요약은 스킵. 거래/청산/차단/에러가 있을 때만 전송.
# - 장 시작(09:00)/마감(15:35) 브리핑은 조용 모드와 무관하게 항상 전송.
# - 사용자는 텔레그램 /quiet on|off 또는 파일 직접 토글 가능.
# - KILL_SWITCH 와 동일한 파일 기반 패턴.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
QUIET_MODE_FILE = _PROJECT_ROOT / "data" / "QUIET_MODE"


def is_active() -> bool:
    return QUIET_MODE_FILE.exists()


def activate(reason: str = "") -> None:
    QUIET_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUIET_MODE_FILE.write_text(
        f"active since {datetime.now().isoformat(timespec='seconds')}\nreason: {reason}\n",
        encoding="utf-8",
    )
    log.info("조용 모드 활성화: %s", reason)


def deactivate() -> None:
    if QUIET_MODE_FILE.exists():
        QUIET_MODE_FILE.unlink()
        log.info("조용 모드 해제")
