"""/update 커맨드 — 봇 자동/수동 업데이트, 릴리스 노트 조회."""
from __future__ import annotations

import logging
from typing import Any

from trading_bot import __version__ as bot_version
from trading_bot.bot import update_manager
from trading_bot.bot.context import BotContext
from trading_bot.bot.keyboards import _reply

log = logging.getLogger(__name__)


def cmd_update(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """봇 업데이트 조작.

    사용법:
      /update                 — 현재/최신 버전 표시 + 업데이트 필요 여부 안내
      /update confirm         — 최신 버전으로 업데이트 실행 (Watchtower 호출)
      /update notes           — 지금 버전의 릴리스 노트 보기
      /update notes 0.2.9     — 특정 버전의 릴리스 노트 보기
      /update enable          — 자동 업데이트 켜기
      /update disable         — 자동 업데이트 끄기
      /update status          — 자동 업데이트 상태 확인
    """
    if not args:
        return _check_update(ctx)

    sub = args[0].lower()
    if sub == "confirm":
        return _apply_update(ctx)
    if sub == "notes" or sub == "note":
        return cmd_notes(ctx, args[1:])
    if sub == "enable":
        update_manager.enable_auto()
        return _reply(
            "✅ *자동 업데이트 켜짐*\n"
            "매일 02:00 KST 에 새 버전이 있으면 자동으로 반영됩니다.\n"
            "지금 확인하려면 `/update` 입력."
        )
    if sub == "disable":
        update_manager.disable_auto(reason="telegram /update disable")
        return _reply(
            "🛑 *자동 업데이트 꺼짐*\n"
            "이제 02:00 KST 자동 업데이트가 스킵됩니다.\n"
            "수동 업데이트는 여전히 가능합니다 — `/update` 로 확인, "
            "`/update confirm` 으로 실행.\n"
            "다시 켜려면 `/update enable`."
        )
    if sub == "status":
        enabled = update_manager.is_auto_enabled()
        if enabled:
            return _reply(
                "*자동 업데이트 상태*\n"
                "• 현재: ✅ 켜짐\n"
                "• 스케줄: 매일 02:00 KST (장외 시간)\n"
                "• 수동 확인: `/update`\n"
                "• 수동 실행: `/update confirm`\n"
                "• 끄기: `/update disable`"
            )
        else:
            since = update_manager.disabled_since() or "(시각 불명)"
            return _reply(
                "*자동 업데이트 상태*\n"
                f"• 현재: 🛑 꺼짐\n"
                f"• 꺼진 시각: `{since}`\n"
                "• 수동 확인: `/update` (여전히 가능)\n"
                "• 수동 실행: `/update confirm`\n"
                "• 다시 켜기: `/update enable`"
            )

    return _reply(
        "*업데이트 명령어*\n"
        "`/update` — 현재/최신 버전 확인\n"
        "`/update confirm` — 최신 버전으로 업데이트 실행\n"
        "`/update notes` — 지금 버전 릴리스 노트\n"
        "`/update notes 0.2.9` — 특정 버전 릴리스 노트\n"
        "`/update enable` — 자동 업데이트 켜기\n"
        "`/update disable` — 자동 업데이트 끄기\n"
        "`/update status` — 자동 업데이트 상태 확인"
    )


def _check_update(ctx: BotContext) -> dict[str, Any]:
    """업데이트 확인만 하고 필요 여부를 안내. 실제 적용은 /update confirm."""
    # 현재 버전 (Docker 이미지에 주입된 BOT_VERSION)
    current_version = bot_version

    # 최신 릴리스 버전 (GitHub Releases API)
    latest_version: str | None = None
    latest_err: str | None = None
    try:
        latest_version = update_manager.fetch_latest_release_version()
    except Exception as exc:
        latest_err = str(exc)
        log.warning("최신 릴리스 버전 조회 실패: %s", exc)

    # digest 비교 — 실제 업데이트 필요 여부는 이걸로 판단
    has_update: bool | None = None
    try:
        has_update, _, _ = update_manager.check_for_update()
    except Exception as exc:
        log.warning("digest 비교 실패: %s", exc)

    lines = [
        "*업데이트 확인*",
        "",
        f"현재 버전: `{current_version}`",
        f"최신 버전: `{latest_version or '?'}`",
    ]
    if latest_err:
        lines.append(f"  _최신 릴리스 조회 실패: {latest_err[:80]}_")
    lines.append("")

    if has_update is False:
        lines.append("✅ *이미 최신 버전이에요*")
        lines.append("지금은 업데이트할 게 없어요.")
    elif has_update is True:
        lines.append("🆕 *새 버전이 있어요*")
        lines.append("")
        lines.append("적용하려면 아래 명령어를 입력하세요:")
        lines.append("`/update confirm`")
        lines.append("")
        lines.append("_약 30~60초 뒤 봇이 자동으로 다시 시작돼요._")
        lines.append("_그동안 잠깐 응답이 멈출 수 있어요._")
    else:
        # digest 비교 실패 — 사용자가 직접 판단
        lines.append("❓ *업데이트 여부를 확인할 수 없어요*")
        lines.append("")
        lines.append("서버 연결에 문제가 있어서 버전 비교를 못했어요.")
        lines.append("그래도 업데이트를 시도하려면 `/update confirm` 을 입력하세요.")

    return _reply("\n".join(lines))


def _apply_update(ctx: BotContext) -> dict[str, Any]:
    """/update confirm — digest 비교 후 필요할 때만 Watchtower 호출."""
    token = ctx.settings.watchtower_http_token
    current_version = bot_version

    # 선행 체크: 이미 최신이면 Watchtower 호출 자체를 건너뛴다.
    try:
        has_update, _, _ = update_manager.check_for_update()
    except Exception as exc:
        log.warning("digest 비교 실패, Watchtower 에 맡김: %s", exc)
        has_update = True

    if not has_update:
        return _reply(f"✅ 현재 최신 버전입니다 (`{current_version}`)")

    # 릴리스 정보 (버전 + 태그 메시지) 조회 — 실패해도 업데이트는 진행
    info: dict[str, str] | None = None
    try:
        info = update_manager.fetch_latest_release_info()
    except Exception as exc:
        log.warning("릴리스 정보 조회 실패: %s", exc)

    try:
        update_manager.trigger_update(token)
    except Exception as exc:
        return _reply(f"❌ *업데이트 요청 실패*\n`{exc}`")

    latest_version = (info or {}).get("tag") or "?"
    lines = [
        "🔄 *업데이트 중...*",
        "_잠시만 기다려주세요_",
        "",
        f"📦 `{current_version}` → `{latest_version}`",
        "⏳ 약 30~60초 후 자동으로 재시작됩니다.",
    ]

    summary = _summarize_release_body((info or {}).get("body") or "")
    if summary:
        lines.append("")
        lines.append("📋 *이번 변경 사항*")
        lines.append("```")
        lines.append(summary)
        lines.append("```")

    return _reply("\n".join(lines))


def cmd_notes(ctx: BotContext, args: list[str]) -> dict[str, Any]:
    """현재 (또는 지정한) 버전의 릴리스 노트를 표시.

    사용법:
      /notes          — 지금 실행 중인 버전의 릴리스 노트
      /notes 0.2.9    — 특정 버전의 릴리스 노트 ('v' 접두사는 있어도/없어도 됨)

    내부적으로 GitHub Git Data API 로 annotated tag 의 message 를 가져온다.
    """
    if args:
        raw_version = args[0].strip().lstrip("vV")
    else:
        raw_version = bot_version

    # 로컬 개발 빌드 (예: '0.2.0-dev') 에는 대응되는 태그가 없다
    if not raw_version or "dev" in raw_version.lower() or "dirty" in raw_version.lower():
        return _reply(
            f"ℹ️ 현재 버전은 `{raw_version or '?'}` — 로컬 개발 빌드라 릴리스 노트가 없어요.\n"
            f"릴리스된 버전만 노트를 조회할 수 있습니다.\n\n"
            f"예: `/notes 0.2.10`"
        )

    tag_name = f"v{raw_version}"
    try:
        body = update_manager.fetch_tag_annotation(tag_name)
    except Exception as exc:
        log.warning("태그 annotation 조회 실패: %s", exc)
        return _reply(f"❌ 릴리스 노트 조회 실패\n`{exc}`")

    if not body:
        return _reply(
            f"ℹ️ `{tag_name}` 릴리스 노트를 찾을 수 없습니다.\n\n"
            f"GitHub 에서 직접 확인:\n"
            f"github.com/hdream0322/trading/releases/tag/{tag_name}"
        )

    summary = _summarize_release_body(body)
    if not summary:
        return _reply(f"ℹ️ `{tag_name}` 릴리스 노트 본문이 비어 있습니다.")

    header = (
        f"📋 *릴리스 노트* `{raw_version}`"
        if args
        else f"📋 *지금 버전 릴리스 노트* `{raw_version}`"
    )
    return _reply(f"{header}\n```\n{summary}\n```")


def _summarize_release_body(body: str, max_chars: int = 1500) -> str:
    """릴리스 바디/태그 메시지에서 사용자에게 보여줄 요약만 추출.

    - `---` 구분선 또는 `## Docker` / `## 배포` / `## NAS` 헤딩이 나오면 거기서 자름
    - 연속된 빈 줄은 하나로 압축
    - 트리플 백틱은 Telegram pre block 과 충돌하므로 작은따옴표 3개로 치환
    - `max_chars` 를 넘으면 뒤를 자르고 안내 문구 추가
    """
    if not body:
        return ""
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("## ") and (
            "Docker" in stripped or "배포" in stripped or "NAS" in stripped
        ):
            break
        # 커밋 메타데이터 (Co-Authored-By, Signed-off-by 등) 는 사용자에게 노이즈
        if stripped.lower().startswith(("co-authored-by:", "signed-off-by:")):
            continue
        kept.append(line)

    compact: list[str] = []
    prev_blank = False
    for line in kept:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        compact.append(line)
        prev_blank = blank

    text = "\n".join(compact).strip()
    text = text.replace("```", "'''")  # Telegram pre block 안전
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n… (전체 내용은 GitHub Release 페이지 참고)"
    return text
