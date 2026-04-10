from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 프로젝트 루트에 KILL_SWITCH 파일이 존재하면 신규 매수 전체 차단.
# 매도 시그널은 여전히 허용 (손절/청산 필요할 수 있음).
# 사용자는 `touch KILL_SWITCH` / `rm KILL_SWITCH` 로 토글 가능.
KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent.parent / "KILL_SWITCH"


def is_active() -> bool:
    return KILL_SWITCH_FILE.exists()


def activate(reason: str = "") -> None:
    KILL_SWITCH_FILE.write_text(
        f"active since {datetime.now().isoformat(timespec='seconds')}\nreason: {reason}\n",
        encoding="utf-8",
    )
    log.warning("KILL SWITCH 활성화: %s", reason)


def deactivate() -> None:
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        log.warning("KILL SWITCH 해제")
