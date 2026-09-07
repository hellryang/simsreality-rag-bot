"""노션 캘린더 조회 + 빈 날 계산 테스트 — 담당: 마준서

"다음주 빈 날 알려줘"를 받쳐 주는 코드다. 이 기능이 벡터 검색이 아니라
노션 직접 조회로 되어 있는 이유는 이렇다.

벡터 검색은 "있는 것 중 비슷한 것"을 top-k개 돌려주는 도구인데, 빈 날은
`범위의 모든 날 − 일정이 있는 날`이라는 차집합이라 한쪽 집합이 **전부**
있어야 계산된다. top-5만 받아서는 나머지 날에 일정이 있는지 알 수 없고,
모르는 채로 답하면 환각이 된다.

그리고 날짜 계산은 파이썬이 한다. 모델에게 맡기면 틀리는 종류의 작업이다.
여기 있는 테스트는 그 계산이 맞는지를 본다. 실제 Notion API는 부르지 않는다.
"""
import pytest

from app.core.config import settings
from app.services.notion_service import (
    NotionWriteError,
    compute_free_days,
    parse_calendar_page,
    query_calendar_events,
)


# --- 빈 날 계산 ------------------------------------------------------


def test_days_without_events_are_reported_as_free():
    events = [{"date": "2026-09-15", "end_date": ""}]

    free = compute_free_days("2026-09-14", "2026-09-16", events)

    assert [f["date"] for f in free] == ["2026-09-14", "2026-09-16"]


def test_a_multi_day_event_blocks_every_day_it_spans():
    """워크숍 3일 중 가운데 날이 비어 있는 것으로 나오면 안 된다.

    시작일만 보고 판단하면 딱 이 실수가 난다.
    """
    events = [{"date": "2026-09-16", "end_date": "2026-09-18"}]

    free = compute_free_days("2026-09-15", "2026-09-19", events)

    assert [f["date"] for f in free] == ["2026-09-15", "2026-09-19"]


def test_no_events_means_every_day_is_free():
    free = compute_free_days("2026-09-14", "2026-09-16", [])

    assert len(free) == 3


def test_a_fully_booked_range_has_no_free_days():
    events = [
        {"date": "2026-09-14", "end_date": ""},
        {"date": "2026-09-15", "end_date": "2026-09-16"},
    ]

    assert compute_free_days("2026-09-14", "2026-09-16", events) == []


def test_free_days_carry_a_weekday_label():
    """사용자에게 "9월 19일(토)"처럼 보여주기 위한 것.

    주말인지 아닌지를 사람이 바로 알아볼 수 있어야 한다.
    """
    free = compute_free_days("2026-09-19", "2026-09-19", [])

    assert free == [{"date": "2026-09-19", "weekday": "토"}]


def test_a_row_with_a_broken_date_is_skipped_not_fatal():
    """노션에서 손으로 고칠 수 있는 문제로 전체 조회를 실패시키지 않는다."""
    events = [
        {"date": "2026-09-15", "end_date": ""},
        {"date": "언젠가", "end_date": ""},
    ]

    free = compute_free_days("2026-09-14", "2026-09-16", events)

    assert [f["date"] for f in free] == ["2026-09-14", "2026-09-16"]


def test_an_end_date_before_the_start_does_not_hang():
    """뒤집힌 기간이 들어와도 무한 루프가 되면 안 된다."""
    events = [{"date": "2026-09-15", "end_date": "2026-09-10"}]

    free = compute_free_days("2026-09-14", "2026-09-16", events)

    assert [f["date"] for f in free] == ["2026-09-14", "2026-09-16"]


# --- 노션 페이지 → 일정 dict -----------------------------------------


def _page(**props):
    base = {
        "url": "https://notion.so/page-1",
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "킥오프 회의"}]},
            "날짜": {"date": {"start": "2026-09-15", "end": None}},
        },
    }
    base["properties"].update(props)
    return base


def test_a_calendar_page_becomes_an_event_dict():
    event = parse_calendar_page(
        _page(
            **{
                "시간": {"rich_text": [{"plain_text": "15:00"}]},
                "장소": {"rich_text": [{"plain_text": "본관 3층 대회의실"}]},
                "유형": {"select": {"name": "회의"}},
            }
        )
    )

    assert event["name"] == "킥오프 회의"
    assert event["date"] == "2026-09-15"
    assert event["time"] == "15:00"
    assert event["place"] == "본관 3층 대회의실"
    assert event["type"] == "회의"
    assert event["url"] == "https://notion.so/page-1"


def test_missing_properties_become_empty_strings():
    """노션에서 비워 둔 속성 때문에 조회가 터지면 안 된다."""
    event = parse_calendar_page(_page())

    assert event["time"] == ""
    assert event["place"] == ""
    assert event["type"] == ""
    assert event["end_date"] == ""


def test_a_null_select_does_not_crash():
    """값을 지우면 select가 None으로 온다. .get()만으로는 터진다."""
    event = parse_calendar_page(_page(**{"유형": {"select": None}}))

    assert event["type"] == ""


# --- 설정·입력 검증 --------------------------------------------------


async def test_a_malformed_range_is_refused_before_any_api_call(monkeypatch):
    monkeypatch.setattr(
        "app.services.notion_service.AsyncClient",
        lambda **kwargs: pytest.fail("API를 부르면 안 된다"),
    )

    with pytest.raises(NotionWriteError, match="기간"):
        await query_calendar_events("다음주", "2026-09-18")


async def test_querying_without_a_target_database_explains_why(monkeypatch):
    monkeypatch.setattr(settings, "notion_calendar_db_id", None)
    monkeypatch.setattr(settings, "notion_privatespace_api", "ntn_test")

    with pytest.raises(NotionWriteError, match="NOTION_CALENDAR_DB_ID"):
        await query_calendar_events("2026-09-14", "2026-09-18")
