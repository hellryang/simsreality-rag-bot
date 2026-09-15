"""Claude 캘린더 도구(Tool Use) 테스트 — 담당: 마준서

여기서 보는 것은 두 가지다.

1. 도구가 돌려주는 **문자열**이 모델에게 쓸모 있는 모양인가.
   모델은 이 문자열만 보고 답하므로, 일정과 빈 날이 다 담겨야 한다.
2. 도구가 실패해도 **예외가 밖으로 나가지 않는가.**
   Tool Use 루프 안에서 예외가 나면 대화가 끊겨 사용자는 아무 답도 못 받는다.
   실패는 예외가 아니라 "조회 실패"라는 글로 모델에게 전해져야 한다.

실제 Notion API도 Claude API도 부르지 않는다.
"""
import pytest

from app.models.schemas import NO_CONTEXT_ANSWER, Answer
from app.services import notion_service
from app.services.claude_service import (
    CALENDAR_TOOL,
    _run_calendar_tool,
    answer_with_citations,
    build_system_prompt,
)

# 2026-09-09은 수요일. 주 경계를 넘는 계산을 확인하기 좋은 기준일이다.
TODAY = "2026-09-09"


# --- 도구 정의 -------------------------------------------------------


def test_the_tool_takes_a_period_label_not_computed_dates():
    """모델은 라벨만 고르고, 날짜 계산은 파이썬이 한다.

    실측에서 2026-09-09(수)에 "저번 주"를 물었더니 모델이 09-01~09-07을
    잡았다(정답 08-31~09-06). 시작도 끝도 하루씩 밀려 이번 주 월요일이
    섞이고 저번 주 월요일이 빠졌다. 그래서 날짜를 직접 받지 않는다.
    """
    schema = CALENDAR_TOOL["input_schema"]

    assert schema["required"] == ["period"]
    assert "last_week" in schema["properties"]["period"]["enum"]


# --- 도구 실행 -------------------------------------------------------


async def test_the_tool_reports_events_and_free_days(monkeypatch):
    async def fake_query(start, end, database_id=None):
        return [
            {
                "date": "2026-09-15",
                "end_date": "",
                "name": "킥오프 회의",
                "time": "15:00",
                "place": "본관 3층",
                "type": "회의",
            }
        ]

    monkeypatch.setattr(notion_service, "query_calendar_events", fake_query)

    result = await _run_calendar_tool({"period": "custom",
                                      "start_date": "2026-09-14",
                                      "end_date": "2026-09-16"}, TODAY)

    assert "킥오프 회의" in result
    assert "2026-09-15" in result
    # 빈 날이 함께 실려야 모델이 "언제 시간 되나"에 답할 수 있다.
    assert "2026-09-14" in result
    assert "2026-09-16" in result


async def test_a_fully_booked_range_says_so_explicitly(monkeypatch):
    """빈 날이 없을 때 아무 말도 없으면 모델이 "비었다"고 지어낼 수 있다."""

    async def fake_query(start, end, database_id=None):
        return [{"date": "2026-09-14", "end_date": "2026-09-16", "name": "워크숍"}]

    monkeypatch.setattr(notion_service, "query_calendar_events", fake_query)

    result = await _run_calendar_tool({"period": "custom",
                                      "start_date": "2026-09-14",
                                      "end_date": "2026-09-16"}, TODAY)

    assert "없음" in result


async def test_a_notion_failure_becomes_text_not_an_exception(monkeypatch):
    async def boom(start, end, database_id=None):
        raise notion_service.NotionWriteError("대상 DB가 연결되지 않았습니다")

    monkeypatch.setattr(notion_service, "query_calendar_events", boom)

    result = await _run_calendar_tool({"period": "custom",
                                      "start_date": "2026-09-14",
                                      "end_date": "2026-09-16"}, TODAY)

    assert "조회 실패" in result


async def test_an_unexpected_error_also_becomes_text(monkeypatch):
    async def boom(start, end, database_id=None):
        raise RuntimeError("예상 못 한 오류")

    monkeypatch.setattr(notion_service, "query_calendar_events", boom)

    result = await _run_calendar_tool({"period": "custom",
                                      "start_date": "2026-09-14",
                                      "end_date": "2026-09-16"}, TODAY)

    assert "조회 실패" in result


# --- 시스템 프롬프트 -------------------------------------------------


def test_the_prompt_gains_calendar_rules_only_when_today_is_given():
    """today 없이 부르면 기존 동작 그대로여야 한다(팀 공용 경로)."""
    plain = build_system_prompt()
    dated = build_system_prompt("2026-09-06")

    assert "lookup_calendar" not in plain
    assert "lookup_calendar" in dated
    assert "2026-09-06" in dated


def test_the_prompt_tells_the_model_not_to_do_date_math():
    """날짜 계산은 파이썬이 한다. 모델이 손대면 틀린다."""
    dated = build_system_prompt("2026-09-06")

    assert "직접 계산하거나" in dated


def test_tool_results_are_declared_valid_grounds():
    """이 규칙이 없으면 모델이 '관련 문서를 찾지 못했습니다'로 답해 버린다.

    출처 번호를 붙이라는 규칙 2와 근거 없으면 거절하라는 규칙 3이,
    출처 번호가 없는 도구 결과와 충돌하기 때문이다.
    """
    dated = build_system_prompt("2026-09-06")

    assert "출처 번호를 붙이지 않고" in dated


# --- 기존 계약 유지 --------------------------------------------------


async def test_empty_hits_still_skip_claude_when_calendar_is_off():
    """tests/test_contracts.py의 계약이 그대로 지켜져야 한다.

    캘린더 도구는 **옵트인**이다. 켜지 않은 호출부는 예전처럼
    근거가 없으면 모델을 부르지 않는다.
    """
    answer = await answer_with_citations("배포 일정 알려줘", hits=[])

    assert isinstance(answer, Answer)
    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


# --- 기준일(base_date) --------------------------------------------------
#
# 실측: "8/24 기준 다음 주에 홍길동님과 회의할 수 있는 날짜"를 물었더니 모델이
# 기준일을 버리고 {'period': 'next_week'}만 넘겨 오늘 기준 9/21~27이 조회됐다.
# 기준일을 따로 받아, 그 날짜로 라벨을 계산하는 일은 파이썬이 한다.


def _capture_query(monkeypatch):
    """조회 기간만 기록하는 가짜 노션 조회."""
    calls: list[tuple[str, str]] = []

    async def fake_query(start, end, database_id=None):
        calls.append((start, end))
        return []

    monkeypatch.setattr(notion_service, "query_calendar_events", fake_query)
    return calls


def test_the_tool_accepts_an_optional_base_date():
    schema = CALENDAR_TOOL["input_schema"]

    assert "base_date" in schema["properties"]
    assert "base_date" not in schema["required"]


async def test_base_date_moves_the_label_to_that_date(monkeypatch):
    calls = _capture_query(monkeypatch)

    # 2026-08-24(월) 기준 다음 주 = 08-31(월) ~ 09-06(일)
    result = await _run_calendar_tool(
        {"period": "next_week", "base_date": "2026-08-24", "keyword": "홍길동"}, TODAY
    )

    assert calls == [("2026-08-31", "2026-09-06")]
    assert "기준일: 2026-08-24" in result


async def test_base_date_on_a_midweek_day_still_uses_monday_weeks(monkeypatch):
    calls = _capture_query(monkeypatch)

    # 2026-08-27은 목요일. 그 주는 08-24(월) ~ 08-30(일)이다.
    await _run_calendar_tool({"period": "this_week", "base_date": "2026-08-27"}, TODAY)

    assert calls == [("2026-08-24", "2026-08-30")]


async def test_without_base_date_today_is_the_anchor(monkeypatch):
    calls = _capture_query(monkeypatch)

    # TODAY 2026-09-09(수) 기준 다음 주 = 09-14 ~ 09-20
    for tool_input in ({"period": "next_week"}, {"period": "next_week", "base_date": ""}):
        result = await _run_calendar_tool(tool_input, TODAY)
        assert "기준일" not in result

    assert calls == [("2026-09-14", "2026-09-20")] * 2


async def test_a_malformed_base_date_is_rejected_not_replaced_by_today(monkeypatch):
    """형식이 틀린 기준일을 오늘로 바꾸면 8월을 물었는데 9월이 조회된다.

    조용히 대체하지 않고 실패를 돌려줘 모델이 고쳐 다시 부르게 한다.
    """
    calls = _capture_query(monkeypatch)

    for bad in ("8월 24일", "2026-8-24", "8/24"):
        result = await _run_calendar_tool(
            {"period": "next_week", "base_date": bad}, TODAY
        )
        assert result.startswith("조회 실패")
        assert bad in result

    assert calls == []


async def test_the_past_warning_compares_with_the_real_today(monkeypatch):
    """기준일로 계산해도 '이미 지난 기간' 판정은 실제 오늘과 비교한다."""
    _capture_query(monkeypatch)

    result = await _run_calendar_tool(
        {"period": "this_week", "base_date": "2026-08-24"}, TODAY
    )

    assert "이미 지났다" in result


async def test_custom_dates_ignore_base_date(monkeypatch):
    calls = _capture_query(monkeypatch)

    await _run_calendar_tool(
        {
            "period": "custom",
            "start_date": "2026-08-24",
            "end_date": "2026-08-28",
            "base_date": "2026-07-01",
        },
        TODAY,
    )

    assert calls == [("2026-08-24", "2026-08-28")]


def test_the_prompt_explains_base_date():
    prompt = build_system_prompt(TODAY)

    assert "base_date" in prompt


# --- 시간대 단위 가능 여부 -----------------------------------------------
#
# 요청: "9/1은 10~11시 제외 가능"처럼, 일정이 있는 날도 잡힌 시간을 보여 달라.
# 예전에는 일정이 하나라도 있으면 그 날이 통째로 빈 날에서 빠졌다.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09:30-10:00", ("09:30", "10:00")),
        ("15:00~18:00", ("15:00", "18:00")),  # 물결표도 노션에 실제로 있다
        ("9:00-17:00", ("09:00", "17:00")),  # 한 자리 시각은 0을 채운다
        (" 14:00 - 15:30 ", ("14:00", "15:30")),
        ("15:00", ("15:00", "")),  # 시작만 적힘
        ("18:30-", ("18:30", "")),  # 끝이 비어 있음
        ("17:00-09:00", ("17:00", "")),  # 끝이 앞서면 끝을 모르는 것으로
    ],
)
def test_time_ranges_in_the_calendar_are_read(text, expected):
    assert notion_service.parse_time_range(text) == expected


@pytest.mark.parametrize("text", ["", "오후 3시", "종일", "25:00", "10:75"])
def test_unreadable_times_are_not_guessed(text):
    assert notion_service.parse_time_range(text) is None


def _day(days, date):
    return next(d for d in days if d["date"] == date)


def test_a_day_with_a_timed_event_is_partially_available():
    days = notion_service.compute_day_availability(
        "2026-09-01",
        "2026-09-02",
        [{"date": "2026-09-01", "time": "10:00-11:00", "name": "주간회의"}],
    )

    assert _day(days, "2026-09-01")["status"] == "partial"
    assert _day(days, "2026-09-01")["busy"] == [("10:00", "11:00")]
    assert _day(days, "2026-09-02")["status"] == "free"


def test_overlapping_slots_on_one_day_are_merged():
    days = notion_service.compute_day_availability(
        "2026-09-03",
        "2026-09-03",
        [
            {"date": "2026-09-03", "time": "10:00-11:00", "name": "A"},
            {"date": "2026-09-03", "time": "09:00-17:00", "name": "출장"},
            {"date": "2026-09-03", "time": "17:00-18:00", "name": "B"},  # 맞닿음
            {"date": "2026-09-03", "time": "19:00", "name": "C"},  # 끝 모름
        ],
    )

    assert _day(days, "2026-09-03")["busy"] == [("09:00", "18:00"), ("19:00", "")]


def test_separate_slots_stay_separate():
    days = notion_service.compute_day_availability(
        "2026-09-03",
        "2026-09-03",
        [
            {"date": "2026-09-03", "time": "14:00-15:30", "name": "B"},
            {"date": "2026-09-03", "time": "10:00-11:00", "name": "A"},
        ],
    )

    assert _day(days, "2026-09-03")["busy"] == [("10:00", "11:00"), ("14:00", "15:30")]


def test_an_event_without_time_is_not_assumed_all_day_or_free():
    """시간이 빈 일정(마일스톤, 반차 등)은 가능/불가를 단정하지 않는다."""
    days = notion_service.compute_day_availability(
        "2026-08-25",
        "2026-08-25",
        [
            {"date": "2026-08-25", "time": "", "name": "시험평가 준비 착수"},
            {"date": "2026-08-25", "time": "09:30-10:00", "name": "정기회의"},
        ],
    )

    day = _day(days, "2026-08-25")
    assert day["status"] == "unknown"
    assert day["untimed"] == ["시험평가 준비 착수"]
    assert day["busy"] == [("09:30", "10:00")]


def test_a_multi_day_event_applies_to_every_day_it_spans():
    days = notion_service.compute_day_availability(
        "2026-07-16",
        "2026-07-19",
        [{"date": "2026-07-17", "end_date": "2026-07-18", "time": "", "name": "워크숍"}],
    )

    assert [d["status"] for d in days] == ["free", "unknown", "unknown", "free"]


async def test_the_tool_lists_excluded_times_for_partially_busy_days(monkeypatch):
    """사용자가 원한 모양: 전일 가능한 날 + 제외할 시간이 붙은 날."""

    async def fake_query(start, end, database_id=None):
        return [
            {"date": "2026-09-01", "time": "10:00-11:00", "name": "주간회의",
             "attendees": "홍길동"},
            {"date": "2026-09-03", "time": "10:00-11:00", "name": "보고",
             "attendees": "홍길동"},
            {"date": "2026-09-03", "time": "14:00-15:30", "name": "리뷰",
             "attendees": "홍길동"},
            {"date": "2026-09-02", "time": "10:00-11:00", "name": "남의 회의",
             "attendees": "김민준"},
        ]

    monkeypatch.setattr(notion_service, "query_calendar_events", fake_query)

    result = await _run_calendar_tool(
        {"period": "custom", "start_date": "2026-08-31", "end_date": "2026-09-04",
         "keyword": "홍길동"},
        TODAY,
    )

    # 홍길동이 없는 회의(9/2)는 홍길동에게 전일 가능이다.
    assert "[전일 가능 - 홍길동 일정 없음] 3일: 2026-08-31(월), 2026-09-02(수), 2026-09-04(금)" in result
    assert "2026-09-01(화): 10:00~11:00 제외 가능" in result
    assert "2026-09-03(목): 10:00~11:00, 14:00~15:30 제외 가능" in result
