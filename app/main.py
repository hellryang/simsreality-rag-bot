from datetime import datetime
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from backend.notion_service import fetch_notion_schedules
from app.services.s3_service import upload_file_to_s3
from app.api.kakao_events import router as kakao_router

app = FastAPI()
app.include_router(kakao_router)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

# 실시간 토큰 집계 DB
token_usage_db = {
    "today_tokens": 0,
    "weekly_tokens": 0,
    "monthly_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_requests": 0,
    "daily_usage": [0, 0, 0, 0, 0, 0, 0]
}

# 사용자 로그 저장소
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
    return response

@app.get("/", response_class=HTMLResponse)
def get_index_page():
    return get_no_cache_response("index.html")

@app.get("/admin", response_class=HTMLResponse)
def get_admin_page():
    return get_no_cache_response("admin.html")

# 기본 사용자 페이지 (/user 접속 시 기본 임혜량 페이지)
@app.get("/user", response_class=HTMLResponse)
def get_user_default_page():
    return get_no_cache_response("user.html")

# [신규] URL 경로에 사용자 이름을 붙여 접속하는 라우트 (/user/peterpan, /user/홍길동 등)
@app.get("/user/{username}", response_class=HTMLResponse)
def get_user_specific_page(username: str):
    return get_no_cache_response("user.html")

# 전체 토큰 정보 API
@app.get("/api/tokens")
async def get_token_metrics():
    return token_usage_db

# [신규] 특정 사용자별 토큰 및 로그 데이터 조회 API
@app.get("/api/user/{username}/tokens")
async def get_specific_user_metrics(username: str):
    # 해당 사용자의 로그만 필터링
    filtered_logs = [log for log in user_logs if log["user_id"] == username]
    
    total_req = len(filtered_logs)
    today_tok = sum(log["used_tokens"] for log in filtered_logs)
    input_tok = sum(len(log["message"]) * 2 for log in filtered_logs)
    output_tok = today_tok - input_tok
    
    return {
        "username": username,
        "today_tokens": today_tok,
        "weekly_tokens": today_tok,
        "monthly_tokens": today_tok,
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "total_requests": total_req,
        "daily_usage": [0, 0, 0, 0, 0, 0, today_tok]
    }

@app.get("/api/logs")
async def get_user_logs():
    return {"logs": user_logs}


@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """업로드된 파일을 AWS S3에 저장한다."""
    try:
        return await upload_file_to_s3(file)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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