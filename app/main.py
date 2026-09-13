__import__('pysqlite3')
import sys
sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
from fastapi import FastAPI

from app.api import health, kakao_events, slack_events

app = FastAPI(title="AI 업무협업 플랫폼 연동 및 자동화 서비스")

app.include_router(health.router)
app.include_router(slack_events.router)
app.include_router(kakao_events.router)
