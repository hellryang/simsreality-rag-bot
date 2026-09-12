"""카카오워크 Webhook 수신."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.services.kakao_service import handle_message

router = APIRouter(prefix="/kakao", tags=["kakaowork"])


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
