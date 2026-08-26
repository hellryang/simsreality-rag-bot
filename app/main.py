from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path
from backend.notion_service import fetch_notion_schedules  # 노션 연동 모듈 불러오기

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

@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    # 실제 노션 API를 호출하여 실시간 데이터베이스 정보를 가져옵니다.
    response_text = await fetch_notion_schedules(mode=req.mode)
    return {"response": response_text}