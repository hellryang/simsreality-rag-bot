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
