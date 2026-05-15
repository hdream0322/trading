from __future__ import annotations
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """tmp 파일에 쓴 뒤 os.replace 로 원자 교체.

    같은 디렉토리 안 tmp 사용 — 다른 파일시스템 cross-device 회피.
    전원 차단·컨테이너 강종 시에도 0바이트 잘림이 없는 invariant 보장.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
