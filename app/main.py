from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pathlib import Path
from backend.notion_service import fetch_notion_schedules

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

token_usage_db = {
    "today_tokens": 0,
    "weekly_tokens": 0,
    "monthly_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_requests": 0,
    "daily_usage": [0, 0, 0, 0, 0, 0, 0]
}

user_logs = []

class ChatRequest(BaseModel):
    user_id: str = "임혜량"
    message: str
    mode: str = "USER"

def get_no_cache_response(file_name: str):
    file_path = TEMPLATES_DIR / file_name
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    response = HTMLResponse(content=content)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/", response_class=HTMLResponse)
def get_index_page():
    return get_no_cache_response("index.html")

@app.get("/admin", response_class=HTMLResponse)
def get_admin_page():
    return get_no_cache_response("admin.html")

@app.get("/user", response_class=HTMLResponse)
def get_user_page():
    return get_no_cache_response("user.html")

@app.get("/api/tokens")
async def get_token_metrics():
    return token_usage_db

@app.get("/api/logs")
async def get_user_logs():
    return {"logs": user_logs}

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    response_text = await fetch_notion_schedules(mode=req.mode)
    
    input_tok = len(req.message) * 2
    output_tok = len(response_text) * 2
    used_tok = input_tok + output_tok

    token_usage_db["today_tokens"] += used_tok
    token_usage_db["weekly_tokens"] += used_tok
    token_usage_db["monthly_tokens"] += used_tok
    token_usage_db["input_tokens"] += input_tok
    token_usage_db["output_tokens"] += output_tok
    token_usage_db["total_requests"] += 1
    token_usage_db["daily_usage"][-1] += used_tok

    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": req.user_id,
        "message": req.message,
        "mode": req.mode,
        "used_tokens": used_tok,
        "response_summary": response_text[:30] + "..." if len(response_text) > 30 else response_text
    }
    user_logs.insert(0, log_entry)

    return {
        "response": response_text,
        "used_tokens": used_tok,
        "input_tokens": input_tok,
        "output_tokens": output_tok
    }