from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# 월간 설정 한도 (예: 1,000,000 토큰)
MONTHLY_TOKEN_LIMIT = 1000000

# 메모리 DB (실시간 토큰 및 로그 저장)
token_usage_db = {
    "today_tokens": 0,
    "weekly_tokens": 0,
    "monthly_tokens": 0,
    "total_requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "daily_usage": [0, 0, 0, 0, 0, 0, 0]
}

user_logs = []

class ChatRequest(BaseModel):
    user_id: str
    message: str
    mode: Optional[str] = "USER"

@app.get("/admin", response_class=HTMLResponse)
async def read_admin(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/user", response_class=HTMLResponse)
@app.get("/user/{username}", response_class=HTMLResponse)
async def read_user(request: Request, username: Optional[str] = "임혜량"):
    return templates.TemplateResponse("user.html", {"request": request, "username": username})

@app.get("/api/tokens")
async def get_tokens():
    remaining_tokens = max(0, MONTHLY_TOKEN_LIMIT - token_usage_db["monthly_tokens"])
    usage_percentage = round((token_usage_db["monthly_tokens"] / MONTHLY_TOKEN_LIMIT) * 100, 1)
    
    return {
        **token_usage_db,
        "monthly_limit": MONTHLY_TOKEN_LIMIT,
        "remaining_tokens": remaining_tokens,
        "usage_percentage": usage_percentage
    }

@app.get("/api/user/{username}/tokens")
async def get_user_tokens(username: str):
    user_specific_logs = [log for log in user_logs if log["user_id"] == username]
    
    total_tokens = sum(log["used_tokens"] for log in user_specific_logs)
    input_tok = sum(log["input_tokens"] for log in user_specific_logs)
    output_tok = sum(log["output_tokens"] for log in user_specific_logs)
    req_count = len(user_specific_logs)
    
    return {
        "username": username,
        "today_tokens": total_tokens,
        "weekly_tokens": total_tokens,
        "monthly_tokens": total_tokens,
        "total_requests": req_count,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "daily_usage": [0, 0, 0, 0, 0, 0, total_tokens]
    }

@app.get("/api/logs")
async def get_logs():
    return {"logs": user_logs}

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    input_tok = len(req.message) * 2
    response_text = f"[{req.user_id}] 님의 요청 ('{req.message}')에 대한 카카오워크 AI 봇 응답입니다."
    output_tok = len(response_text) * 2
    total_tok = input_tok + output_tok

    token_usage_db["today_tokens"] += total_tok
    token_usage_db["weekly_tokens"] += total_tok
    token_usage_db["monthly_tokens"] += total_tok
    token_usage_db["total_requests"] += 1
    token_usage_db["input_tokens"] += input_tok
    token_usage_db["output_tokens"] += output_tok
    token_usage_db["daily_usage"][-1] = token_usage_db["today_tokens"]

    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": req.user_id,
        "message": req.message,
        "mode": req.mode,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "used_tokens": total_tok
    }
    user_logs.insert(0, log_entry)

    return {
        "reply": response_text,
        "user_id": req.user_id,
        "used_tokens": total_tok
    }