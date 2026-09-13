"""FastAPI 엔트리포인트."""
import sys

# 리눅스 배포 환경의 sqlite3 가 낮은 버전이면 ChromaDB 가 기동을 거부한다.
# pysqlite3 를 표준 sqlite3 자리에 끼워 넣어 우회한다(배포 중 실제로 겪은 문제).
#
# try 로 감싸는 이유: 개발용 윈도우 환경에는 pysqlite3 가 없고 필요하지도 않다.
# 감싸지 않으면 로컬에서 서버가 아예 기동하지 못한다. 그리고 벡터 검색을 끈
# 구성(USE_VECTOR_SEARCH=false)에서는 ChromaDB 자체를 쓰지 않으므로 없어도 된다.
try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ModuleNotFoundError:
    pass
from fastapi import FastAPI

from app.api import health, kakao_events, slack_events

app = FastAPI(title="AI 업무협업 플랫폼 연동 및 자동화 서비스")

app.include_router(health.router)
app.include_router(slack_events.router)
app.include_router(kakao_events.router)
