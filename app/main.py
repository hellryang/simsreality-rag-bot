from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path

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
    user_msg = req.message
    current_mode = req.mode

    # -------------------------------------------------------------
    # TODO: 추후 실제 Notion API / Kakaowork API 모듈을 호출하는 부분입니다.
    # 현재는 화면 확인을 위해 실제 회의 일정 데이터를 반환하도록 설정합니다.
    # -------------------------------------------------------------

    if current_mode == "ADMIN":
        response_text = (
            "🔒 [관리자 모드] PM 및 부서장 채널 동기화 목록\n\n"
            "1. 8월 27일(목) 14:00 - Q3 프로젝트 중간 점검 회의 (부서장/PM)\n"
            "2. 8월 28일(금) 10:00 - 백엔드/RAG 파이프라인 보안 리뷰\n"
            "3. 8월 31일(월) 11:00 - 예산 및 인력 배정 논의"
        )
    else:
        response_text = (
            "📌 [사용자 모드] 현재 노션에 정리된 회의 일정입니다.\n\n"
            "• 8월 27일(목) 15:30 - CHONDAEYONG UI/UX 개선 회의\n"
            "• 8월 28일(금) 14:00 - 카카오워크 API 동기화 및 테스트\n"
            "• 9월 01일(화) 10:00 - 전체 팀 주간 스크럼"
        )

    return {"response": response_text}