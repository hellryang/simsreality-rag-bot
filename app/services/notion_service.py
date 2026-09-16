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
import re
from datetime import date, datetime, timedelta
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
            # Notion API 2025-09-03부터 행 조회는 데이터 소스 단위다.
            # notion-client 3.x에는 databases.query가 없다.
            response = await _with_backoff(
                client.data_sources.query,
                data_source_id=await resolve_data_source_id(
                    client, settings.notion_database_id
                ),
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


# --- 쓰기 (봇 → 노션 일정 등록) --------------------------------------
#
# 위쪽 수집 함수들과 달리 이쪽은 **쓰기 권한 키**(notion_privatespace_api)를 쓴다.
# 키를 나눠 두면 수집용 Integration에는 읽기 권한만 주면 된다(최소 권한 원칙).
#
# 대상 캘린더 DB의 속성 이름·타입은 fill_notion_calendar.py로 61건을 실제로
# 넣어 확인한 것이다:
#   이름(title) / 날짜(date) / 프로젝트명(select) / 유형(select)
#   시간(rich_text) / 참석자(rich_text) / 장소(rich_text) / 메모 → 페이지 본문

# Notion의 rich_text 한 덩어리는 2000자까지다. 넘기면 400이 난다.
_RICH_TEXT_LIMIT = 2000
# 한 요청에 실을 수 있는 children 블록은 100개까지다.
_MAX_CHILDREN = 100


class NotionWriteError(RuntimeError):
    """노션에 쓰지 못했을 때. 사용자에게 그대로 보여줄 이유를 담는다."""


def is_iso_date(value: str) -> bool:
    """노션 date 속성이 받는 YYYY-MM-DD 형식인지 확인한다.

    노션은 ISO 8601만 받는다. "다음주 화요일" 같은 값을 그대로 넣으면 400이
    나므로, 저장을 시도하기 전에 여기서 거른다.
    """
    # strptime은 "2026-8-24"처럼 0이 빠진 값도 받아 준다. 그러면 문자열로
    # 날짜를 비교하는 곳("2026-8-24" > "2026-08-31")이 조용히 틀리고, 노션에도
    # 형식이 다른 값이 넘어간다. 파싱한 뒤 다시 찍은 모양과 같아야 통과시킨다.
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
    except (ValueError, TypeError):
        return False


def _rich_text(content: str) -> list[dict[str, Any]]:
    """문자열 하나를 rich_text 배열로 감싼다. 2000자를 넘으면 자른다."""
    return [{"type": "text", "text": {"content": content[:_RICH_TEXT_LIMIT]}}]


def _paragraph_blocks(text: str) -> list[dict[str, Any]]:
    """긴 본문을 2000자 단위 paragraph 블록으로 쪼갠다.

    통째로 한 블록에 넣으면 긴 메모에서 400이 난다. 잘라서 여러 문단으로
    넣으면 노션에서는 그냥 이어진 문단으로 보인다.
    """
    pieces = [
        text[i : i + _RICH_TEXT_LIMIT] for i in range(0, len(text), _RICH_TEXT_LIMIT)
    ]
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(piece)},
        }
        for piece in pieces[:_MAX_CHILDREN]
    ]


def _field(event: dict[str, Any], key: str) -> str:
    """일정 dict에서 값을 문자열로 꺼낸다. 값이 없으면 빈 문자열.

    None을 그대로 str()하면 "None"이라는 **글자**가 되어 노션에 그 텍스트가
    박힌다. fill_notion_calendar.py의 _val()이 막던 것과 같은 함정이다.
    Claude가 돌려준 JSON에도 null이 섞일 수 있어 여기서 한 번 거른다.
    """
    value = event.get(key)
    if value is None or value in ("", "None", "null"):
        return ""
    return str(value).strip()


def build_event_properties(event: dict[str, Any]) -> dict[str, Any]:
    """일정 dict 하나를 노션 properties로 바꾼다.

    값이 없는 속성은 **아예 넣지 않는다**. 노션은 안 보낸 속성을 빈 칸으로
    두므로, 굳이 빈 값을 채워 보낼 이유가 없다.

    Args:
        event: {"name","date","end_date","time","place","attendees",
                "type","project"} 중 있는 것만. name과 date는 필수.

    Raises:
        NotionWriteError: 이름이 비었거나 날짜 형식이 YYYY-MM-DD가 아닐 때.
    """
    name = _field(event, "name")
    if not name:
        raise NotionWriteError("일정 이름이 비어 있습니다.")

    date = _field(event, "date")
    if not is_iso_date(date):
        raise NotionWriteError(
            f"날짜를 알아볼 수 없습니다: {date or '(없음)'} "
            f"— 2026-09-15 형식이어야 합니다."
        )

    props: dict[str, Any] = {"이름": {"title": _rich_text(name)}}

    date_value: dict[str, str] = {"start": date}
    end_date = _field(event, "end_date")
    # 종료일이 이상해도 일정 전체를 버리지는 않는다. 시작일만 넣는다.
    if end_date and is_iso_date(end_date):
        date_value["end"] = end_date
    props["날짜"] = {"date": date_value}

    # select는 DB에 없는 값 이름을 주면 옵션이 자동 생성된다(에러 아님).
    # 다만 오타가 그대로 새 옵션이 되므로 길이만 잘라 둔다.
    for key, prop_name in (("project", "프로젝트명"), ("type", "유형")):
        value = _field(event, key)
        if value:
            props[prop_name] = {"select": {"name": value[:100]}}

    for key, prop_name in (("time", "시간"), ("attendees", "참석자"), ("place", "장소")):
        value = _field(event, key)
        if value:
            props[prop_name] = {"rich_text": _rich_text(value)}

    return props


async def create_calendar_event(
    event: dict[str, Any], database_id: str | None = None
) -> str:
    """일정 한 건을 노션 캘린더 DB에 새 페이지로 만든다.

    Args:
        event: build_event_properties()가 받는 형태. "memo"가 있으면 페이지
            본문으로 들어간다.
        database_id: 넣을 DB. None이면 설정의 notion_calendar_db_id.

    Returns:
        만들어진 노션 페이지 URL. 사용자에게 보내 확인·수정하게 한다.

    Raises:
        NotionWriteError: 키·DB 설정이 없거나 입력이 형식에 맞지 않을 때.
    """
    if not settings.notion_privatespace_api:
        raise NotionWriteError(
            "노션 쓰기 키가 없습니다. .env의 NOTION_PRIVATESPACE_API를 확인하세요."
        )

    db_id = (database_id or settings.notion_calendar_db_id or "").replace("-", "")
    if not db_id:
        raise NotionWriteError(
            "노션 캘린더 DB가 지정되지 않았습니다. "
            ".env의 NOTION_CALENDAR_DB_ID를 확인하세요."
        )

    # 형식 검증을 먼저 한다. 여기서 걸리면 API를 부르지도 않는다.
    properties = build_event_properties(event)

    memo = _field(event, "memo")
    children = _paragraph_blocks(memo) if memo else []

    client = AsyncClient(auth=settings.notion_privatespace_api)
    try:
        page = await _with_backoff(
            client.pages.create,
            parent={"database_id": db_id},
            properties=properties,
            children=children,
        )
    except APIResponseError as exc:
        # 403은 대상 DB가 Integration과 연결되지 않은 경우가 대부분이다.
        logger.error("노션 일정 등록 실패 (status=%s)", exc.status)
        raise NotionWriteError(
            f"노션에 등록하지 못했습니다. (status={exc.status}) "
            f"대상 DB가 Integration과 연결되어 있는지 확인하세요."
        ) from exc
    finally:
        await client.aclose()

    url = page.get("url", "")
    logger.info("노션 일정 등록: %s (%s)", event.get("name"), url)
    return url




# --- 기간 해석 -------------------------------------------------------
#
# "저번 주"가 며칠부터 며칠까지인지는 **모델에게 계산시키지 않는다.**
# 실측에서 2026-09-09(수)에 "저번 주"를 물었더니 모델이 09-01~09-07을
# 잡았다. 정답은 08-31~09-06이다. 시작도 끝도 하루씩 밀려서, 이번 주
# 월요일이 결과에 섞이고 저번 주 월요일이 빠졌다.
#
# 그래서 모델은 "저번 주"라는 말을 `last_week`이라는 **라벨로 분류만** 하고,
# 실제 날짜 계산은 여기서 한다. 분류는 모델이 잘하고, 날짜 산술은 못한다.
# 명시적 날짜("9월 15일부터 20일")는 custom으로 그대로 받는다 - 그건
# 계산이 아니라 옮겨 적기라 안전하다.

# 한 주는 월요일에 시작한다(한국 관례). date.weekday()가 월=0이라 그대로 맞다.
PERIOD_TODAY = "today"
PERIOD_TOMORROW = "tomorrow"
PERIOD_THIS_WEEK = "this_week"
PERIOD_LAST_WEEK = "last_week"
PERIOD_NEXT_WEEK = "next_week"
PERIOD_THIS_MONTH = "this_month"
PERIOD_LAST_MONTH = "last_month"
PERIOD_NEXT_MONTH = "next_month"
PERIOD_TWO_MONTHS_AGO = "two_months_ago"
PERIOD_THREE_MONTHS_AGO = "three_months_ago"
PERIOD_IN_TWO_MONTHS = "in_two_months"
PERIOD_IN_THREE_MONTHS = "in_three_months"
PERIOD_PAST_3_MONTHS = "past_3_months"
PERIOD_NEXT_3_MONTHS = "next_3_months"
PERIOD_AROUND_3_MONTHS = "around_3_months"
PERIOD_CUSTOM = "custom"

# 월 라벨을 "오늘 기준 몇 달 전/후"로 옮긴 표. 앞뒤 3개월까지 다룬다.
# 라벨을 하나씩 두는 이유: 모델은 "지지난달"을 two_months_ago로 **분류**하는
# 일만 하고, 그것이 몇 월인지는 계산하지 않는다. 숫자 오프셋을 받으면
# -2인지 -3인지 모델이 세어야 하고, 그게 틀리는 종류의 작업이다.
_MONTH_OFFSETS = {
    PERIOD_THREE_MONTHS_AGO: -3,
    PERIOD_TWO_MONTHS_AGO: -2,
    PERIOD_LAST_MONTH: -1,
    PERIOD_THIS_MONTH: 0,
    PERIOD_NEXT_MONTH: 1,
    PERIOD_IN_TWO_MONTHS: 2,
    PERIOD_IN_THREE_MONTHS: 3,
}

PERIOD_LABELS = (
    PERIOD_TODAY,
    PERIOD_TOMORROW,
    PERIOD_THIS_WEEK,
    PERIOD_LAST_WEEK,
    PERIOD_NEXT_WEEK,
    PERIOD_THIS_MONTH,
    PERIOD_LAST_MONTH,
    PERIOD_NEXT_MONTH,
    PERIOD_TWO_MONTHS_AGO,
    PERIOD_THREE_MONTHS_AGO,
    PERIOD_IN_TWO_MONTHS,
    PERIOD_IN_THREE_MONTHS,
    PERIOD_PAST_3_MONTHS,
    PERIOD_NEXT_3_MONTHS,
    PERIOD_AROUND_3_MONTHS,
    PERIOD_CUSTOM,
)


def _month_end(day: date) -> date:
    """그 달의 마지막 날."""
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def _shift_month(day: date, months: int) -> date:
    """그 달의 1일로 옮긴 뒤 months만큼 이동한다.

    month +- n 으로 계산하면 1월에서 0월이 되거나 12월에서 13월이 되어
    터진다. 연·월을 통째로 개월 수로 환산해 더하면 경계가 저절로 맞는다.
    """
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def resolve_period(
    period: str,
    today: str,
    start_date: str = "",
    end_date: str = "",
) -> tuple[str, str]:
    """기간 라벨을 실제 날짜 범위로 바꾼다. 양끝을 포함한다.

    Args:
        period: PERIOD_LABELS 중 하나. 모르는 값이면 기본 범위를 쓴다.
        today: 오늘 날짜(YYYY-MM-DD).
        start_date, end_date: period가 custom일 때만 쓴다.

    Returns:
        (시작일, 종료일) 둘 다 YYYY-MM-DD.

    Raises:
        NotionWriteError: today가 형식에 맞지 않거나, custom인데 날짜가
            올바르지 않을 때.
    """
    if not is_iso_date(today):
        raise NotionWriteError(f"오늘 날짜가 올바르지 않습니다: {today}")

    base = datetime.strptime(today, "%Y-%m-%d").date()
    monday = base - timedelta(days=base.weekday())  # 이번 주 월요일

    if period == PERIOD_CUSTOM:
        if not is_iso_date(start_date) or not is_iso_date(end_date):
            raise NotionWriteError(
                f"조회 기간이 올바르지 않습니다: {start_date or '(없음)'} ~ "
                f"{end_date or '(없음)'}"
            )
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date

    if period == PERIOD_TODAY:
        return today, today
    if period == PERIOD_TOMORROW:
        tomorrow = (base + timedelta(days=1)).isoformat()
        return tomorrow, tomorrow
    if period == PERIOD_THIS_WEEK:
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if period == PERIOD_LAST_WEEK:
        last_monday = monday - timedelta(days=7)
        return last_monday.isoformat(), (last_monday + timedelta(days=6)).isoformat()
    if period == PERIOD_NEXT_WEEK:
        next_monday = monday + timedelta(days=7)
        return next_monday.isoformat(), (next_monday + timedelta(days=6)).isoformat()
    if period in _MONTH_OFFSETS:
        first = _shift_month(base, _MONTH_OFFSETS[period])
        return first.isoformat(), _month_end(first).isoformat()

    # 여러 달을 한 번에 훑는 범위. 기간이 모호한 질문("이전에", "언제였지")
    # 에 쓴다. 한 달씩 끊어진 라벨로는 이런 질문을 시작할 수 없다.
    if period == PERIOD_PAST_3_MONTHS:
        return _shift_month(base, -3).isoformat(), today
    if period == PERIOD_NEXT_3_MONTHS:
        return today, _month_end(_shift_month(base, 3)).isoformat()
    if period == PERIOD_AROUND_3_MONTHS:
        return (
            _shift_month(base, -3).isoformat(),
            _month_end(_shift_month(base, 3)).isoformat(),
        )

    # 모르는 라벨. 기간을 특정 못 한 것으로 보고 넓게 훑는다.
    # 좁게 잡으면(예: 오늘~2주) 과거 질문을 놓치고 '없다'고 단정하게 된다.
    logger.info("알 수 없는 기간 라벨(%s). 앞뒤 3개월을 조회합니다.", period)
    return (
        _shift_month(base, -3).isoformat(),
        _month_end(_shift_month(base, 3)).isoformat(),
    )


def is_past_range(end_date: str, today: str) -> bool:
    """조회 범위가 통째로 지난 날인지.

    "저번 주에 회의 가능한 날"처럼 이미 지난 기간을 묻는 경우가 있다.
    답은 해주되, 지난 날이라는 사실을 함께 알려야 사용자가 헷갈리지 않는다.
    """
    return bool(end_date) and bool(today) and end_date < today


# --- 예약 날짜 계산 --------------------------------------------------
#
# 예약하기도 질문하기와 같은 원칙이다. 모델은 날짜 표현을 분류하고 숫자·요일을
# 옮겨 적기만 하고, 실제 날짜는 여기서 계산한다. 모델이 YYYY-MM-DD를 직접
# 만들면 형식은 맞는데 날짜가 틀린 값이 노션에 그대로 남는다.

# 노션이 응답하지 않을 때 쓰는 기본 선택지(2026-09-15 조회).
# 평소에는 calendar_choices()가 노션에서 읽어 온 목록을 쓴다.
# 없는 이름을 보내면 노션이 새 선택지를 만들어 버리므로 반드시 대조한다.
CALENDAR_TYPES = (
    "회의", "정기회의", "외부미팅", "보고", "리뷰", "시연", "시험", "현장조사",
    "출장", "교육", "행사", "공지", "마일스톤", "휴가", "휴일",
)
CALENDAR_PROJECTS = (
    "스마트물류센터 디지털트윈 플랫폼 구축",
    "정밀유도무기 디지털트윈 시뮬레이션 개발",
    "(사내 공통/일반)",
)
PROP_TYPE = "유형"
PROP_PROJECT = "프로젝트명"
_FALLBACK_CHOICES = {PROP_TYPE: CALENDAR_TYPES, PROP_PROJECT: CALENDAR_PROJECTS}

# 읽어 둔 선택지. 예약 모달을 만들 때 force=True로 새로 읽고, 같은 예약의
# 제출 처리가 이 값을 재사용한다(예약 1건당 노션 호출 1번).
# 시간 제한은 두지 않는다. 모달을 열 때마다 새로 읽으므로 낡을 틈이 없고,
# 쓰이지 않는 만료 시간을 남겨 두면 코드를 읽는 사람만 헷갈린다.
_choices_cache: dict[str, tuple[str, ...]] = {}


def clear_choices_cache() -> None:
    """다음 조회 때 노션에서 다시 읽게 한다."""
    _choices_cache.clear()


async def _read_choices() -> dict[str, tuple[str, ...]]:
    """캘린더 DB에서 select 속성의 선택지를 읽는다."""
    key = settings.notion_privatespace_api or settings.notion_api_key
    db_id = (settings.notion_calendar_db_id or "").replace("-", "")
    if not key or not db_id:
        raise NotionWriteError("노션 키 또는 캘린더 DB가 설정되지 않았습니다.")

    client = AsyncClient(auth=key)
    try:
        data_source_id = await resolve_data_source_id(client, db_id)
        info = await _with_backoff(
            client.data_sources.retrieve, data_source_id=data_source_id
        )
    finally:
        await client.aclose()

    properties = info.get("properties") or {}
    found: dict[str, tuple[str, ...]] = {}
    for prop_name in (PROP_TYPE, PROP_PROJECT):
        options = ((properties.get(prop_name) or {}).get("select") or {}).get("options", [])
        names = tuple(o["name"] for o in options if o.get("name"))
        if names:
            found[prop_name] = names
    return found


async def calendar_choices(force: bool = False) -> dict[str, tuple[str, ...]]:
    """유형·프로젝트명 선택지. {"유형": (...), "프로젝트명": (...)}

    Args:
        force: True면 읽어 둔 값이 있어도 노션에서 다시 읽는다. 예약 모달을
            만들 때 이 값으로 부른다(선택 상자가 항상 최신이어야 하므로).

    노션을 읽지 못하면 직전에 읽어 둔 목록을, 그것도 없으면 코드의 기본값을
    돌려준다. 선택지를 못 읽었다고 예약을 실패시키지 않는다.
    """
    if _choices_cache and not force:
        return dict(_choices_cache)

    try:
        found = await _read_choices()
    except Exception:
        logger.warning("선택지를 읽지 못했습니다. 이전 목록을 씁니다.", exc_info=True)
        return dict(_choices_cache) or dict(_FALLBACK_CHOICES)

    _choices_cache.clear()
    _choices_cache.update({**_FALLBACK_CHOICES, **found})
    logger.info(
        "선택지 갱신: 유형 %d개, 프로젝트명 %d개",
        len(_choices_cache[PROP_TYPE]), len(_choices_cache[PROP_PROJECT]),
    )
    return dict(_choices_cache)

DATE_EXACT = "exact"
DATE_DAYS_LATER = "days_later"
_DAY_OFFSETS = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
_WEEK_OFFSETS = {"this_week": 0, "next_week": 7, "week_after_next": 14}
DATE_TYPES = (DATE_EXACT, *_DAY_OFFSETS, DATE_DAYS_LATER, *_WEEK_OFFSETS)


def _to_int(value: Any) -> int | None:
    """모델이 숫자를 "25"처럼 문자열로 줄 때도 받는다. 못 읽으면 None."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _weekday_index(value: Any) -> int | None:
    """"화", "화요일" → 1. 못 읽으면 None."""
    text = str(value or "").strip()
    if not text or text[0] not in _WEEKDAYS:
        return None
    return _WEEKDAYS.index(text[0])


def weekday_label(iso_date: str) -> str:
    """"2026-09-22" → "화". 형식이 틀리면 빈 문자열."""
    if not is_iso_date(iso_date):
        return ""
    return _WEEKDAYS[datetime.strptime(iso_date, "%Y-%m-%d").date().weekday()]


def _month_day(year: Any, month: Any, day: Any, not_before: date) -> date:
    """월·일(과 연도)을 날짜로. 연도가 없으면 not_before 이후로 가장 가까운 해.

    9월에 "1월 5일"을 예약하면 올해 1월은 이미 지났으므로 내년 1월 5일이다.
    """
    m, d, y = _to_int(month), _to_int(day), _to_int(year)
    if m is None or d is None:
        raise NotionWriteError("날짜(월·일)를 알아볼 수 없습니다.")
    try:
        if y is not None:
            return date(y, m, d)
        candidate = date(not_before.year, m, d)
        if candidate < not_before:
            candidate = date(not_before.year + 1, m, d)
        return candidate
    except ValueError:
        raise NotionWriteError(f"없는 날짜입니다: {m}월 {d}일") from None


def resolve_event_date(spec: dict[str, Any], today: str) -> tuple[str, str]:
    """모델이 분류한 날짜 표현을 (시작일, 종료일)로 계산한다.

    Args:
        spec: date_type과 그에 딸린 값.
            exact              month, day, (year)   "9월 25일"
            today/tomorrow/day_after_tomorrow        "내일", "모레"
            days_later         days                 "3일 뒤"
            this_week/next_week/week_after_next  weekday  "다음 주 화요일"
            종료일(선택)       end_month, end_day, (end_year) 또는 end_weekday
        today: 오늘 날짜(YYYY-MM-DD).

    Returns:
        ("2026-09-22", "") 종료일이 없거나 시작일보다 앞서면 빈 문자열.

    Raises:
        NotionWriteError: 날짜를 계산할 수 없을 때. 사용자에게 그대로 보인다.
    """
    if not is_iso_date(today):
        raise NotionWriteError(f"오늘 날짜가 올바르지 않습니다: {today}")
    base = datetime.strptime(today, "%Y-%m-%d").date()
    monday = base - timedelta(days=base.weekday())
    kind = str(spec.get("date_type") or "").strip()

    if kind == DATE_EXACT:
        start = _month_day(spec.get("year"), spec.get("month"), spec.get("day"), base)
    elif kind in _DAY_OFFSETS:
        start = base + timedelta(days=_DAY_OFFSETS[kind])
    elif kind == DATE_DAYS_LATER:
        days = _to_int(spec.get("days"))
        if days is None or not 0 <= days <= 366:
            raise NotionWriteError("며칠 뒤인지 알아볼 수 없습니다.")
        start = base + timedelta(days=days)
    elif kind in _WEEK_OFFSETS:
        index = _weekday_index(spec.get("weekday"))
        if index is None:
            raise NotionWriteError("요일을 알아볼 수 없습니다.")
        start = monday + timedelta(days=_WEEK_OFFSETS[kind] + index)
    else:
        raise NotionWriteError(f"날짜 표현을 알아볼 수 없습니다: {kind or '(없음)'}")

    end: date | None = None
    if _to_int(spec.get("end_day")) is not None:
        # "25일부터 27일까지"처럼 달을 생략하면 시작일의 달이다.
        end_month = spec.get("end_month") or start.month
        end = _month_day(spec.get("end_year"), end_month, spec.get("end_day"), start)
    elif kind in _WEEK_OFFSETS and _weekday_index(spec.get("end_weekday")) is not None:
        # "다음 주 월요일부터 수요일까지" - 같은 주의 요일
        end = monday + timedelta(
            days=_WEEK_OFFSETS[kind] + _weekday_index(spec.get("end_weekday"))
        )

    if end is not None and end <= start:
        end = None
    return start.isoformat(), end.isoformat() if end else ""


def prepare_reserved_event(
    event: dict[str, Any],
    today: str,
    choices: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """모델이 뽑은 일정 한 건을 노션에 넣을 수 있는 모양으로 만든다.

    date_type이 있으면 날짜를 계산해 date/end_date를 채운다. 유형·프로젝트명이
    선택지에 없으면 비운다(노션은 없는 이름을 받으면 새 선택지를 만든다).
    원본 dict는 바꾸지 않는다.

    Args:
        choices: calendar_choices() 결과. 주지 않으면 코드의 기본값을 쓴다.
    """
    prepared = dict(event)
    if prepared.get("date_type"):
        prepared["date"], prepared["end_date"] = resolve_event_date(prepared, today)

    allowed = choices or _FALLBACK_CHOICES
    for key, prop_name in (("type", PROP_TYPE), ("project", PROP_PROJECT)):
        value = _field(prepared, key)
        if value and value not in allowed.get(prop_name, ()):
            logger.info("%s '%s'은 캘린더 선택지에 없어 비웁니다.", prop_name, value)
            prepared[key] = ""
    return prepared


# --- 데이터 소스 해석 ------------------------------------------------
#
# Notion API가 2025-09-03 버전에서 **데이터 소스(data source)** 개념을 도입했다.
# 데이터베이스 하나가 여러 데이터 소스를 가질 수 있게 되면서, 행 조회가
# `databases.query`에서 `data_sources.query`로 옮겨졌다.
# notion-client 3.x에는 `databases.query`가 아예 없다(AttributeError).
#
# 그래서 DB id로 먼저 데이터 소스 id를 얻어야 한다. 이 매핑은 바뀌지 않으므로
# 프로세스 안에서 한 번만 조회하고 캐시한다(질문마다 호출이 하나 늘면
# Rate Limit에 그만큼 가까워진다).
_DATA_SOURCE_IDS: dict[str, str] = {}


async def resolve_data_source_id(client: AsyncClient, database_id: str) -> str:
    """DB id로 그 안의 데이터 소스 id를 얻는다.

    Raises:
        NotionWriteError: 데이터 소스를 찾지 못했을 때(권한 없음 포함).
    """
    cached = _DATA_SOURCE_IDS.get(database_id)
    if cached:
        return cached

    database = await _with_backoff(client.databases.retrieve, database_id=database_id)
    sources = database.get("data_sources") or []
    if not sources:
        raise NotionWriteError(
            "이 데이터베이스에서 데이터 소스를 찾지 못했습니다. "
            "Integration이 대상 DB와 연결되어 있는지 확인하세요."
        )
    if len(sources) > 1:
        # 우리 캘린더는 하나뿐이다. 여러 개가 되면 어느 것을 쓸지 정해야 한다.
        logger.warning(
            "데이터 소스가 %d개입니다. 첫 번째를 사용합니다: %s",
            len(sources), [x.get("name") for x in sources],
        )

    _DATA_SOURCE_IDS[database_id] = sources[0]["id"]
    return _DATA_SOURCE_IDS[database_id]


# --- 캘린더 조회 (빈 날 찾기) ----------------------------------------
#
# 왜 벡터 DB가 아니라 노션을 직접 읽는가:
#
# 벡터 검색은 "있는 것 중 비슷한 것"을 top-k개 돌려주는 도구다. 그런데
# "빈 날"은 `범위의 모든 날 − 일정이 있는 날`이라는 **차집합**이라, 한쪽
# 집합이 전부 있어야 계산된다. top-k만 받아서는 나머지 날에 일정이 있는지
# 없는지 알 수 없고, 모르는 채로 답하면 환각이 된다.
#
# 그래서 캘린더는 날짜 필터로 **전부** 읽어 온다. 그리고 빈 날 계산은
# 파이썬이 한다. 날짜 산술을 모델에게 맡기면 틀린다.

_WEEKDAYS = "월화수목금토일"


def _prop(page: dict[str, Any], name: str) -> dict[str, Any]:
    return page.get("properties", {}).get(name, {}) or {}


def _prop_rich_text(page: dict[str, Any], name: str) -> str:
    return _extract_plain_text(_prop(page, name).get("rich_text", []))


def _prop_select(page: dict[str, Any], name: str) -> str:
    select = _prop(page, name).get("select") or {}
    return select.get("name", "")


def parse_calendar_page(page: dict[str, Any]) -> dict[str, Any]:
    """캘린더 DB의 페이지 한 건을 일정 dict로 바꾼다.

    속성 이름은 fill_notion_calendar.py로 61건을 넣어 확인한 것과 같다.
    제목만 type으로 찾는다(DB마다 이름이 달라질 수 있어서).
    """
    date = _prop(page, "날짜").get("date") or {}
    return {
        "name": _extract_title(page),
        "date": date.get("start", "") or "",
        "end_date": date.get("end", "") or "",
        "time": _prop_rich_text(page, "시간"),
        "place": _prop_rich_text(page, "장소"),
        "attendees": _prop_rich_text(page, "참석자"),
        "type": _prop_select(page, "유형"),
        "project": _prop_select(page, "프로젝트명"),
        "url": page.get("url", ""),
    }


async def query_calendar_events(
    start_date: str,
    end_date: str,
    database_id: str | None = None,
) -> list[dict[str, Any]]:
    """캘린더 DB에서 기간에 걸치는 일정을 **전부** 가져온다.

    Args:
        start_date, end_date: YYYY-MM-DD. 양끝을 포함한다.
        database_id: None이면 설정의 notion_calendar_db_id.

    Returns:
        parse_calendar_page() 형태의 일정 목록. 날짜 오름차순.

    Raises:
        NotionWriteError: 키·DB 설정이 없거나 날짜 형식이 틀릴 때.

    페이지네이션을 끝까지 따라간다. 한 페이지(100건)만 읽고 멈추면 뒤쪽
    일정이 통째로 빠지고, 그러면 "빈 날" 계산이 조용히 틀린다.
    """
    if not is_iso_date(start_date) or not is_iso_date(end_date):
        raise NotionWriteError(
            f"조회 기간이 올바르지 않습니다: {start_date} ~ {end_date}"
        )

    key = settings.notion_privatespace_api or settings.notion_api_key
    if not key:
        raise NotionWriteError("노션 API 키가 없습니다.")

    db_id = (database_id or settings.notion_calendar_db_id or "").replace("-", "")
    if not db_id:
        raise NotionWriteError(
            "노션 캘린더 DB가 지정되지 않았습니다. NOTION_CALENDAR_DB_ID를 확인하세요."
        )

    # 시작일이 기간 안에 있는 일정을 가져온다. 여러 날에 걸친 일정은
    # 시작일이 기간보다 앞설 수 있으므로 앞으로 조금 여유를 두고 읽은 뒤
    # compute_free_days()에서 실제로 겹치는지 다시 따진다.
    query_filter = {
        "and": [
            {"property": "날짜", "date": {"on_or_before": end_date}},
            {"property": "날짜", "date": {"is_not_empty": True}},
        ]
    }

    client = AsyncClient(auth=key)
    events: list[dict[str, Any]] = []
    cursor: str | None = None

    try:
        data_source_id = await resolve_data_source_id(client, db_id)

        while True:
            await asyncio.sleep(_REQUEST_INTERVAL_SEC)
            response = await _with_backoff(
                client.data_sources.query,
                data_source_id=data_source_id,
                filter=query_filter,
                page_size=100,
                **({"start_cursor": cursor} if cursor else {}),
            )
            for page in response.get("results", []):
                event = parse_calendar_page(page)
                # 기간에 실제로 걸치는 것만 남긴다.
                last = event["end_date"] or event["date"]
                if event["date"] and last >= start_date:
                    events.append(event)

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
    except APIResponseError as exc:
        logger.error("캘린더 조회 실패 (status=%s)", exc.status)
        raise NotionWriteError(
            f"캘린더를 읽지 못했습니다. (status={exc.status}) "
            f"대상 DB가 Integration과 연결되어 있는지 확인하세요."
        ) from exc
    finally:
        await client.aclose()

    events.sort(key=lambda e: (e["date"], e.get("time", "")))
    logger.info("캘린더 %s~%s 일정 %d건 조회", start_date, end_date, len(events))
    return events


def filter_events(
    events: list[dict[str, Any]], keyword: str
) -> list[dict[str, Any]]:
    """이름·장소·참석자·프로젝트·유형 어디에든 keyword가 든 일정만 남긴다.

    긴 목록에서 한 줄을 찾는 일은 코드가 정확하다. 모델에게 스캔을 맡겼을 때
    62건 목록에서 찾던 항목을 놓치고 "없다"고 답한 사례가 있었다. 실제로는
    그 목록 안에 있었다.

    대소문자를 무시하고 부분 일치로 본다. 사용자가 "이후경"이라고만 말해도
    "김민준,이후경"에서 걸린다.
    """
    if not keyword:
        return events

    # 모델이 "이후경 대구"처럼 여러 낱말을 한 번에 넘긴다. 전체 문자열을
    # 그대로 찾으면 0건이 나오므로, 낱말별로 나눠 **전부 포함**(AND)하는
    # 일정만 남긴다. "이후경"과 "대구"가 각각 참석자·장소에 있어도 걸린다.
    needles = [word for word in keyword.lower().split() if word]
    if not needles:
        return events

    matched: list[dict[str, Any]] = []
    for event in events:
        haystack = " ".join(
            str(event.get(field) or "")
            for field in ("name", "place", "attendees", "project", "type")
        ).lower()
        if all(word in haystack for word in needles):
            matched.append(event)

    logger.info(
        "키워드 %r(낱말 %d개)로 %d건 → %d건",
        keyword, len(needles), len(events), len(matched),
    )
    return matched


def _date_range(start_date: str, end_date: str) -> list[str]:
    """양끝을 포함한 날짜 문자열 목록."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def compute_free_days(
    start_date: str, end_date: str, events: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """일정이 하나도 없는 날을 골라낸다.

    여러 날에 걸친 일정(end_date가 있는 것)은 그 사이의 모든 날을 채운
    것으로 본다. 그렇게 하지 않으면 워크숍 3일 중 가운데 날이 비어 있는
    것으로 잘못 나온다.

    Returns:
        [{"date": "2026-09-17", "weekday": "목"}, ...]

    이 계산을 모델에게 맡기지 않는 이유는 날짜 산술이 LLM이 가장 자주
    틀리는 종류의 작업이기 때문이다. 여기서 확정해 값으로 넘긴다.
    """
    busy: set[str] = set()
    for event in events:
        first = event.get("date", "")
        if not first:
            continue
        last = event.get("end_date") or first
        if last < first:
            last = first
        try:
            busy.update(_date_range(first, last))
        except ValueError:
            # 날짜 형식이 깨진 행은 건너뛴다. 노션에서 손으로 고칠 수 있다.
            logger.warning("캘린더에 형식이 이상한 날짜: %s", first)
            continue

    free: list[dict[str, str]] = []
    for day in _date_range(start_date, end_date):
        if day in busy:
            continue
        weekday = _WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").date().weekday()]
        free.append({"date": day, "weekday": weekday})
    return free


# --- 시간대 단위 가능 여부 -------------------------------------------
#
# 빈 날(compute_free_days)은 일정이 하나라도 있으면 그 날을 뺀다. 그러면
# 10시 회의 하나 있는 날도 "회의 불가"처럼 보인다. 사용자가 원한 답은
# "9/1은 10~11시 제외 가능"이다. 그래서 날마다 잡힌 시간을 모아 돌려준다.

# 노션 '시간' 칸에서 확인한 형식: "09:30-10:00", "15:00~18:00", "15:00", "18:30-"
_TIME_RANGE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(?:[-~–]\s*(?:(\d{1,2}):(\d{2}))?\s*)?$"
)


def parse_time_range(text: str) -> tuple[str, str] | None:
    """'시간' 칸을 (시작, 끝)으로 읽는다. 끝을 모르면 끝은 빈 문자열.

    Returns:
        ("09:30", "10:00") / ("15:00", "") / 읽을 수 없거나 비었으면 None.
    """
    match = _TIME_RANGE.match(text or "")
    if not match:
        return None
    start_h, start_m, end_h, end_m = match.groups()
    if int(start_h) > 23 or int(start_m) > 59:
        return None
    start = f"{int(start_h):02d}:{start_m}"

    end = ""
    if end_h is not None:
        if int(end_h) > 24 or int(end_m) > 59:
            return None
        end = f"{int(end_h):02d}:{end_m}"
        # 끝이 시작보다 앞서면 오타다. 끝을 모르는 것으로 본다.
        if end <= start:
            end = ""
    return start, end


def _merge_slots(slots: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """겹치거나 맞닿은 시간대를 합친다. 끝을 모르는 시간대는 합치지 않는다.

    09:00-17:00 출장과 10:00-11:00 회의가 같은 날이면 09:00~17:00 하나다.
    "HH:MM"은 0으로 채워져 있어 문자열 비교가 곧 시간 비교다.
    """
    closed = sorted((s, e) for s, e in slots if e)
    merged: list[tuple[str, str]] = []
    for start, end in closed:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    open_ended = [(start, "") for start in sorted({s for s, e in slots if not e})]
    return sorted(merged + open_ended)


def compute_day_availability(
    start_date: str, end_date: str, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """기간의 날마다 가능 여부를 계산한다.

    Returns:
        날짜 순 목록. 각 항목:
          date, weekday
          status  "free"    일정 없음 - 전일 가능
                  "partial" 시간이 적힌 일정만 있음 - 그 시간 빼면 가능
                  "unknown" 시간이 안 적힌 일정이 있음 - 단정할 수 없음
          busy    합친 시간대 [("10:00", "11:00"), ("15:00", "")]
          names   시간이 적힌 일정 이름
          untimed 시간이 안 적힌 일정 이름

    여러 날에 걸친 일정은 걸친 날마다 같은 시간(또는 시간 미기재)으로 본다.
    """
    per_day: dict[str, dict[str, list]] = {
        day: {"slots": [], "names": [], "untimed": []}
        for day in _date_range(start_date, end_date)
    }

    for event in events:
        first = (event.get("date") or "")[:10]
        if not first:
            continue
        last = (event.get("end_date") or "")[:10] or first
        if last < first:
            last = first
        try:
            span = _date_range(first, last)
        except ValueError:
            logger.warning("캘린더에 형식이 이상한 날짜: %s", first)
            continue

        name = event.get("name") or "(이름 없음)"
        slot = parse_time_range(event.get("time") or "")
        for day in span:
            info = per_day.get(day)
            if info is None:
                continue
            if slot is None:
                info["untimed"].append(name)
            else:
                info["slots"].append(slot)
                if name not in info["names"]:
                    info["names"].append(name)

    result: list[dict[str, Any]] = []
    for day, info in per_day.items():
        if info["untimed"]:
            status = "unknown"
        elif info["slots"]:
            status = "partial"
        else:
            status = "free"
        result.append(
            {
                "date": day,
                "weekday": _WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").date().weekday()],
                "status": status,
                "busy": _merge_slots(info["slots"]),
                "names": info["names"],
                "untimed": info["untimed"],
            }
        )
    return result


# --- 예약 겹침 확인 --------------------------------------------------
#
# 예약할 때 같은 시간에 같은 사람이 이미 다른 일정에 들어가 있으면 알린다.
# 등록은 막지 않는다(사용자 결정). 사람이 겹치지 않으면 같은 시간이어도
# 알리지 않는다 - 회의실이 아니라 사람의 일정이 겹치는 게 문제이기 때문이다.

# 끝 시간이 없는 일정("14:00", "18:30-")은 이만큼 이어진다고 본다.
DEFAULT_SLOT_MINUTES = 60

# 이름 뒤에 붙는 직함. 참석자 비교에서 이름으로 치지 않는다.
# "홍길동 상무"와 "홍길동(태원전장)"이 같은 사람으로 비교되게 한다.
_TITLES = {
    "님", "대표", "대표님", "상무", "상무님", "전무", "이사", "사장", "부사장",
    "부장", "차장", "과장", "대리", "주임", "사원", "팀장", "실장", "본부장",
    "센터장", "매니저", "책임", "선임", "수석", "연구원", "교수", "박사",
}


def attendee_names(text: str) -> set[str]:
    """참석자 칸을 이름 집합으로 바꾼다.

    "김민준,이후경" / "김민준, 이후경" / "김민준 이후경" / "홍길동(태원전장)" /
    "김명환 대표님" 을 모두 받는다. 괄호 안(소속)과 직함은 버린다.
    """
    cleaned = re.sub(r"\([^)]*\)", " ", text or "")
    names: set[str] = set()
    for token in re.split(r"[,，、·/\s]+", cleaned):
        token = token.strip()
        if token.endswith("님") and len(token) > 2:
            token = token[:-1]
        if len(token) < 2 or token in _TITLES:
            continue
        names.add(token)
    return names


def _slot_minutes(time_text: str) -> tuple[int, int] | None:
    """'시간' 칸을 (시작 분, 끝 분)으로. 시간이 없거나 못 읽으면 None."""
    slot = parse_time_range(time_text or "")
    if slot is None:
        return None
    start_h, start_m = slot[0].split(":")
    start = int(start_h) * 60 + int(start_m)
    if slot[1]:
        end_h, end_m = slot[1].split(":")
        end = int(end_h) * 60 + int(end_m)
    else:
        end = start + DEFAULT_SLOT_MINUTES
    return start, end


def find_conflicts(
    event: dict[str, Any], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """새 일정과 참석자·시간이 겹치는 기존 일정을 찾는다.

    Args:
        event: 등록할 일정. date(필수), end_date, time, attendees.
        existing: 같은 기간의 기존 일정(parse_calendar_page 형태).

    Returns:
        [{"event": 기존 일정, "shared": ["김민준"], "kind": "time" | "untimed"}, ...]
        kind가 "untimed"면 둘 중 하나에 시간이 없어 같은 날이라는 것만 안다.

    겹침 조건: 날짜 범위가 겹치고, 참석자가 한 명 이상 같고,
    시간이 겹친다(한쪽이라도 시간이 없으면 같은 날이면 겹침으로 본다).
    끝과 시작이 맞닿기만 한 것(14:00-15:00 뒤의 15:00)은 겹침이 아니다.
    """
    names = attendee_names(event.get("attendees", ""))
    start = (event.get("date") or "")[:10]
    if not names or not start:
        return []
    end = (event.get("end_date") or "")[:10] or start
    new_slot = _slot_minutes(event.get("time", ""))

    conflicts: list[dict[str, Any]] = []
    for other in existing:
        other_start = (other.get("date") or "")[:10]
        if not other_start:
            continue
        other_end = (other.get("end_date") or "")[:10] or other_start
        if other_end < start or other_start > end:
            continue

        shared = sorted(names & attendee_names(other.get("attendees", "")))
        if not shared:
            continue

        other_slot = _slot_minutes(other.get("time", ""))
        if new_slot and other_slot:
            if not (new_slot[0] < other_slot[1] and other_slot[0] < new_slot[1]):
                continue
            kind = "time"
        else:
            kind = "untimed"
        conflicts.append({"event": other, "shared": shared, "kind": kind})
    return conflicts


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
