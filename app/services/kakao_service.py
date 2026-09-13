"""카카오워크 업무 요청 처리."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.schemas import Document
from app.services.claude_service import extract_calendar_event, summarize_work_request
from app.services.claude_service import answer_with_citations
from app.services.embedder import chunk_document
from app.services.notion_service import create_calendar_event, create_work_request_page
from app.services.qa_pipeline import search_documents
from app.services.vector_store import VectorStore


async def answer_query(text: str) -> str:
    """ChromaDB 검색 결과를 바탕으로 질문에 답한다."""
    hits = search_documents(text)
    answer = await answer_with_citations(text, hits)
    return answer.text


def extract_message(payload: dict[str, Any]) -> tuple[str, str]:
    """카카오워크 이벤트에서 메시지와 사용자 식별자를 꺼낸다.

    카카오워크 관리자센터에서 사용하는 이벤트 스키마가 앱 설정에 따라
    다를 수 있어 대표적인 필드만 허용한다. 실제 payload가 다르면 로그의
    원문을 기준으로 이 함수의 필드를 맞춰야 한다.
    """
    candidates = (
        payload.get("text"),
        payload.get("utterance"),
        payload.get("message", {}).get("text")
        if isinstance(payload.get("message"), dict)
        else None,
    )
    text = next((value.strip() for value in candidates if isinstance(value, str) and value.strip()), "")

    user = payload.get("user", {})
    user_id = (
        user.get("id") if isinstance(user, dict) else None
    ) or payload.get("user_id") or "unknown"
    return text, str(user_id)


async def handle_message(payload: dict[str, Any]) -> dict[str, str]:
    """등록 요청은 저장하고 조회 질문은 Notion·ChromaDB에서 답변한다."""
    text, user_id = extract_message(payload)
    if not text:
        raise ValueError("카카오워크 메시지 본문을 찾을 수 없습니다.")

    is_query = any(
        keyword in text
        for keyword in ("누구", "언제", "몇 시", "있어", "알려", "조회", "찾아")
    )
    is_calendar_request = any(keyword in text for keyword in ("회의", "일정", "미팅"))

    if is_query:
        return {"text": await answer_query(text), "notion_url": ""}

    if is_calendar_request:
        event = await extract_calendar_event(text)
        notion_url = await create_calendar_event(event)
        title = event["title"]
        summary = (
            f"제목: {event['title']}\n"
            f"날짜: {event['date']}"
            f"{f' {event['time']}' if event['time'] else ''}\n"
            f"참석자: {event['attendees'] or '없음'}"
        )
    else:
        summary = await summarize_work_request(text)
        title = f"카카오워크 업무 요청 - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        notion_url = await create_work_request_page(title, f"요청자: {user_id}\n\n{summary}")

    document = Document(
        text=f"{title}\n요청자: {user_id}\n{summary}",
        source="kakaowork",
        url=notion_url,
        title=title,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    VectorStore().add(chunk_document(document))

    return {
        "text": f"업무 요청을 저장했습니다.\n{summary}",
        "notion_url": notion_url,
    }
