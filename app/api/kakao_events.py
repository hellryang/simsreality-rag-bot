"""카카오워크 Webhook 수신."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.kakao_service import handle_message
from app.services.kakao_service import answer_query

router = APIRouter(prefix="/kakao", tags=["kakaowork"])


def _blockkit_response(text: str) -> dict[str, Any]:
    """카카오워크 모달에 표시할 BlockKit 응답을 만든다."""
    return {
        "blocks": [
            {"type": "header", "text": "업무 도우미", "style": "blue"},
            {"type": "text", "text": text},
        ]
    }


def _extract_query(payload: dict[str, Any]) -> str:
    """모달 입력값에서 질문을 찾아낸다."""
    for key in ("query", "question", "text", "value", "utterance"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("params", "inputs", "values", "action", "data", "form"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            query = _extract_query(nested)
            if query:
                return query
    return ""


@router.post("/request")
async def kakao_request(request: Request) -> dict[str, Any]:
    """버튼 클릭 시 질문 입력 모달을 반환한다."""
    await request.json()
    return {
        "blocks": [
            {"type": "header", "text": "일정·업무 검색", "style": "blue"},
            {
                "type": "input",
                "name": "query",
                "text": "질문",
                "placeholder": "예: 9월 22일 회의 누구랑 해?",
            },
            {
                "type": "button",
                "text": "검색",
                "action": {"type": "submit", "name": "search"},
            },
        ]
    }


@router.post("/callback")
async def kakao_callback(request: Request) -> dict[str, Any]:
    """모달 입력을 처리하고 검색 결과를 모달에 표시한다."""
    payload: dict[str, Any] = await request.json()
    query = _extract_query(payload)
    if not query:
        raise HTTPException(status_code=400, detail="모달 질문을 찾을 수 없습니다.")

    try:
        answer = await answer_query(query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _blockkit_response(answer)


@router.post("/webhook")
async def kakao_webhook(request: Request) -> dict[str, Any]:
    """카카오워크 Callback에 BlockKit 응답을 반환한다."""
    payload: dict[str, Any] = await request.json()
    try:
        result = await handle_message(payload)
        return {
            "text": result["text"],
            "blocks": [{"type": "text", "text": result["text"]}],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
