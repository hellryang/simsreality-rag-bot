from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import sqlite3

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# 연간 토큰 한도 설정 (12,000,000 토큰)
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
    
    annual_tokens = total_tok
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
