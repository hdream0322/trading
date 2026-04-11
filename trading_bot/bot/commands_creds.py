"""/setcreds, /reload, /restart 커맨드 — 자격증명 교체와 컨테이너 재시작."""
from __future__ import annotations

import logging
from typing import Any

from trading_bot.bot import expiry, runtime_state
from trading_bot.bot.context import BotContext
from trading_bot.bot.formatters import mode_badge
from trading_bot.bot.keyboards import _reply
from trading_bot.config import (
    CREDENTIALS_OVERRIDE_FILE,
    build_trade_cfg,
    load_credentials_override,
)
from trading_bot.kis.client import KisClient

log = logging.getLogger(__name__)


def cmd_reload(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """data/credentials.env 파일을 재로드하고 KisClient 를 재생성.

    3개월 주기로 KIS 모의투자 앱키를 재발급 받아야 할 때 사용.
    Docker 재시작 없이 런타임에 새 자격증명 적용.

    사용자 흐름:
      1. KIS 에서 새 앱키/시크릿/계좌번호 발급
      2. NAS SSH: nano /volume1/docker/trading/data/credentials.env
      3. 파일에 새 값 작성 (KIS_PAPER_APP_KEY=... 등) 후 저장
      4. 텔레그램 /reload — 즉시 반영
    """
    if not CREDENTIALS_OVERRIDE_FILE.exists():
        return _reply(
            "ℹ️ 아직 자격증명 오버라이드 파일이 없어요.\n\n"
            "지금 봇은 `.env` 에 있는 키로 정상 동작 중입니다. "
            "`/reload` 는 그 위에 덮어쓸 새 키가 있을 때만 의미가 있어요.\n\n"
            "*새 앱키로 갈아끼우려면* 텔레그램에서 바로 입력하세요:\n"
            "`/setcreds paper APP_KEY APP_SECRET ACCOUNT_NO`\n\n"
            "- 파일이 자동으로 생성되고 즉시 반영됩니다\n"
            "- 원본 메시지는 보안상 자동 삭제돼요\n"
            "- 실전 계좌는 끝에 `confirm` 을 붙이세요"
        )

    try:
        load_credentials_override()
        new_cfg = build_trade_cfg(ctx.settings.kis.mode)
    except Exception as exc:
        log.exception("자격증명 재로드 실패")
        return _reply(
            f"❌ *자격증명 재로드 실패*\n`{exc}`\n\n"
            f"credentials.env 내용 확인 후 다시 시도하세요."
        )

    # 토큰 캐시 삭제 — 옛 키로 발급된 토큰은 새 키와 안 맞음
    from pathlib import Path
    tokens_dir = Path(__file__).resolve().parent.parent.parent / "tokens"
    deleted_tokens = 0
    try:
        for token_file in tokens_dir.glob("kis_token_*.json"):
            token_file.unlink()
            deleted_tokens += 1
    except Exception as exc:
        log.warning("토큰 캐시 삭제 실패: %s", exc)

    # KisClient 원자적 교체 (trading_lock 으로 사이클과 직렬화)
    with ctx.trading_lock:
        old_kis = ctx.kis
        new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
        ctx.settings.kis = new_cfg
        ctx.kis = new_kis
        try:
            old_kis.close()
        except Exception:
            pass

    # paper 모드 자격증명 재로드 시 만료 카운트다운 리셋
    if new_cfg.mode == "paper":
        expiry.mark_updated()

    badge = mode_badge(new_cfg.mode)
    expiry_line = ""
    if new_cfg.mode == "paper":
        expiry_line = f"\n만료 카운트다운: {expiry.PAPER_EXPIRY_DAYS}일 리셋됨"
    return _reply(
        f"✅ *자격증명 재로드 완료*\n\n"
        f"모드: {badge}\n"
        f"계좌: `{new_cfg.account_no}-{new_cfg.account_product_cd}`\n"
        f"앱키 앞 12자: `{new_cfg.app_key[:12]}...`\n"
        f"토큰 캐시 삭제: {deleted_tokens}개{expiry_line}\n\n"
        f"다음 KIS 호출 시 새 키로 새 토큰 자동 발급됩니다.\n"
        f"`/status` 로 동작 확인."
    )


def cmd_setcreds(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """텔레그램으로 KIS 자격증명 직접 교체.

    사용법:
      /setcreds                                  — 사용법 표시
      /setcreds paper KEY SECRET ACCOUNT         — 모의 계좌 교체 (즉시)
      /setcreds live KEY SECRET ACCOUNT confirm  — 실전 계좌 교체 (confirm 필수)

    동작:
      1. 인자 검증
      2. data/credentials.env 에 병합 저장 (다른 모드 키는 보존)
      3. load_credentials_override() → build_trade_cfg → KisClient 교체
      4. 토큰 캐시 삭제 (해당 모드만)
      5. paper 모드면 expiry.mark_updated() (카운트다운 리셋)
      6. runtime_state.credentials_last_mtime 갱신 (watcher 중복 방지)
      7. 원본 /setcreds 메시지 삭제 플래그 반환 → poller 가 실제로 삭제

    보안:
      - chat_id 화이트리스트는 이미 poller 에서 적용됨
      - 시크릿 포함 메시지는 처리 직후 Telegram API 로 삭제
      - 로그에는 [REDACTED] 로 남김 (poller 쪽에서 처리)
    """
    if not args:
        return _reply(
            "*자격증명 직접 교체*\n\n"
            "*사용법*:\n"
            "`/setcreds paper <APP_KEY> <APP_SECRET> <계좌번호>`\n"
            "`/setcreds live <APP_KEY> <APP_SECRET> <계좌번호> confirm`\n\n"
            "*예시*:\n"
            "`/setcreds paper PSXXXyyy... longBase64String== 50181867`\n\n"
            "*주의*:\n"
            "- 모의(`paper`) 는 즉시 적용\n"
            "- 실전(`live`) 은 마지막에 `confirm` 필수\n"
            "- 시크릿이 포함된 메시지는 자동 삭제됩니다\n"
            "- 파일에 쓰는 방식(`nano ... credentials.env` + `/reload`) 도 여전히 사용 가능"
        )

    mode = args[0].lower()
    if mode not in ("paper", "live"):
        return _reply("첫 인자는 `paper` 또는 `live` 여야 합니다")

    if len(args) < 4:
        need = "KEY SECRET 계좌번호" + (" confirm" if mode == "live" else "")
        return _reply(
            f"인자 부족.\n"
            f"`/setcreds {mode} {need}` 형태로 입력하세요."
        )

    app_key = args[1]
    app_secret = args[2]
    account = args[3]

    # 실전은 confirm 필수
    if mode == "live":
        if len(args) < 5 or args[4].lower() != "confirm":
            return _reply(
                "🚨 *실전 계좌 자격증명 교체*\n\n"
                "실수 방지를 위해 마지막에 `confirm` 을 붙여야 합니다:\n"
                f"`/setcreds live <KEY> <SECRET> <계좌> confirm`\n\n"
                "실전 키는 실제 돈이 움직이는 계좌입니다. 신중하세요."
            )

    # 형식 검증
    if not (10 <= len(app_key) <= 64):
        return _reply(f"앱키 길이 이상: {len(app_key)}자 (보통 36자)")
    if not (40 <= len(app_secret) <= 256):
        return _reply(f"시크릿 길이 이상: {len(app_secret)}자 (보통 180자)")
    if not account.isdigit() or not (8 <= len(account) <= 10):
        return _reply(f"계좌번호 형식 이상: `{account}` (8자리 숫자 예상)")

    # 기존 credentials.env 읽어서 병합 (다른 모드 키 보존)
    existing: dict[str, str] = {}
    if CREDENTIALS_OVERRIDE_FILE.exists():
        try:
            for line in CREDENTIALS_OVERRIDE_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v
        except OSError as exc:
            return _reply(f"❌ 기존 credentials.env 읽기 실패\n`{exc}`")

    prefix = "KIS_LIVE" if mode == "live" else "KIS_PAPER"
    existing[f"{prefix}_APP_KEY"] = app_key
    existing[f"{prefix}_APP_SECRET"] = app_secret
    existing[f"{prefix}_ACCOUNT_NO"] = account
    # 상품코드는 기존 값 유지, 없으면 기본 01
    existing.setdefault(f"{prefix}_ACCOUNT_PRODUCT_CD", "01")

    # 파일 쓰기
    try:
        CREDENTIALS_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CREDENTIALS_OVERRIDE_FILE.open("w", encoding="utf-8") as f:
            f.write("# 텔레그램 /setcreds 또는 수동 편집으로 관리되는 자격증명 오버라이드\n")
            f.write("# 이 파일이 있으면 .env 의 KIS_* 값을 덮어씀\n")
            f.write("# 마지막 갱신: /setcreds 또는 nano 편집\n\n")
            for k in sorted(existing.keys()):
                f.write(f"{k}={existing[k]}\n")
        try:
            CREDENTIALS_OVERRIDE_FILE.chmod(0o600)
        except OSError:
            pass
        # watcher 가 중복 반응하지 않도록 mtime 기록 선반영
        try:
            runtime_state.credentials_last_mtime = CREDENTIALS_OVERRIDE_FILE.stat().st_mtime
        except OSError:
            pass
    except OSError as exc:
        return _reply(f"❌ credentials.env 파일 쓰기 실패\n`{exc}`")

    # 런타임 반영
    applied_now = False
    try:
        load_credentials_override()
        # 현재 활성 모드와 일치할 때만 KisClient 교체
        if ctx.settings.kis.mode == mode:
            new_cfg = build_trade_cfg(mode)
            with ctx.trading_lock:
                old_kis = ctx.kis
                new_kis = KisClient.from_settings_with_override(ctx.settings, new_cfg)
                ctx.settings.kis = new_cfg
                ctx.kis = new_kis
                try:
                    old_kis.close()
                except Exception:
                    pass

            # 해당 모드 토큰 캐시만 삭제
            from pathlib import Path
            tokens_dir = Path(__file__).resolve().parent.parent.parent / "tokens"
            try:
                token_file = tokens_dir / f"kis_token_{mode}.json"
                if token_file.exists():
                    token_file.unlink()
            except OSError:
                pass

            if mode == "paper":
                expiry.mark_updated()
            applied_now = True
    except Exception as exc:
        log.exception("setcreds 런타임 반영 실패")
        return _reply(
            f"⚠️ 파일 저장은 됐지만 런타임 반영 실패\n"
            f"`{exc}`\n\n"
            f"/reload 로 재시도 가능.",
            delete_original=True,
        )

    badge = mode_badge(mode)
    if applied_now:
        status_line = "✅ *즉시 반영됨*"
    else:
        status_line = (
            f"💾 파일에 저장됨 (현재 활성 모드는 `{ctx.settings.kis.mode}` 이라 이 값은 대기)"
        )

    extra = ""
    if mode == "paper" and applied_now:
        extra = f"\n만료 카운트다운: {expiry.PAPER_EXPIRY_DAYS}일 리셋됨"

    return _reply(
        f"✅ *자격증명 교체 완료* {badge}\n\n"
        f"계좌: `{account}`\n"
        f"앱키 앞 12자: `{app_key[:12]}...`\n"
        f"{status_line}{extra}\n\n"
        f"_원본 /setcreds 메시지는 자동 삭제됩니다._\n"
        f"`/status` 로 동작 확인.",
        delete_original=True,
    )


def cmd_restart(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """컨테이너 완전 재시작.

    /reload 와 다른 점: /reload 는 Python 프로세스 내부에서 자격증명만 교체하지만,
    /restart 는 Python 프로세스 자체를 종료한다. docker-compose 의
    restart: unless-stopped 정책에 의해 Docker 가 자동으로 새 컨테이너를 띄운다.

    동작:
      1. 텔레그램에 '재시작 시작' 응답 전송 (return)
      2. 응답 전송 여유를 위해 2초 대기 후 SIGTERM 전송 (백그라운드 스레드)
      3. SIGTERM → main.py 의 _shutdown 핸들러 → scheduler.shutdown →
         sys.exit(0) → Docker 가 컨테이너 재생성
      4. 새 컨테이너가 기동되며 '봇 기동' 메시지 발송

    총 소요 시간: 약 10~20초 (이미지 다운로드 없이 순수 재시작).
    """
    import os
    import signal
    import threading
    import time

    def _delayed_kill() -> None:
        time.sleep(2)  # 응답 메시지가 먼저 전송되도록 잠시 대기
        log.warning("/restart 요청에 의한 SIGTERM 전송")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_delayed_kill, daemon=True).start()

    return _reply(
        "🔄 *컨테이너 재시작 요청*\n\n"
        "2초 후 봇 프로세스가 종료되고 Docker 가 새로 띄웁니다.\n"
        "약 10~20초 후 *봇 기동* 메시지가 도착하면 완료입니다.\n\n"
        "_이 동안 텔레그램 커맨드는 일시적으로 응답하지 않습니다._"
    )
