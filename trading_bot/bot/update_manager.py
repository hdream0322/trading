from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# data/ 볼륨에 상태 파일 보관 — 컨테이너 재시작 / 업데이트 후에도 영속.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AUTO_UPDATE_DISABLED_FILE = _PROJECT_ROOT / "data" / "AUTO_UPDATE_DISABLED"
CURRENT_IMAGE_DIGEST_FILE = _PROJECT_ROOT / "data" / "current_image_digest"

# 같은 compose 네트워크 안에서 service name 으로 도달. 포트는 내부 전용 (expose).
WATCHTOWER_UPDATE_URL = "http://watchtower:8080/v1/update"

# GHCR 익명 pull 엔드포인트 (public 패키지 전용)
GHCR_TOKEN_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository:hdream0322/trading:pull"
GHCR_MANIFEST_URL = "https://ghcr.io/v2/hdream0322/trading/manifests/latest"
GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/hdream0322/trading/releases/latest"
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


# ─────────────────────────────────────────────────────────────
# 자동 업데이트 토글 (상태는 파일 기반, 재시작에도 보존)
# ─────────────────────────────────────────────────────────────

def is_auto_enabled() -> bool:
    """상태 파일이 없으면 활성, 있으면 비활성."""
    return not AUTO_UPDATE_DISABLED_FILE.exists()


def enable_auto() -> None:
    if AUTO_UPDATE_DISABLED_FILE.exists():
        AUTO_UPDATE_DISABLED_FILE.unlink()
    log.info("자동 업데이트 활성화")


def disable_auto(reason: str = "") -> None:
    AUTO_UPDATE_DISABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTO_UPDATE_DISABLED_FILE.write_text(
        f"disabled at {datetime.now().isoformat(timespec='seconds')}\nreason: {reason}\n",
        encoding="utf-8",
    )
    log.info("자동 업데이트 비활성화: %s", reason)


def disabled_since() -> str | None:
    """비활성화된 시각 문자열을 반환 (활성 상태면 None)."""
    if not AUTO_UPDATE_DISABLED_FILE.exists():
        return None
    try:
        content = AUTO_UPDATE_DISABLED_FILE.read_text(encoding="utf-8")
        first = content.splitlines()[0] if content else ""
        if first.startswith("disabled at "):
            return first[len("disabled at "):]
    except OSError:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# Watchtower HTTP API 호출
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# GHCR manifest digest 조회 (public 이미지 익명 접근)
# ─────────────────────────────────────────────────────────────

def fetch_remote_digest(timeout: float = 10.0) -> str:
    """GHCR 에 올라와 있는 :latest 이미지의 manifest digest 조회.

    익명 pull 토큰 → HEAD 요청 → Docker-Content-Digest 헤더 반환.
    public 패키지 전제.
    """
    # 1. 익명 Bearer 토큰 발급
    resp = httpx.get(GHCR_TOKEN_URL, timeout=timeout)
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("GHCR 익명 토큰 발급 실패")

    # 2. manifest HEAD 요청
    resp = httpx.head(
        GHCR_MANIFEST_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": _MANIFEST_ACCEPT,
        },
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    digest = resp.headers.get("Docker-Content-Digest", "").strip()
    if not digest:
        raise RuntimeError("GHCR 응답에 Docker-Content-Digest 헤더 없음")
    return digest


def snapshot_current_digest() -> None:
    """봇 기동 시 호출. 현재 실행 중인 이미지의 digest 를 파일에 저장.

    GHCR :latest 가 이 시점에 가리키는 digest == 방금 pull 받은 이미지의 digest
    라는 가정 (Watchtower 가 업데이트 후 재기동한 경우).

    네트워크 에러 등 실패 시 digest 저장 없음 — 이후 /update 는 보수적으로
    '업데이트 가능' 으로 간주하고 Watchtower 에 요청 전송.
    """
    try:
        digest = fetch_remote_digest()
    except Exception as exc:
        log.warning("기동 시 digest 스냅샷 실패: %s", exc)
        return
    try:
        CURRENT_IMAGE_DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        CURRENT_IMAGE_DIGEST_FILE.write_text(digest, encoding="utf-8")
        log.info("기동 시 이미지 digest 저장: %s", digest[:24] + "...")
    except OSError as exc:
        log.warning("digest 파일 쓰기 실패: %s", exc)


def read_current_digest() -> str | None:
    try:
        return CURRENT_IMAGE_DIGEST_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def check_for_update() -> tuple[bool, str, str]:
    """최신 버전이 있는지 확인.

    반환: (has_update, current_digest, remote_digest)
      has_update=True   → 업데이트 필요 (Watchtower 호출 대상)
      has_update=False  → 이미 최신
    current_digest 가 빈 문자열이면 스냅샷 기록이 없는 상태 — 보수적으로 True 반환.
    """
    current = read_current_digest() or ""
    remote = fetch_remote_digest()
    if not current:
        return True, "", remote
    return current != remote, current, remote


def fetch_latest_release_version(timeout: float = 10.0) -> str:
    """GitHub Releases API 에서 최신 릴리스 태그 문자열 조회.

    반환 예시: '0.2.7'  ('v' prefix 는 제거)
    """
    resp = httpx.get(GITHUB_LATEST_RELEASE_URL, timeout=timeout)
    if resp.status_code == 404:
        raise RuntimeError("아직 생성된 GitHub Release 가 없습니다")
    resp.raise_for_status()
    data = resp.json()
    tag = str(data.get("tag_name", "")).strip()
    if not tag:
        raise RuntimeError("GitHub API 응답에 tag_name 없음")
    return tag.lstrip("vV")


def trigger_update(token: str) -> dict[str, object]:
    """Watchtower 에게 즉시 업데이트 요청.

    중요: Watchtower HTTP API 는 fire-and-forget 이 아니라 *동기* 호출이다.
    업데이트 작업(scan + pull + stop + recreate)이 전부 끝난 후에야 HTTP 응답을
    보낸다. 이 과정은 이미지 크기/네트워크에 따라 30~120초 걸릴 수 있다.

    따라서 이 함수는 두 단계 타임아웃을 둔다:
      - connect timeout: 5초 — TCP 연결 실패는 빠르게 감지
      - read timeout: 5초 — 응답을 끝까지 기다리지 않음

    ReadTimeout 은 "요청은 전달됐지만 응답이 늦음 = 처리 중" 으로 해석하고
    성공으로 간주. 결과는 Watchtower 의 자체 Telegram 알림 + 봇 재기동 메시지로
    사용자에게 별도 통지된다.

    반환 dict 의 status:
      - "triggered"        : 즉시 응답 받음 (보통 이미 최신인 경우)
      - "accepted"         : ReadTimeout — 처리 중 (가장 흔한 경우)
    """
    if not token:
        raise RuntimeError(
            "WATCHTOWER_HTTP_TOKEN 이 .env 에 설정되지 않았습니다. "
            "openssl rand -hex 32 로 생성 후 추가하고 봇을 재시작하세요."
        )

    http_timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    try:
        resp = httpx.post(
            WATCHTOWER_UPDATE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=http_timeout,
        )
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Watchtower 에 연결할 수 없습니다 ({exc}). "
            f"'sudo docker compose ps' 로 watchtower 컨테이너가 떠있는지 확인하세요."
        ) from exc
    except httpx.ConnectTimeout as exc:
        raise RuntimeError(f"Watchtower 연결 타임아웃: {exc}") from exc
    except httpx.ReadTimeout:
        # 정상 케이스: Watchtower 가 업데이트 중이라 응답이 늦음.
        # 요청은 이미 전달됐고, 결과는 Watchtower 알림으로 별도 전송된다.
        log.info("Watchtower ReadTimeout — 요청은 전달됨, 백그라운드 처리 중")
        return {"status": "accepted", "http_status": None}

    if resp.status_code == 401:
        raise RuntimeError(
            "Watchtower 인증 실패 (401). .env 의 WATCHTOWER_HTTP_TOKEN 과 "
            "watchtower 컨테이너 환경변수가 일치하는지 확인 후 compose 재기동하세요."
        )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"Watchtower 응답 실패: status={resp.status_code} body={resp.text[:200]}"
        )
    return {"status": "triggered", "http_status": resp.status_code}
