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
from app.core.security import scrub_pii

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

# 자식 블록을 몇 단계까지 따라 내려갈지.
# 토글 안의 토글, 다단(column) 안의 표처럼 중첩이 있어서 1단계로는 부족하다.
# 무한정 내려가면 Rate Limit에 걸리므로 상한을 둔다.
_MAX_BLOCK_DEPTH = 3

# 표 한 줄 안에서 칸을 구분하는 문자.
_CELL_SEPARATOR = " | "


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


def _extract_child_pages(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """블록 목록에서 하위 페이지의 (id, 제목)만 뽑는다.

    Notion은 페이지 안에 끼워 넣은 하위 페이지를 `child_page` 타입 블록으로 준다.
    문단·표·하위 데이터베이스는 문서가 아니므로 여기서 걸러진다.
    """
    return [
        (block["id"], block.get("child_page", {}).get("title", "(제목 없음)"))
        for block in blocks
        if block.get("type") == "child_page"
    ]


async def _list_all_blocks(client: AsyncClient, page_id: str) -> list[dict[str, Any]]:
    """페이지에 달린 블록을 페이지네이션까지 따라가며 전부 가져온다.

    Notion은 한 번에 최대 100개만 주고, 더 있으면 `has_more`와 `next_cursor`로
    알려준다. 커서를 따라가지 않으면 긴 문서의 뒷부분이 통째로 누락된다.
    """
    blocks: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        await asyncio.sleep(_REQUEST_INTERVAL_SEC)
        response = await _with_backoff(
            client.blocks.children.list,
            block_id=page_id,
            page_size=100,
            **({"start_cursor": cursor} if cursor else {}),
        )
        blocks.extend(response.get("results", []))

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return blocks


def _extract_table_rows(rows: list[dict[str, Any]], has_column_header: bool) -> list[str]:
    """표의 행 블록들을 검색이 되는 문장으로 바꾼다.

    Notion 표는 `table` 블록 밑에 `table_row` 블록이 자식으로 달리고,
    각 행의 `cells`는 [[rich_text], [rich_text], ...] 로 칸마다 한 겹 더 감싸여 온다.

    **머리글을 각 행에 되붙이는 이유**: 본문은 나중에 300자 단위로 잘린다.
    표를 그대로 옮겨 적으면 "임혜량 | 완료 | 8/22" 같은 줄만 남아, 조각이 잘리는
    순간 이 값들이 무엇을 뜻하는지 알 수 없게 된다. 그래서 행마다
    "담당: 임혜량 | 상태: 완료" 로 머리글을 붙여 한 줄만 봐도 뜻이 통하게 만든다.
    임베딩은 문장의 의미로 검색하므로, 이렇게 해야 "누가 담당이야" 같은 질문에 걸린다.

    Args:
        rows: `table` 블록의 자식 블록 목록
        has_column_header: 첫 행이 머리글인지 (`table` 블록이 알려준다)

    Returns:
        행마다 한 줄씩. 빈 행은 제외한다.
    """
    cell_rows: list[list[str]] = [
        [_extract_plain_text(cell) for cell in row.get("table_row", {}).get("cells", [])]
        for row in rows
        if row.get("type") == "table_row"
    ]
    if not cell_rows:
        return []

    if not has_column_header:
        # 머리글이 없으면 붙일 이름도 없다. 값만 이어 붙인다.
        return [
            _CELL_SEPARATOR.join(cell for cell in cells if cell.strip())
            for cells in cell_rows
            if any(cell.strip() for cell in cells)
        ]

    headers = cell_rows[0]
    lines: list[str] = []

    for cells in cell_rows[1:]:
        pairs: list[str] = []
        for index, value in enumerate(cells):
            if not value.strip():
                continue
            # 머리글 칸이 비어 있을 수 있으므로 있을 때만 이름을 붙인다.
            header = headers[index] if index < len(headers) else ""
            pairs.append(f"{header}: {value}" if header.strip() else value)
        if pairs:
            lines.append(_CELL_SEPARATOR.join(pairs))

    return lines


async def _fetch_table_lines(client: AsyncClient, table_block: dict[str, Any]) -> list[str]:
    """표 블록 하나를 읽어 행 목록으로 만든다.

    행은 표 블록의 '자식'이라 블록 목록을 한 번 더 요청해야 나온다.
    """
    rows = await _list_all_blocks(client, table_block["id"])
    return _extract_table_rows(
        rows,
        has_column_header=table_block.get("table", {}).get("has_column_header", False),
    )


async def _collect_block_lines(
    client: AsyncClient, block_id: str, depth: int = 0
) -> list[str]:
    """블록을 훑어 본문 줄 목록을 만든다. 자식이 있으면 재귀로 따라 내려간다.

    토글(접힌 내용), 다단(column_list), 중첩 목록은 내용이 전부 자식 블록에
    들어 있다. 1단계만 읽으면 이 내용이 통째로 누락된다.
    """
    lines: list[str] = []

    for block in await _list_all_blocks(client, block_id):
        block_type = block.get("type")

        # 하위 페이지·하위 DB는 별도 문서로 따로 수집한다. 본문에 섞으면 중복된다.
        if block_type in ("child_page", "child_database"):
            continue

        if block_type == "table":
            lines.extend(await _fetch_table_lines(client, block))
            continue

        if block_type in _TEXT_BLOCK_TYPES:
            text = _extract_plain_text(block.get(block_type, {}).get("rich_text", []))
            if text.strip():
                lines.append(text)

        if block.get("has_children") and depth < _MAX_BLOCK_DEPTH:
            lines.extend(await _collect_block_lines(client, block["id"], depth + 1))

    return lines


async def _fetch_page_text(client: AsyncClient, page_id: str) -> str:
    """페이지 본문 블록을 읽어 하나의 문자열로 합친다."""
    return "\n".join(await _collect_block_lines(client, page_id))


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
                        "text": scrub_pii(f"{title}\n{body}".strip()),
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


async def create_work_request_page(title: str, body: str) -> str:
    """업무 요청을 루트 Notion 페이지 아래에 새 페이지로 저장한다."""
    if not settings.notion_root_page_id:
        raise RuntimeError("NOTION_ROOT_PAGE_ID가 설정되지 않았습니다.")

    client = AsyncClient(auth=settings.notion_api_key)
    try:
        response = await _with_backoff(
            client.pages.create,
            parent={"page_id": settings.notion_root_page_id},
            properties={
                "title": {
                    "title": [{"text": {"content": title[:200]}}],
                }
            },
            children=[
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": body[:2000]}}
                        ]
                    },
                }
            ],
        )
        return response.get("url", "")
    finally:
        await client.aclose()


async def _build_document(
    client: AsyncClient, page_id: str, title: str
) -> dict[str, Any] | None:
    """페이지 하나를 읽어 수집 결과 dict 한 건으로 만든다.

    본문이 비어 있으면 제목만으로도 검색에 쓸모가 있으므로 버리지 않는다.
    연락처·이메일은 벡터 DB에 들어가기 전에 여기서 마스킹한다.
    """
    body = await _fetch_page_text(client, page_id)

    await asyncio.sleep(_REQUEST_INTERVAL_SEC)
    meta = await _with_backoff(client.pages.retrieve, page_id=page_id)

    return {
        "text": scrub_pii(f"{title}\n{body}".strip()),
        "source": "notion",
        "url": meta.get("url", ""),
        "title": title,
        "created_at": meta.get("created_time", ""),
    }


async def collect_notion_page_tree(
    root_page_id: str,
    limit: int | None = None,
    max_depth: int = 2,
    include_root: bool = False,
) -> list[dict[str, Any]]:
    """페이지 아래에 하위 페이지로 붙어 있는 문서들을 수집한다.

    우리 Notion은 '2026 일경험 프로젝트' 페이지 밑에 회의록·계획서 같은 문서가
    하위 페이지로 달려 있는 구조다. 데이터베이스가 아니므로
    `collect_notion_documents()`(databases.query 방식)로는 읽히지 않는다.

    Args:
        root_page_id: 기준이 되는 페이지 ID
        limit: 가져올 최대 문서 수. None이면 전체
        max_depth: 하위의 하위까지 몇 단계나 따라갈지. 1이면 바로 아래만
        include_root: 기준 페이지 본문 자체도 문서로 넣을지.
            기본값 False다. 최상단 페이지에는 팀원 명단처럼 개인정보가
            섞이기 쉬워서 켜는 쪽을 의식적으로 고르게 했다.
            연락처·이메일은 `scrub_pii`가 마스킹하므로 켜도 안전하다.

    Returns:
        [{"text", "source", "url", "title", "created_at"}, ...]
    """
    client = AsyncClient(auth=settings.notion_api_key)
    documents: list[dict[str, Any]] = []
    visited: set[str] = set()

    async def _walk(page_id: str, title: str, depth: int) -> None:
        if page_id in visited or (limit is not None and len(documents) >= limit):
            return
        visited.add(page_id)

        document = await _build_document(client, page_id, title)
        if document:
            documents.append(document)
            logger.info("수집: %s", title)

        if depth >= max_depth:
            return

        children = _extract_child_pages(await _list_all_blocks(client, page_id))
        for child_id, child_title in children:
            if limit is not None and len(documents) >= limit:
                return
            await _walk(child_id, child_title, depth + 1)

    try:
        blocks = await _list_all_blocks(client, root_page_id)

        if include_root:
            await _walk(root_page_id, _root_title(await _with_backoff(
                client.pages.retrieve, page_id=root_page_id)), 0)

        for child_id, child_title in _extract_child_pages(blocks):
            if limit is not None and len(documents) >= limit:
                break
            await _walk(child_id, child_title, 1)

    finally:
        await client.aclose()

    logger.info("Notion 하위 페이지 %d건 수집 완료", len(documents))
    return documents


def _root_title(page: dict[str, Any]) -> str:
    """최상단 페이지의 제목을 꺼낸다. 페이지는 properties 구조가 DB와 다르다."""
    title_prop = page.get("properties", {}).get("title", {})
    return _extract_plain_text(title_prop.get("title", [])) or "(제목 없음)"


if __name__ == "__main__":
    # 단독 실행 확인용: python -m app.services.notion_service
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async def _main() -> None:
        # 설정된 쪽으로 자동 선택한다. 페이지 방식을 우선한다.
        if settings.notion_root_page_id:
            print("수집 방식: 페이지 하위 문서 (NOTION_ROOT_PAGE_ID)")
            docs = await collect_notion_page_tree(settings.notion_root_page_id, limit=5)
        elif settings.notion_database_id:
            print("수집 방식: 데이터베이스 (NOTION_DATABASE_ID)")
            docs = await collect_notion_documents(limit=5)
        else:
            print("[!] .env에 NOTION_ROOT_PAGE_ID 또는 NOTION_DATABASE_ID 중")
            print("    하나는 채워야 합니다.")
            return

        print(f"\n수집된 문서: {len(docs)}건\n")
        for i, doc in enumerate(docs, start=1):
            preview = doc["text"][:80].replace("\n", " ")
            print(f"[{i}] {doc['title']}")
            print(f"    출처: {doc['url']}")
            print(f"    작성: {doc['created_at']}")
            print(f"    본문: {preview}...\n")

    asyncio.run(_main())
