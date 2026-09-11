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
from datetime import date, datetime, timedelta
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


async def _fetch_page_text(client: AsyncClient, page_id: str) -> str:
    """페이지 본문 블록을 읽어 하나의 문자열로 합친다.

    하위 블록(자식)까지 재귀로 따라가지는 않는다. 1단계 깊이만 읽어도
    회의록·업무 문서 대부분의 본문은 확보된다.
    """
    lines: list[str] = []

    for block in await _list_all_blocks(client, page_id):
        block_type = block.get("type")
        if block_type not in _TEXT_BLOCK_TYPES:
            continue
        text = _extract_plain_text(block.get(block_type, {}).get("rich_text", []))
        if text.strip():
            lines.append(text)

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


async def _build_document(
    client: AsyncClient, page_id: str, title: str
) -> dict[str, Any] | None:
    """페이지 하나를 읽어 수집 결과 dict 한 건으로 만든다.

    본문이 비어 있으면 제목만으로도 검색에 쓸모가 있으므로 버리지 않는다.
    """
    body = await _fetch_page_text(client, page_id)

    await asyncio.sleep(_REQUEST_INTERVAL_SEC)
    meta = await _with_backoff(client.pages.retrieve, page_id=page_id)

    return {
        "text": f"{title}\n{body}".strip(),
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
            기본값 False다. 프로젝트 최상단 페이지에는 팀원 명단처럼
            개인정보가 섞이기 쉬워서, 명시적으로 켤 때만 넣는다.

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
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
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
PERIOD_NEXT_MONTH = "next_month"
PERIOD_CUSTOM = "custom"

PERIOD_LABELS = (
    PERIOD_TODAY,
    PERIOD_TOMORROW,
    PERIOD_THIS_WEEK,
    PERIOD_LAST_WEEK,
    PERIOD_NEXT_WEEK,
    PERIOD_THIS_MONTH,
    PERIOD_NEXT_MONTH,
    PERIOD_CUSTOM,
)

# 기간을 특정할 수 없을 때 볼 범위. "회의 가능한 날 알려줘"처럼 기간 언급이
# 없는 질문에 쓴다. 오늘부터 2주면 일정 잡기에 대체로 충분하다.
DEFAULT_LOOKAHEAD_DAYS = 13


def _month_end(day: date) -> date:
    """그 달의 마지막 날."""
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


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
    if period == PERIOD_THIS_MONTH:
        return base.replace(day=1).isoformat(), _month_end(base).isoformat()
    if period == PERIOD_NEXT_MONTH:
        first = _month_end(base) + timedelta(days=1)
        return first.isoformat(), _month_end(first).isoformat()

    # 모르는 라벨. 기간을 특정 못 한 것으로 보고 기본 범위를 쓴다.
    logger.info("알 수 없는 기간 라벨(%s). 기본 범위를 사용합니다.", period)
    return today, (base + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)).isoformat()


def is_past_range(end_date: str, today: str) -> bool:
    """조회 범위가 통째로 지난 날인지.

    "저번 주에 회의 가능한 날"처럼 이미 지난 기간을 묻는 경우가 있다.
    답은 해주되, 지난 날이라는 사실을 함께 알려야 사용자가 헷갈리지 않는다.
    """
    return bool(end_date) and bool(today) and end_date < today


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
