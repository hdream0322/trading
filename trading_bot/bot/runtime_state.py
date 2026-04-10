"""런타임에 여러 모듈이 공유하는 상태 변수 보관소.

모듈 최상위 변수이므로 같은 Python 프로세스 안에서 import 한 모든 곳이
동일한 값을 본다. 스레드 안전이 필요하면 각 변수를 읽고 쓰는 시점에
적절한 락을 별도로 사용할 것.
"""
from __future__ import annotations

# credentials_watcher_job 이 감시하는 credentials.env 의 마지막 확인 mtime.
# - 초기값 0.0 : 아직 한 번도 확인 안 함 (최초 tick 은 baseline 기록만)
# - 봇이 직접 credentials.env 를 수정할 때 (/setcreds) 이 값을 함께 갱신하면
#   watcher 가 다음 tick 에서 중복 재로드를 하지 않는다.
credentials_last_mtime: float = 0.0
