"""Slack 이벤트 수신.

Slack은 3초 내 200 응답이 없으면 이벤트를 재전송하므로,
즉시 ack 후 실제 처리는 백그라운드 태스크로 분리한다.
event_id 기반 멱등 처리 필요.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/slack", tags=["slack"])
