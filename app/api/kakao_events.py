"""카카오워크 Webhook 수신."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.kakao_service import handle_message

router = APIRouter(prefix="/kakao", tags=["kakaowork"])


@router.post("/webhook")
async def kakao_webhook(request: Request) -> dict[str, str]:
    """카카오워크가 보낸 업무 요청을 저장하고 답변 형식으로 반환한다."""
    payload: dict[str, Any] = await request.json()
    try:
        return await handle_message(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
