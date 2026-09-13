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
import logging

from fastapi import FastAPI

from app.api import health, kakao_events, slack_events

# uvicorn 은 자기 로거만 설정하고 앱 로거(app.*)는 손대지 않는다. 그래서
# 배포 환경의 journalctl 에 접근 기록만 찍히고 우리 logger.info 는 사라진다.
# "요청이 도달했는가", "어느 버튼이 눌렸는가", "캘린더를 조회했는가"를
# 구분할 수 없어 진단이 막힌다. 배포에서는 로그가 유일한 관찰 수단이다.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    force=True,  # uvicorn 이 먼저 설정했더라도 덮어쓴다
)

# 라이브러리가 수십 줄씩 쏟아내면 정작 볼 것이 묻힌다.
for _noisy in ("httpx", "httpcore", "anthropic", "urllib3", "chromadb"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = FastAPI(title="AI 업무협업 플랫폼 연동 및 자동화 서비스")

app.include_router(health.router)
app.include_router(slack_events.router)
app.include_router(kakao_events.router)
