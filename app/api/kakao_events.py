"""KakaoWork Webhook 수신.

토큰 환경변수화 + 서명/출처 검증 원칙은 Slack과 동일하게 적용한다.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/kakao", tags=["kakaowork"])
