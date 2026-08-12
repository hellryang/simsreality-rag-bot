"""Notion 데이터 수집.

Notion·Slack·KakaoWork는 각각 독립적으로 직접 벡터 DB에 적재된다.
다른 소스의 중간 경유지로 사용하지 않는다.

수집 결과는 vector_store에 그대로 넣을 수 있도록
{"text", "source", "url", "title", "created_at"} 형태의 dict 리스트로 돌려준다.
이 메타데이터가 있어야 나중에 답변에 출처(citation)를 붙일 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from notion_client import AsyncClient
from notion_client.errors import APIResponseError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Notion Rate Limit은 평균 초당 약 3회. 연속 호출 사이에 최소 간격을 둔다.
_REQUEST_INTERVAL_SEC = 0.34
_MAX_RETRY = 4

# 본문에서 텍스트를 뽑아낼 블록 타입. 그 외(이미지, 파일 등)는 건너뛴다.
_TEXT_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "toggle",
    "code",
)


async def _with_backoff(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """지수 백오프(exponential backoff) 재시도 래퍼.

    지수 백오프는 실패할 때마다 대기 시간을 2배씩 늘려가며 다시 시도하는 방식이다.
    외부 API는 순간적으로 429(요청 과다)나 5xx를 돌려주는 일이 잦은데,
    곧바로 재시도하면 또 막히기 때문에 간격을 벌리면서 시도한다.
    """
    delay = 1.0
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            return await func(*args, **kwargs)
        except APIResponseError as exc:
            # 401/403/404는 몇 번을 다시 보내도 결과가 같으므로 즉시 중단한다.
            if exc.status in (401, 403, 404):
                logger.error(
                    "Notion 요청 거부 (status=%s). API 키가 맞는지, "
                    "대상 DB가 Integration과 '연결'되어 있는지 확인하세요.",
                    exc.status,
                )
                raise
            if attempt == _MAX_RETRY:
                logger.error("Notion 요청이 %d회 모두 실패했습니다.", _MAX_RETRY)
                raise
            logger.warning(
                "Notion 요청 실패 (status=%s). %.1f초 후 재시도 %d/%d",
                exc.status, delay, attempt, _MAX_RETRY,
            )
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("도달할 수 없는 분기")  # 방어용


def _extract_plain_text(rich_text: list[dict[str, Any]]) -> str:
    """Notion의 rich_text 배열에서 순수 텍스트만 이어붙인다."""
    return "".join(item.get("plain_text", "") for item in rich_text)


def _extract_title(page: dict[str, Any]) -> str:
    """페이지 properties 중 type이 'title'인 항목에서 제목을 꺼낸다.

    제목 property의 '이름'은 DB마다 다르므로(이름/Name/제목...) 이름이 아니라
    type으로 찾아야 안전하다.
    """
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            title = _extract_plain_text(prop.get("title", []))
            if title:
                return title
    return "(제목 없음)"


async def _fetch_page_text(client: AsyncClient, page_id: str) -> str:
    """페이지 본문 블록을 읽어 하나의 문자열로 합친다.

    하위 블록(자식)까지 재귀로 따라가지는 않는다. 1단계 깊이만 읽어도
    회의록·업무 문서 대부분의 본문은 확보된다.
    """
    lines: list[str] = []
    cursor: str | None = None

    while True:
        await asyncio.sleep(_REQUEST_INTERVAL_SEC)
        response = await _with_backoff(
            client.blocks.children.list,
            block_id=page_id,
            page_size=100,
            **({"start_cursor": cursor} if cursor else {}),
        )

        for block in response.get("results", []):
            block_type = block.get("type")
            if block_type not in _TEXT_BLOCK_TYPES:
                continue
            text = _extract_plain_text(block.get(block_type, {}).get("rich_text", []))
            if text.strip():
                lines.append(text)

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return "\n".join(lines)


async def collect_notion_documents(limit: int | None = None) -> list[dict[str, Any]]:
    """Notion 데이터베이스의 페이지를 수집한다.

    Args:
        limit: 가져올 최대 페이지 수. None이면 전체. 개발 중에는 5~10 정도로
            제한해 두면 Rate Limit에 걸리지 않고 빠르게 확인할 수 있다.

    Returns:
        [{"text", "source", "url", "title", "created_at"}, ...]
    """
    client = AsyncClient(auth=settings.notion_api_key)
    documents: list[dict[str, Any]] = []
    cursor: str | None = None

    try:
        while True:
            await asyncio.sleep(_REQUEST_INTERVAL_SEC)
            # 참고: Notion API 버전에 따라 데이터 소스(data source) 단위 조회로
            # 바뀔 수 있다. 아래 호출에서 400이 나면 설치된 notion-client 버전과
            # https://developers.notion.com 의 Query a database 문서를 확인할 것.
            response = await _with_backoff(
                client.databases.query,
                database_id=settings.notion_database_id,
                page_size=100,
                **({"start_cursor": cursor} if cursor else {}),
            )

            for page in response.get("results", []):
                title = _extract_title(page)
                body = await _fetch_page_text(client, page["id"])

                # 제목만 있고 본문이 비어도 제목 자체가 검색에 쓸모가 있으므로 남긴다.
                documents.append(
                    {
                        "text": f"{title}\n{body}".strip(),
                        "source": "notion",
                        "url": page.get("url", ""),
                        "title": title,
                        "created_at": page.get("created_time", ""),
                    }
                )

                if limit is not None and len(documents) >= limit:
                    logger.info("Notion 페이지 %d건 수집 (limit 도달)", len(documents))
                    return documents

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

    finally:
        # AsyncClient는 내부에 HTTP 연결을 열어두므로 반드시 닫는다.
        await client.aclose()

    logger.info("Notion 페이지 %d건 수집 완료", len(documents))
    return documents


if __name__ == "__main__":
    # 단독 실행 확인용: python -m app.services.notion_service
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async def _main() -> None:
        docs = await collect_notion_documents(limit=5)
        print(f"\n수집된 문서: {len(docs)}건\n")
        for i, doc in enumerate(docs, start=1):
            preview = doc["text"][:80].replace("\n", " ")
            print(f"[{i}] {doc['title']}")
            print(f"    출처: {doc['url']}")
            print(f"    작성: {doc['created_at']}")
            print(f"    본문: {preview}...\n")

    asyncio.run(_main())
