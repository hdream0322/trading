from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# data/ 볼륨 안에 QUIET_MODE 파일이 존재하면 "조용 모드" 활성.
# - 사이클 요약은 원래 이벤트(주문/청산/차단/에러) 있을 때만 발송되는데,
#   조용 모드에서는 추가로 장 시작/마감 브리핑 알림도 끈다.
# - 차단/에러는 조용 모드여도 반드시 전송된다 (중요 이벤트니까).
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
