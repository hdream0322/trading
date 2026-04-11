from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from trading_bot.store.db import DB_PATH

log = logging.getLogger(__name__)

BACKUP_DIR = DB_PATH.parent / "backup"
RETENTION_DAYS = 7  # 롤링 보관 기간


def create_daily_backup() -> Path | None:
    """오늘 날짜의 SQLite 스냅샷을 data/backup/ 에 생성.

    sqlite3 의 Online Backup API 를 사용하여 다른 트랜잭션이 진행 중이어도
    안전하게 복사. 동시성 문제 없음.

    반환: 생성된 백업 파일 경로 (실패 시 None)
    """
    if not DB_PATH.exists():
        log.warning("원본 DB 가 없어 백업 생략: %s", DB_PATH)
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    dest = BACKUP_DIR / f"trading_{today}.sqlite"

    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        log.info("DB 백업 완료: %s (%d bytes)", dest, dest.stat().st_size)
        return dest
    except Exception:
        log.exception("DB 백업 실패")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        return None


def prune_old_backups(retention_days: int = RETENTION_DAYS) -> int:
    """retention_days 를 초과한 백업 파일 삭제. 반환: 삭제한 파일 수."""
    if not BACKUP_DIR.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0
    for f in BACKUP_DIR.glob("trading_*.sqlite"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            continue
    if deleted:
        log.info("오래된 백업 %d개 삭제 (%d일 초과)", deleted, retention_days)
    return deleted
