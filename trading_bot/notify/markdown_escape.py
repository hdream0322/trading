"""Telegram Bot API Markdown(v1) 호환 escape.

종목명/필드값에 _ * [ ` 가 들어가면 parse_mode=Markdown 으로 400 에러.
응답 자체가 안 오므로 silent 실패가 됨. 동적 텍스트 삽입 전 모두 거치게 함.
"""
from __future__ import annotations


_MD_SPECIALS = ("_", "*", "[", "`")


def escape_markdown(text: str) -> str:
    if text is None:
        return ""
    out = str(text)
    for ch in _MD_SPECIALS:
        out = out.replace(ch, "\\" + ch)
    return out
