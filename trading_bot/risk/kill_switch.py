from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# data/ 볼륨 안에 KILL_SWITCH 파일이 존재하면 신규 매수 전체 차단.
# 매도 시그널은 여전히 허용 (손절/청산 필요할 수 있음).
# 사용자는 텔레그램 /stop /resume 또는 파일 직접 토글(`touch data/KILL_SWITCH`) 가능.
# data/ 는 Docker 볼륨 마운트 대상이라 컨테이너 재시작에도 상태가 유지된다.
#
# 파일 포맷 (plain text, 세 줄):
#   active since 2026-04-11T10:23:45
#   reason: 에러 급증 자동 차단 (최근 1시간 12건)
#   trigger: auto           ← auto|manual (없으면 manual 로 간주 — 하위 호환)
#
# 회로차단기(error_spike_watchdog)가 자동으로 활성화한 경우 `trigger: auto`.
# 해제 정책:
#   - 수동 활성화(/stop, touch): 반드시 수동 해제(/resume) 필요
#   - 자동 활성화: watchdog 이 복구 조건(에러 0건 + 최소 대기 경과) 충족 시 자동 해제
#   - 단, 최근 1시간 내 자동 해제 이력이 있으면 그 이후는 수동만 허용 (플래핑 방지)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KILL_SWITCH_FILE = _PROJECT_ROOT / "data" / "KILL_SWITCH"
AUTO_RELEASE_LOG_FILE = _PROJECT_ROOT / "data" / "KILL_SWITCH_AUTO_RELEASE.log"


def is_active() -> bool:
    return KILL_SWITCH_FILE.exists()


def activate(reason: str = "", auto: bool = False) -> None:
    KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    trigger = "auto" if auto else "manual"
    KILL_SWITCH_FILE.write_text(
        f"active since {datetime.now().isoformat(timespec='seconds')}\n"
        f"reason: {reason}\n"
        f"trigger: {trigger}\n",
        encoding="utf-8",
    )
    log.warning("KILL SWITCH 활성화 [%s]: %s", trigger, reason)


def deactivate(auto: bool = False) -> None:
    """킬스위치 해제. auto=True 면 자동 해제 이력을 기록 (플래핑 판정용)."""
    if KILL_SWITCH_FILE.exists():
        KILL_SWITCH_FILE.unlink()
        if auto:
            _record_auto_release()
            log.warning("KILL SWITCH 자동 해제")
        else:
            log.warning("KILL SWITCH 수동 해제")


def is_auto_triggered() -> bool:
    """현재 활성화된 킬스위치가 자동(회로차단기)에 의한 것인지 판별.

    파일이 없으면 False. 파일에 `trigger: auto` 줄이 있을 때만 True.
    하위 호환: 기존 포맷(trigger 줄 없음)은 manual 로 간주.
    """
    if not KILL_SWITCH_FILE.exists():
        return False
    try:
        text = KILL_SWITCH_FILE.read_text(encoding="utf-8")
    except Exception:
        return False
    for line in text.splitlines():
        if line.strip().lower() == "trigger: auto":
            return True
    return False


def get_activated_at() -> datetime | None:
    """킬스위치가 언제 활성화됐는지 (파일 첫 줄 파싱). 실패 시 None."""
    if not KILL_SWITCH_FILE.exists():
        return None
    try:
        text = KILL_SWITCH_FILE.read_text(encoding="utf-8")
        first = text.splitlines()[0] if text else ""
        # "active since 2026-04-11T10:23:45"
        if first.startswith("active since "):
            return datetime.fromisoformat(first[len("active since "):].strip())
    except Exception:
        return None
    return None


def _record_auto_release() -> None:
    """자동 해제 이력을 로그 파일에 한 줄 추가 (플래핑 판정용)."""
    try:
        AUTO_RELEASE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUTO_RELEASE_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds") + "\n")
    except Exception:
        log.exception("자동 해제 이력 기록 실패")


def count_recent_auto_releases(hours: int = 1) -> int:
    """최근 N시간 내 자동 해제 이력 건수. 플래핑 방지용 판정."""
    if not AUTO_RELEASE_LOG_FILE.exists():
        return 0
    cutoff = datetime.now() - timedelta(hours=hours)
    count = 0
    try:
        for line in AUTO_RELEASE_LOG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ts = datetime.fromisoformat(line)
            except ValueError:
                continue
            if ts >= cutoff:
                count += 1
    except Exception:
        log.exception("자동 해제 이력 읽기 실패")
        return 0
    return count
