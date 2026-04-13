from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    retention_days: int = 7,
) -> None:
    """표준출력 + 파일(일자별 회전, N일 보관) 로그 핸들러 설정.

    파일: log_dir/bot.log (현재), log_dir/bot.log.YYYY-MM-DD (이전 날짜)
    매일 자정(프로세스 로컬 시각)에 회전. retention_days 일 지난 파일은 자동 삭제.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            filename=str(log_dir / "bot.log"),
            when="midnight",
            interval=1,
            backupCount=retention_days,
            encoding="utf-8",
            utc=False,
        )
        file_handler.suffix = "%Y-%m-%d"
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
