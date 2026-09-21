"""Notion 쓰기(봇 → 노션 일정 등록) 테스트 — 담당: 마준서

수집 쪽 테스트(test_notion_service.py)와 파일을 나눈 이유는 담당자가 달라서다.
같은 파일을 양쪽에서 고치면 병합할 때 충돌한다.

여기서도 실제 Notion API는 부르지 않는다. 우리가 만드는 **요청 본문이
노션 스펙에 맞는지**와, **잘못된 입력을 API 호출 전에 걸러내는지**만 본다.
그래야 키 없이도, 인터넷 없이도 돌아간다.
"""
import pytest

from app.core.config import settings
from app.services.notion_service import (
    NotionWriteError,
    _paragraph_blocks,
    _RICH_TEXT_LIMIT,
    build_event_properties,
    create_calendar_event,
    is_iso_date,
)


# --- 날짜 형식 -------------------------------------------------------


@pytest.mark.parametrize("value", ["2026-09-15", "2026-01-01", "2026-12-31"])
def test_iso_dates_are_accepted(value):
    assert is_iso_date(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "다음주 화요일",  # 모델이 형식을 안 지킨 경우
        "2026/09/15",  # 구분자가 다름
        "2026-09-15 00:00:00",  # 엑셀 datetime을 str()한 형태
        "2026-13-01",  # 13월은 없다
        "2026-8-24",  # 0이 빠짐 - strptime은 통과시킨다
        "2026-08-5",
        "",
    ],
)
def test_other_date_shapes_are_rejected(value):
    """노션 date 속성은 YYYY-MM-DD만 받는다.

    여기서 걸러내지 않으면 노션이 400을 돌려주고, 사용자는 이유를 모른 채
    "등록 실패"만 보게 된다.
    """
    assert is_iso_date(value) is False


# --- properties 조립 -------------------------------------------------


def test_a_minimal_event_becomes_title_and_date():
    props = build_event_properties({"name": "킥오프 회의", "date": "2026-09-15"})

    assert props["이름"]["title"][0]["text"]["content"] == "킥오프 회의"
    assert props["날짜"]["date"] == {"start": "2026-09-15"}


def test_empty_fields_are_left_out_entirely():
    """값이 없는 속성은 아예 보내지 않는다.

    노션은 안 보낸 속성을 빈 칸으로 둔다. 빈 문자열을 굳이 채워 보내면
    select에 이름 없는 옵션이 생기는 등 부작용만 난다.
    """
    props = build_event_properties(
        {"name": "회의", "date": "2026-09-15", "place": "", "time": None}
    )

    assert "장소" not in props
    assert "시간" not in props


def test_all_optional_fields_map_to_their_notion_properties():
    """엑셀 적재(fill_notion_calendar.py)로 검증한 속성 이름과 같아야 한다."""
    props = build_event_properties(
        {
            "name": "킥오프 회의",
            "date": "2026-09-15",
            "time": "15:00",
            "place": "회의실A",
            "attendees": "김민준,이서연",
            "type": "회의",
            "project": "디지털트윈",
        }
    )

    assert props["시간"]["rich_text"][0]["text"]["content"] == "15:00"
    assert props["장소"]["rich_text"][0]["text"]["content"] == "회의실A"
    assert props["참석자"]["rich_text"][0]["text"]["content"] == "김민준,이서연"
    assert props["유형"]["select"]["name"] == "회의"
    assert props["프로젝트명"]["select"]["name"] == "디지털트윈"


def test_an_end_date_becomes_a_date_range():
    props = build_event_properties(
        {"name": "워크숍", "date": "2026-09-15", "end_date": "2026-09-17"}
    )

    assert props["날짜"]["date"] == {"start": "2026-09-15", "end": "2026-09-17"}


def test_a_malformed_end_date_is_dropped_but_the_event_survives():
    """종료일이 이상하다고 일정 전체를 버리지는 않는다. 시작일만 넣는다."""
    props = build_event_properties(
        {"name": "워크숍", "date": "2026-09-15", "end_date": "언젠가"}
    )

    assert props["날짜"]["date"] == {"start": "2026-09-15"}


def test_an_event_without_a_name_is_refused():
    with pytest.raises(NotionWriteError, match="이름"):
        build_event_properties({"name": "  ", "date": "2026-09-15"})


def test_an_event_with_an_unparseable_date_is_refused():
    """모델이 형식을 안 지킨 경우다. API를 부르기 전에 여기서 멈춘다."""
    with pytest.raises(NotionWriteError, match="날짜"):
        build_event_properties({"name": "회의", "date": "다음주 화요일"})


# --- 본문 분할 -------------------------------------------------------


def test_a_short_memo_becomes_one_paragraph():
    blocks = _paragraph_blocks("짧은 메모")

    assert len(blocks) == 1
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "짧은 메모"


def test_a_long_memo_is_split_so_notion_does_not_reject_it():
    """rich_text 한 덩어리는 2000자까지다. 넘기면 400이 난다.

    대화록은 쉽게 2000자를 넘으므로 반드시 쪼개야 한다.
    """
    blocks = _paragraph_blocks("가" * (_RICH_TEXT_LIMIT * 2 + 10))

    assert len(blocks) == 3
    for block in blocks:
        content = block["paragraph"]["rich_text"][0]["text"]["content"]
        assert len(content) <= _RICH_TEXT_LIMIT


# --- 설정이 빠졌을 때 -------------------------------------------------


async def test_registering_without_a_write_key_explains_why(monkeypatch):
    monkeypatch.setattr(settings, "notion_privatespace_api", None)

    with pytest.raises(NotionWriteError, match="NOTION_PRIVATESPACE_API"):
        await create_calendar_event({"name": "회의", "date": "2026-09-15"})


async def test_registering_without_a_target_database_explains_why(monkeypatch):
    monkeypatch.setattr(settings, "notion_privatespace_api", "ntn_test")
    monkeypatch.setattr(settings, "notion_calendar_db_id", None)

    with pytest.raises(NotionWriteError, match="NOTION_CALENDAR_DB_ID"):
        await create_calendar_event({"name": "회의", "date": "2026-09-15"})


async def test_a_bad_date_is_caught_before_any_api_call(monkeypatch):
    """검증이 API 호출보다 먼저 일어나는지 본다.

    순서가 뒤바뀌면 노션에 쓸모없는 요청을 보내고 429 한도만 축낸다.
    """
    monkeypatch.setattr(settings, "notion_privatespace_api", "ntn_test")
    monkeypatch.setattr(settings, "notion_calendar_db_id", "db-1")

    called = False

    def explode(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("API를 부르면 안 된다")

    monkeypatch.setattr("app.services.notion_service.AsyncClient", explode)

    with pytest.raises(NotionWriteError, match="날짜"):
        await create_calendar_event({"name": "회의", "date": "내일쯤"})

    assert called is False
