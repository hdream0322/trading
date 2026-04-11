"""KIS Auto Trading Bot package.

버전 문자열은 다음 우선순위로 결정:
1. 환경변수 BOT_VERSION (Docker 이미지 빌드 시 주입되는 값)
2. 하드코딩된 fallback (로컬 개발 시)

Docker 빌드 과정(GitHub Actions)에서는 git tag 이름이 BOT_VERSION으로 전달됩니다.
로컬에서 직접 실행할 땐 아래 _LOCAL_VERSION 값이 사용됩니다.
"""
from __future__ import annotations

import os

_LOCAL_VERSION = "0.4.0-dev"
__version__ = os.environ.get("BOT_VERSION", _LOCAL_VERSION).strip() or _LOCAL_VERSION
