from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# data/ 볼륨 안에 KILL_SWITCH 파일이 존재하면 신규 매수 전체 차단.
# 매도 시그널은 여전히 허용 (손절/청산 필요할 수 있음).
# 사용자는 텔레그램 /stop /resume 또는 파일 직접 토글(`touch data/KILL_SWITCH`) 가능.
# data/ 는 Docker 볼륨 마운트 대상이라 컨테이너 재시작에도 상태가 유지된다.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KILL_SWITCH_FILE = _PROJECT_ROOT / "data" / "KILL_SWITCH"


def is_active() -> bool:
    return KILL_SWITCH_FILE.exists()


def activate(reason: str = "") -> None:
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH_FILE.write_text(
        f"active since {datetime.now().isoformat(timespec='seconds')}\nreason: {reason}\n",
        encoding="utf-8",
    )
    log.warning("KILL SWITCH 활성화: %s", reason)


def deactivate() -> None:
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        log.warning("KILL SWITCH 해제")
