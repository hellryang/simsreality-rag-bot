"""예약하기 날짜 계산 테스트 — 담당: 마준서

예약하기는 모델이 "다음 주 화요일"을 YYYY-MM-DD로 직접 계산해 넘겼다.
파이썬은 형식만 검사했으므로, 형식은 맞고 날짜가 틀린 값이 노션에 그대로
남을 수 있었다. 질문하기에서 "저번 주"를 틀렸던 것과 같은 종류의 오류다.

지금은 모델이 날짜 표현을 분류하고 숫자·요일만 옮겨 적으며, 날짜 계산은
resolve_event_date()가 한다. 여기서는 그 계산과, 등록 결과 DM이 날짜를
확인할 수 있는 모양인지를 본다.

실제 Notion API도 Claude API도 부르지 않는다.
"""
import pytest

from app.api import kakao_events
from app.services import notion_service
from app.services.claude_service import SCHEDULE_TOOL, build_schedule_system_prompt
from app.services.notion_service import (
    CALENDAR_TYPES,
    NotionWriteError,
    prepare_reserved_event,
    resolve_event_date,
)

# 2026-09-15는 화요일.
TODAY = "2026-09-15"


# --- 모델에게 주는 양식 ---------------------------------------------


def test_the_model_is_not_asked_for_a_computed_date():
    item = SCHEDULE_TOOL["input_schema"]["properties"]["events"]["items"]

    assert "date" not in item["properties"]
    assert "end_date" not in item["properties"]
    assert item["required"] == ["name", "date_type"]


def test_types_are_limited_to_the_calendar_choices():
    """노션 select는 없는 이름을 받으면 새 선택지를 만든다."""
    item = SCHEDULE_TOOL["input_schema"]["properties"]["events"]["items"]

    assert item["properties"]["type"]["enum"] == list(CALENDAR_TYPES)
    assert "정기회의" in CALENDAR_TYPES
    assert "발표" not in CALENDAR_TYPES


def test_the_prompt_forbids_date_arithmetic():
    assert "계산하지 않는다" in build_schedule_system_prompt(TODAY)


# --- 날짜 계산 ------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"date_type": "today"}, "2026-09-15"),
        ({"date_type": "tomorrow"}, "2026-09-16"),
        ({"date_type": "day_after_tomorrow"}, "2026-09-17"),
        ({"date_type": "days_later", "days": 3}, "2026-09-18"),
        ({"date_type": "days_later", "days": "10"}, "2026-09-25"),  # 문자열 숫자
        # 이번 주 = 09-14(월) ~ 09-20(일)
        ({"date_type": "this_week", "weekday": "금"}, "2026-09-18"),
        ({"date_type": "this_week", "weekday": "월"}, "2026-09-14"),  # 이미 지남
        ({"date_type": "next_week", "weekday": "화"}, "2026-09-22"),
        ({"date_type": "next_week", "weekday": "화요일"}, "2026-09-22"),
        ({"date_type": "next_week", "weekday": "일"}, "2026-09-27"),
        ({"date_type": "week_after_next", "weekday": "월"}, "2026-09-28"),
        ({"date_type": "exact", "month": 9, "day": 25}, "2026-09-25"),
        ({"date_type": "exact", "month": 9, "day": 15}, "2026-09-15"),  # 오늘
    ],
)
def test_date_expressions_become_dates(spec, expected):
    assert resolve_event_date(spec, TODAY) == (expected, "")


def test_a_month_day_that_already_passed_this_year_goes_to_next_year():
    """9월에 '1월 5일'을 예약하면 내년 1월 5일이다."""
    assert resolve_event_date({"date_type": "exact", "month": 1, "day": 5}, TODAY) == (
        "2027-01-05",
        "",
    )
    assert resolve_event_date({"date_type": "exact", "month": 9, "day": 14}, TODAY) == (
        "2027-09-14",
        "",
    )


def test_a_stated_year_is_kept_even_if_it_is_past():
    assert resolve_event_date(
        {"date_type": "exact", "year": 2026, "month": 1, "day": 5}, TODAY
    ) == ("2026-01-05", "")


def test_the_week_crosses_a_month_and_year_boundary():
    # 2026-12-30(수) 기준 다음 주 금요일 = 2027-01-08
    assert resolve_event_date(
        {"date_type": "next_week", "weekday": "금"}, "2026-12-30"
    ) == ("2027-01-08", "")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        # 9월 25일부터 27일까지 (달 생략)
        ({"date_type": "exact", "month": 9, "day": 25, "end_day": 27},
         ("2026-09-25", "2026-09-27")),
        # 9월 30일부터 10월 2일까지
        ({"date_type": "exact", "month": 9, "day": 30, "end_month": 10, "end_day": 2},
         ("2026-09-30", "2026-10-02")),
        # 12월 30일부터 1월 2일까지 - 종료일은 이듬해
        ({"date_type": "exact", "month": 12, "day": 30, "end_month": 1, "end_day": 2},
         ("2026-12-30", "2027-01-02")),
        # 다음 주 월요일부터 수요일까지
        ({"date_type": "next_week", "weekday": "월", "end_weekday": "수"},
         ("2026-09-21", "2026-09-23")),
    ],
)
def test_multi_day_events_get_an_end_date(spec, expected):
    assert resolve_event_date(spec, TODAY) == expected


def test_an_end_before_the_start_is_dropped():
    assert resolve_event_date(
        {"date_type": "next_week", "weekday": "수", "end_weekday": "월"}, TODAY
    ) == ("2026-09-23", "")


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"date_type": "someday"},
        {"date_type": "exact", "month": 9},  # 일 없음
        {"date_type": "exact", "month": 2, "day": 30},  # 없는 날
        {"date_type": "exact", "month": 13, "day": 1},
        {"date_type": "next_week"},  # 요일 없음
        {"date_type": "next_week", "weekday": "주말"},  # 못 읽는 요일
        {"date_type": "days_later"},
        {"date_type": "days_later", "days": -1},
    ],
)
def test_unresolvable_dates_raise_a_readable_error(spec):
    with pytest.raises(NotionWriteError):
        resolve_event_date(spec, TODAY)


# --- 등록 준비 ------------------------------------------------------


def test_prepare_fills_the_notion_date_fields():
    event = {"name": "킥오프", "date_type": "next_week", "weekday": "화", "type": "회의"}

    prepared = prepare_reserved_event(event, TODAY)

    assert prepared["date"] == "2026-09-22"
    assert prepared["end_date"] == ""
    assert prepared["type"] == "회의"
    assert "date" not in event  # 원본은 그대로


def test_prepare_clears_a_type_that_is_not_a_calendar_choice():
    prepared = prepare_reserved_event(
        {"name": "발표", "date_type": "tomorrow", "type": "발표"}, TODAY
    )

    assert prepared["type"] == ""


def test_prepare_leaves_an_already_dated_event_alone():
    """date_type이 없으면 계산하지 않는다(엑셀 적재 등 다른 경로)."""
    prepared = prepare_reserved_event({"name": "회의", "date": "2026-10-01"}, TODAY)

    assert prepared["date"] == "2026-10-01"


# --- 등록 결과 DM ---------------------------------------------------


async def test_the_dm_shows_weekday_attendees_and_the_computed_date(monkeypatch):
    """날짜가 틀려도 사용자가 DM만 보고 알아챌 수 있어야 한다."""

    async def extracted(chat_log, today):
        return [
            {
                "name": "킥오프 회의",
                "date_type": "next_week",
                "weekday": "화",
                "time": "15:00",
                "place": "본관 대회의실",
                "attendees": "김민준, 이후경",
            }
        ]

    created: list[dict] = []

    async def fake_create(event, database_id=None):
        created.append(event)
        return "https://notion.so/page-1"

    async def no_events(start, end, database_id=None):
        return []

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)
    monkeypatch.setattr(kakao_events.notion_service, "query_calendar_events", no_events)

    lines = await kakao_events._register_notion_events("다음 주 화요일 3시 킥오프", TODAY)
    joined = "\n".join(lines)

    assert created[0]["date"] == "2026-09-22"
    assert "2026-09-22(화)" in joined
    assert "참석 김민준, 이후경" in joined
    assert "지난 날짜" not in joined


async def test_the_dm_flags_a_date_that_already_passed(monkeypatch):
    async def extracted(chat_log, today):
        return [{"name": "주간회의", "date_type": "this_week", "weekday": "월"}]

    async def fake_create(event, database_id=None):
        return "https://notion.so/page-1"

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)

    lines = await kakao_events._register_notion_events("이번 주 월요일 회의", TODAY)

    assert "2026-09-14(월) · 지난 날짜" in "\n".join(lines)


async def test_an_unresolvable_date_is_reported_and_others_still_register(monkeypatch):
    async def extracted(chat_log, today):
        return [
            {"name": "킥오프", "date_type": "tomorrow"},
            {"name": "이상한 회의", "date_type": "exact", "month": 2, "day": 30},
        ]

    async def fake_create(event, database_id=None):
        return "https://notion.so/page-1"

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)

    joined = "\n".join(await kakao_events._register_notion_events("...", TODAY))

    assert "1건을 등록했습니다" in joined
    assert "킥오프" in joined
    assert "등록하지 못한 일정 1건" in joined
    assert "없는 날짜입니다: 2월 30일" in joined
