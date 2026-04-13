"""/logs — 텔레그램에서 서버 로그 조회. SSH 없이 운영하기 위한 커맨드.

사용법:
  /logs                — 최근 30줄 (기본)
  /logs 100            — 최근 100줄 (길면 자동으로 파일 전송으로 전환)
  /logs error          — 최근 ERROR/WARNING 30건
  /logs error 50       — 최근 ERROR/WARNING 50건
  /logs file           — 오늘 로그 파일 통째로 다운로드
  /logs file 2026-04-12 — 특정 날짜 파일 (bot.log.YYYY-MM-DD)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply

log = logging.getLogger(__name__)

# 프로젝트 루트의 logs 디렉토리.
# 이 파일: trading_bot/bot/commands_logs.py → parents[2] = 프로젝트 루트
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_LOG_FILE = _LOG_DIR / "bot.log"

# 텔레그램 메시지 한도 4096자. 코드블록 펜스(```...```)·여유 차감해 안전치.
_MAX_TEXT = 3800
# 기본/최대 줄 수
_DEFAULT_LINES = 30
_MAX_LINES = 500


def cmd_logs(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    if not _LOG_FILE.exists():
        return _reply(
            f"로그 파일이 없습니다: `{_LOG_FILE}`\n"
            "_컨테이너 로그는 `docker logs trading-bot` 로 확인하세요_"
        )

    # 서브커맨드 파싱
    if args:
        sub = args[0].strip().lower()
        if sub == "file":
            date = args[1].strip() if len(args) > 1 else ""
            return _send_file(date)
        if sub in {"error", "err", "errors"}:
            n = _parse_int(args[1] if len(args) > 1 else None, _DEFAULT_LINES)
            return _send_filtered(n, level_filter={"ERROR", "WARNING", "CRITICAL"})
        # 숫자면 줄 수
        n = _parse_int(sub, _DEFAULT_LINES)
        return _send_tail(n)

    return _send_tail(_DEFAULT_LINES)


def _parse_int(s: str | None, default: int) -> int:
    if not s:
        return default
    try:
        return max(1, min(int(s), _MAX_LINES))
    except (ValueError, TypeError):
        return default


def _read_tail(path: Path, n: int) -> list[str]:
    """파일 끝에서 N줄 읽기 (전체 로드 없이 역방향 블록 읽기)."""
    n = max(1, n)
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:]
    except Exception as exc:
        log.exception("로그 tail 읽기 실패")
        return [f"[로그 읽기 실패: {exc}]"]


def _send_tail(n: int) -> dict[str, Any]:
    lines = _read_tail(_LOG_FILE, n)
    return _format_or_file(lines, f"최근 {n}줄")


def _send_filtered(n: int, level_filter: set[str]) -> dict[str, Any]:
    """ERROR/WARNING 만 N건 추출. 파일 뒤에서 넉넉히 읽어 필터링."""
    # level 행 찾으려면 최소 n*20줄은 훑어야 확률 높음. 상한 5000.
    scan = min(max(n * 30, 500), 5000)
    all_lines = _read_tail(_LOG_FILE, scan)
    pattern = re.compile(r"\[(ERROR|WARNING|CRITICAL)\]")
    filtered = [ln for ln in all_lines if pattern.search(ln)]
    picked = filtered[-n:] if filtered else []
    label = f"최근 {level_filter & {'ERROR','WARNING','CRITICAL'}} {n}건 중 {len(picked)}건"
    label = f"최근 ERROR/WARNING {len(picked)}건 (요청 {n}건)"
    if not picked:
        return _reply(f"*로그 — {label}*\n_해당 레벨 로그가 최근 기록에 없습니다_")
    return _format_or_file(picked, label)


def _format_or_file(lines: list[str], label: str) -> dict[str, Any]:
    """줄 배열을 받아 4096자 내면 코드블록 텍스트, 넘치면 파일로 자동 전환."""
    text_body = "\n".join(lines)
    # 코드블록 ```...``` 은 탭 한 번에 복사 가능.
    # parse_mode=Markdown 호환. 안에 ``` 이 있으면 깨질 수 있지만 로그에선 드묾.
    candidate = f"*로그 — {label}*\n```\n{text_body}\n```"
    if len(candidate) <= _MAX_TEXT:
        return _reply(candidate)

    # 너무 길면 파일로 전환
    filename = f"logs_{label.replace(' ', '_').replace('/', '-')[:40]}.txt"
    caption = (
        f"*로그 — {label}*\n"
        f"_메시지 길이 초과({len(candidate):,}자 > {_MAX_TEXT:,}자) 로 파일로 보냅니다_"
    )
    content = text_body.encode("utf-8")
    return {"document": (filename, content), "text": caption}


def _send_file(date: str) -> dict[str, Any]:
    """로그 파일 통째로 다운로드. date 없으면 오늘 bot.log."""
    if date:
        # YYYY-MM-DD 형식만 허용 (경로 주입 방지)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return _reply(
                f"잘못된 날짜 형식: `{date}`\n"
                "예시: `/logs file 2026-04-12`"
            )
        path = _LOG_DIR / f"bot.log.{date}"
    else:
        path = _LOG_FILE

    if not path.exists():
        available = sorted(p.name for p in _LOG_DIR.glob("bot.log*"))[-8:]
        listing = "\n".join(f"• `{x}`" for x in available) or "_없음_"
        return _reply(
            f"파일이 없습니다: `{path.name}`\n\n"
            f"*현재 남아있는 파일* (최근 8개)\n{listing}"
        )

    try:
        content = path.read_bytes()
    except Exception as exc:
        return _reply(f"❌ 파일 읽기 실패\n`{exc}`")

    size_mb = len(content) / 1024 / 1024
    if size_mb > 45:  # 텔레그램 업로드 한도 50MB, 여유 5MB
        return _reply(
            f"❌ 파일이 너무 큽니다 ({size_mb:.1f}MB > 45MB)\n"
            "_`/logs 500` 으로 최근 줄만 받거나 SSH 로 분할 다운로드 하세요_"
        )

    line_count = content.count(b"\n")
    caption = (
        f"*로그 파일 — `{path.name}`*\n"
        f"크기: {size_mb:.2f} MB · 줄 수: {line_count:,}"
    )
    return {"document": (path.name, content), "text": caption}
