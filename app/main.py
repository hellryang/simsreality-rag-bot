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

    if current_mode == "ADMIN":
        response_text = (
            "🔒 [관리자 모드] 근거 자료 기반 일정 분석 결과\n\n"
            "--------------------------------------------------\n"
            "📅 날짜: 2026-08-27 (목) 14:00\n"
            "👤 작성자: 이현우 PM\n"
            "💬 대화방: [기밀] PM/부서장 전략회의방\n"
            "📝 내용: Q3 프로젝트 중간 점검 및 핵심 RAG 파이프라인 보안 이슈 논의\n"
            "--------------------------------------------------\n"
            "📅 날짜: 2026-08-28 (금) 10:00\n"
            "👤 작성자: 박서준 팀장\n"
            "💬 대화방: [기밀] PM/부서장 전략회의방\n"
            "📝 내용: 백엔드 보안 서브시스템 코드 리뷰 및 권한 검증 테스팅\n"
            "--------------------------------------------------"
        )
    else:
        response_text = (
            "📌 [사용자 모드] 근거 자료 기반 회의 일정 목록\n\n"
            "--------------------------------------------------\n"
            "📅 날짜: 2026-08-27 (목) 15:30\n"
            "👤 작성자: 김민준\n"
            "💬 대화방: CHONDAEYONG 개발 소모임방\n"
            "📝 내용: UI/UX 디자인 수정안 공유 및 대화창 프론트엔드 개선 회의\n"
            "--------------------------------------------------\n"
            "📅 날짜: 2026-08-28 (금) 14:00\n"
            "👤 작성자: 정수아\n"
            "💬 대화방: 카카오워크 연동 TF팀\n"
            "📝 내용: 카카오워크 Webhook 및 노션 API 실시간 동기화 테스트 진행\n"
            "--------------------------------------------------\n"
            "📅 날짜: 2026-09-01 (화) 10:00\n"
            "👤 작성자: 최도현\n"
            "💬 대화방: 전체 공지 및 자유 수다방\n"
            "📝 내용: 9월 1차 전체 팀 스크럼 회의 (온라인 구글 미트)"
        )

    return {"response": response_text}