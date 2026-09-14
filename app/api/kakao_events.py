"""카카오워크 Webhook 수신."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.kakao_service import extract_message
from app.services.kakao_service import handle_message
from app.services.kakao_service import answer_query

router = APIRouter(prefix="/kakao", tags=["kakaowork"])


def _blockkit_response(text: str) -> dict[str, Any]:
    """카카오워크 메시지용 BlockKit 응답을 만든다."""
    return {
        "blocks": [
            {"type": "header", "text": "업무 도우미", "style": "blue"},
            {"type": "text", "text": text},
        ]
    }


def _welcome_response() -> dict[str, Any]:
    """질문·업무 기능을 선택하는 초기 말풍선을 만든다."""
    return {
        "text": "업무 도우미",
        "is_allow_to_send_any_conversation": True,
        "blocks": [
            {"type": "header", "text": "업무 도우미", "style": "blue"},
            {
                "type": "text",
                "text": (
                    "무엇이든 물어보세요. Notion·Slack·KakaoWork에 쌓인 "
                    "문서에서 찾아 출처와 함께 답해드립니다."
                ),
                "markdown": True,
            },
            {"type": "divider"},
            {
                "type": "action",
                "elements": [
                    {
                        "type": "button",
                        "text": "질문하기",
                        "style": "primary",
                        "action": {
                            "type": "call_modal",
                            "name": "ask_question",
                            "value": "ask_question",
                        },
                    },
                    {
                        "type": "button",
                        "text": "대화 정리 요청",
                        "style": "default",
                        "action": {
                            "type": "call_modal",
                            "name": "submit_chat_log",
                            "value": "submit_chat_log",
                        },
                    },
                ],
            },
        ],
    }


def _extract_query(payload: dict[str, Any]) -> str:
    """모달 입력값에서 질문을 찾아낸다."""
    for key in ("query", "question", "text", "value", "utterance"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("actions", "params", "inputs", "values", "action", "data", "form"):
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
        "view": {
            "title": "일정·업무 검색",
            "accept": "검색",
            "decline": "취소",
            "value": "search_query",
            "blocks": [
                {
                    "type": "label",
                    "text": "검색할 질문을 입력하세요.",
                    "markdown": False,
                },
                {
                    "type": "input",
                    "name": "query",
                    "required": True,
                    "placeholder": "예: 9월 22일 회의 누구랑 해?",
                },
            ],
        },
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
        message, _ = extract_message(payload)
        if message.strip().lower() in {"메뉴", "menu"}:
            return _welcome_response()

        result = await handle_message(payload)
        return {
            "text": result["text"],
            "blocks": [{"type": "text", "text": result["text"]}],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
