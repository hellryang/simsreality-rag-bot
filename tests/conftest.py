"""pytest 공통 설정.

`app.core.config`는 모듈을 import하는 순간 `Settings()`를 만든다.
그래서 `.env`가 없는 환경(예: GitHub Actions CI)에서는 import만 해도
ValidationError가 나서 테스트 수집 자체가 실패한다.

여기서 더미 키를 미리 넣어두면 테스트는 `.env` 없이도 돌아간다.
실제 값이 아니므로 외부 API를 호출하는 테스트에는 쓸 수 없고,
순수 로직(청킹, 스키마, 프롬프트 조립) 테스트용이다.
"""
import os

_DUMMY_ENV = {
    "NOTION_API_KEY": "ntn_test",
    "NOTION_DATABASE_ID": "test_database_id",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "test_signing_secret",
    "KAKAOWORK_APP_KEY": "test_app_key",
}

for _key, _value in _DUMMY_ENV.items():
    # 이미 진짜 값이 들어 있으면 덮어쓰지 않는다.
    os.environ.setdefault(_key, _value)
