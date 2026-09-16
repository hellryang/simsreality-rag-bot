"""FastAPI 엔트리포인트.

대시보드(/admin, /user, /api/*)와 카카오워크 봇(/kakao/*, /health)이 한 앱에
함께 올라간다. 경로가 겹치지 않아 서로 영향을 주지 않는다.
"""
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

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import sqlite3

app = FastAPI(title="AI 업무협업 플랫폼 연동 및 자동화 서비스")
templates = Jinja2Templates(directory="app/templates")

# 연간 토큰 한도 설정 (예: 12,000,000 토큰 / 필요시 수치 변경 가능)
ANNUAL_TOKEN_LIMIT = 12000000

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            user_id TEXT,
            message TEXT,
            mode TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            used_tokens INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ChatRequest(BaseModel):
    user_id: str
    message: str
    mode: Optional[str] = "USER"

@app.get("/admin", response_class=HTMLResponse)
async def read_admin(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")

@app.get("/user", response_class=HTMLResponse)
@app.get("/user/{username}", response_class=HTMLResponse)
async def read_user(request: Request, username: Optional[str] = "임혜량"):
    return templates.TemplateResponse(request=request, name="user.html", context={"username": username})

@app.get("/api/tokens")
async def get_tokens():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT SUM(used_tokens), SUM(input_tokens), SUM(output_tokens), COUNT(*) FROM logs")
    row = cursor.fetchone()
    
    total_tok = row[0] or 0
    input_tok = row[1] or 0
    output_tok = row[2] or 0
    req_count = row[3] or 0
    
    cursor.execute("SELECT COUNT(*) FROM logs WHERE mode = 'SUMMARY'")
    summary_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM logs WHERE mode = 'NOTION'")
    notion_count = cursor.fetchone()[0] or 0
    
    conn.close()
    
    summary_percent = round((summary_count / req_count) * 100) if req_count > 0 else 0
    notion_percent = round((notion_count / req_count) * 100) if req_count > 0 else 0
    
    # 연간 토큰 관련 계산
    annual_tokens = total_tok  # 누적 사용 토큰
    remaining_annual_tokens = max(0, ANNUAL_TOKEN_LIMIT - annual_tokens)
    annual_usage_percentage = round((annual_tokens / ANNUAL_TOKEN_LIMIT) * 100, 1) if ANNUAL_TOKEN_LIMIT > 0 else 0
    
    return {
        "today_tokens": total_tok,
        "weekly_tokens": total_tok,
        "monthly_tokens": total_tok,
        "annual_tokens": annual_tokens,
        "annual_limit": ANNUAL_TOKEN_LIMIT,
        "remaining_annual_tokens": remaining_annual_tokens,
        "annual_usage_percentage": annual_usage_percentage,
        "total_requests": req_count,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "daily_usage": [0, 0, 0, 0, 0, 0, total_tok],
        "summary_count": summary_count,
        "summary_percent": summary_percent,
        "notion_count": notion_count,
        "notion_percent": notion_percent
    }

@app.get("/api/user/{username}/tokens")
async def get_user_tokens(username: str):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT SUM(used_tokens), SUM(input_tokens), SUM(output_tokens), COUNT(*) 
        FROM logs WHERE user_id = ?
    """, (username,))
    row = cursor.fetchone()
    conn.close()
    
    total_tok = row[0] or 0
    input_tok = row[1] or 0
    output_tok = row[2] or 0
    req_count = row[3] or 0
    
    return {
        "username": username,
        "today_tokens": total_tok,
        "weekly_tokens": total_tok,
        "monthly_tokens": total_tok,
        "total_requests": req_count,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "daily_usage": [0, 0, 0, 0, 0, 0, total_tok]
    }

@app.get("/api/logs")
async def get_logs():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, user_id, message, mode, used_tokens FROM logs ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    
    logs = [
        {
            "timestamp": r[0],
            "user_id": r[1],
            "message": r[2],
            "mode": r[3],
            "used_tokens": r[4]
        } for r in rows
    ]
    return {"logs": logs}

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    input_tok = len(req.message) * 2
    response_text = f"[{req.user_id}] 님의 요청 ('{req.message}')에 대한 카카오워크 AI 봇 응답입니다."
    output_tok = len(response_text) * 2
    total_tok = input_tok + output_tok
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO logs (timestamp, user_id, message, mode, input_tokens, output_tokens, used_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, req.user_id, req.message, req.mode, input_tok, output_tok, total_tok))
    conn.commit()
    conn.close()

    return {
        "reply": response_text,
        "user_id": req.user_id,
        "used_tokens": total_tok
    }

# --- 카카오워크 봇 ---------------------------------------------------
#
# 이 세 줄이 없으면 /kakao/request, /kakao/callback, /health 가 404 가 되어
# 봇이 동작하지 않는다(머지할 때 가장 놓치기 쉬운 부분).
from app.api import health, kakao_events, slack_events  # noqa: E402

app.include_router(health.router)
app.include_router(slack_events.router)
app.include_router(kakao_events.router)
