from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# data/paper_account_issued — 현재 paper 자격증명을 사용하기 시작한 시각 기록.
# 이 시점 + 90일이 KIS 모의투자 계좌 유효기간 만료 예정일.
# 파일은 Docker 볼륨(data/) 에 있어 컨테이너 재시작에도 유지된다.
ISSUED_FILE = _PROJECT_ROOT / "data" / "paper_account_issued"

PAPER_EXPIRY_DAYS = 90
KIS_PORTAL_URL = "https://apiportal.koreainvestment.com/intro"


def ensure_issued_date() -> None:
    """파일이 없으면 지금 시각으로 초기화.

    최초 배포 시 한 번 호출되어 타이머 시작. 이미 파일이 있으면 건드리지 않음
    (컨테이너 재시작해도 타이머 유지).
    """
    if ISSUED_FILE.exists():
        return
    ISSUED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUED_FILE.write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )
    log.info("paper_account_issued 초기 생성: 지금 시각으로 설정")


def mark_updated() -> None:
    """자격증명 재로드 시 호출. 만료 카운트다운 리셋."""
    ISSUED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUED_FILE.write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )
    log.info("paper_account_issued 갱신 — 만료 카운트다운 리셋")


def get_issued_date() -> datetime | None:
    if not ISSUED_FILE.exists():
        return None
    try:
        return datetime.fromisoformat(ISSUED_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def days_until_expiry() -> int | None:
    """만료까지 남은 일수. 파일 없거나 파싱 실패 시 None."""
    issued = get_issued_date()
    if issued is None:
        return None
    expiry = issued + timedelta(days=PAPER_EXPIRY_DAYS)
    return (expiry - datetime.now()).days


def build_expiry_warning(days_left: int | None, mode: str) -> str | None:
    """만료 경고 메시지 빌드. 경고할 필요 없으면 None.

    - 실전 모드: 만료 없음, 항상 None
    - paper 모드 + 7일 이내: 알림 메시지
    - paper 모드 + 8일 이상: None
    """
    if mode != "paper":
        return None
    if days_left is None:
        return None
    if days_left > 7:
        return None

    if days_left <= 0:
        expired_days = abs(days_left)
        return (
            "🚨 *모의 계좌 만료됨*\n\n"
            f"KIS 모의투자 계좌 유효기간 {PAPER_EXPIRY_DAYS}일이 "
            f"{expired_days}일 전에 지났습니다.\n"
            "이 시점부터 거래 API 호출이 실패할 수 있습니다.\n\n"
            "*지금 해야 할 것*:\n"
            f"1. {KIS_PORTAL_URL} 접속\n"
            "2. 모의투자 재신청 → 새 앱키/시크릿/계좌번호 발급\n"
            "3. NAS SSH 에서:\n"
            "   `nano /volume1/docker/trading/data/credentials.env`\n"
            "4. 새 `KIS_PAPER_APP_KEY`, `KIS_PAPER_APP_SECRET`, "
            "`KIS_PAPER_ACCOUNT_NO` 입력 후 저장\n"
            "5. 5분 내 자동 감지 · 반영\n"
            "   (즉시 반영하려면 텔레그램 `/reload`)"
        )

    return (
        "⏰ *모의 계좌 만료 임박*\n\n"
        f"KIS 모의투자 계좌가 *{days_left}일 후* 만료됩니다.\n"
        "여유있게 미리 재신청해두는 것을 권장합니다.\n\n"
        f"*재신청*: {KIS_PORTAL_URL}\n\n"
        "새 키 받으신 후:\n"
        "1. NAS: `nano /volume1/docker/trading/data/credentials.env`\n"
        "2. 새 `KIS_PAPER_*` 값 입력 후 저장\n"
        "3. 5분 내 자동 반영 (또는 `/reload` 즉시 반영)"
    )
