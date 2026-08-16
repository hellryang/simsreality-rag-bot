"""Slack 데이터 수집·발송.

`scrub_pii`는 Slack뿐 아니라 Notion 수집에서도 쓰이므로 실제 구현은
`app/core/security.py`에 두고 여기서는 이름만 다시 내보낸다.
계약 테스트(tests/test_contracts.py)가 이 경로로 import하기 때문에
이름을 옮기지 않고 유지한다.
"""
from app.core.security import scrub_pii

__all__ = ["scrub_pii"]
