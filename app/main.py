from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
import os

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

class ChatRequest(BaseModel):
    message: str
    mode: str = "USER"

@app.get("/", response_class=HTMLResponse)
def get_index_page():
    file_path = TEMPLATES_DIR / "index.html"
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

# 실제 챗봇 연동 API 엔드포인트
@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    user_msg = req.message
    current_mode = req.mode

    # TODO: 프로젝트 내의 Claude / Notion / Kakaowork 서비스 모듈 호출 로직 연결
    # 예시: response_text = await run_rag_pipeline(user_msg, current_mode)
    
    # 임시 연동 응답 예시 (백엔드 모듈 연결 확인용)
    response_text = f"[{current_mode} 모드] 요청하신 '{user_msg}'에 대한 노션 데이터베이스 및 대화 분석이 완료되었습니다."
    
    return {"response": response_text}