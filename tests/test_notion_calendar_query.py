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
    is_past_range,
    parse_calendar_page,
    query_calendar_events,
    resolve_period,
)

# 2026-09-09은 수요일. 주 경계를 넘는 계산을 확인하기 좋은 기준일이다.
TODAY = "2026-09-09"


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


# --- 기간 해석 -------------------------------------------------------
#
# 이 부분이 없던 동안 실제로 틀렸다. 2026-09-09(수)에 "저번 주"를 물었더니
# 모델이 09-01~09-07을 잡아, 이번 주 월요일(09-07)이 답에 섞이고 저번 주
# 월요일(08-31)이 빠졌다. 그래서 계산을 파이썬으로 가져왔다.


def test_last_week_starts_on_the_previous_monday():
    """실제로 났던 오류의 회귀 테스트.

    수요일 기준으로 "저번 주"는 08-31(월)~09-06(일)이다.
    09-07(이번 주 월요일)이 포함되면 안 된다.
    """
    assert resolve_period("last_week", TODAY) == ("2026-08-31", "2026-09-06")


def test_this_week_runs_monday_to_sunday():
    assert resolve_period("this_week", TODAY) == ("2026-09-07", "2026-09-13")


def test_next_week_does_not_overlap_this_week():
    this_start, this_end = resolve_period("this_week", TODAY)
    next_start, next_end = resolve_period("next_week", TODAY)

    assert next_start > this_end
    assert (next_start, next_end) == ("2026-09-14", "2026-09-20")


def test_today_and_tomorrow_are_single_days():
    assert resolve_period("today", TODAY) == (TODAY, TODAY)
    assert resolve_period("tomorrow", TODAY) == ("2026-09-10", "2026-09-10")


def test_months_end_on_the_real_last_day():
    assert resolve_period("this_month", TODAY) == ("2026-09-01", "2026-09-30")
    assert resolve_period("next_month", TODAY) == ("2026-10-01", "2026-10-31")


def test_last_month_is_the_previous_calendar_month():
    """실제로 없어서 틀렸던 라벨.

    "지난달 일정 알려줘"를 물었더니 last_month가 enum에 없어서 기본
    범위(오늘~2주)로 떨어졌다. 8월을 물었는데 9월 일정이 나왔다.
    """
    assert resolve_period("last_month", TODAY) == ("2026-08-01", "2026-08-31")


def test_months_reach_three_back_and_three_forward():
    """오늘 기준 앞뒤 3개월까지 라벨로 닿아야 한다.

    라벨이 없으면 기본 범위로 떨어져 엉뚱한 기간을 조회한다
    (last_month가 없던 동안 실제로 그랬다).
    """
    expected = {
        "three_months_ago": ("2026-06-01", "2026-06-30"),
        "two_months_ago": ("2026-07-01", "2026-07-31"),
        "last_month": ("2026-08-01", "2026-08-31"),
        "this_month": ("2026-09-01", "2026-09-30"),
        "next_month": ("2026-10-01", "2026-10-31"),
        "in_two_months": ("2026-11-01", "2026-11-30"),
        "in_three_months": ("2026-12-01", "2026-12-31"),
    }

    for label, window in expected.items():
        assert resolve_period(label, TODAY) == window, label


def test_month_labels_cross_the_year_in_both_directions():
    """month +- n 으로 계산하면 0월이나 13월이 되어 터진다."""
    assert resolve_period("three_months_ago", "2026-01-15") == (
        "2025-10-01",
        "2025-10-31",
    )
    assert resolve_period("in_three_months", "2026-11-20") == (
        "2027-02-01",
        "2027-02-28",
    )


def test_last_month_crosses_the_year_boundary():
    """1월의 지난달은 작년 12월이다. month-1로 계산하면 0월이 된다."""
    assert resolve_period("last_month", "2026-01-15") == ("2025-12-01", "2025-12-31")


def test_last_month_handles_a_short_february():
    assert resolve_period("last_month", "2026-03-10") == ("2026-02-01", "2026-02-28")


def test_next_month_crosses_the_year_boundary():
    """12월의 다음 달은 이듬해 1월이다. month+1로 계산하면 터진다."""
    assert resolve_period("next_month", "2026-12-15") == ("2027-01-01", "2027-01-31")


def test_february_length_is_not_hardcoded():
    assert resolve_period("this_month", "2028-02-10") == ("2028-02-01", "2028-02-29")


def test_custom_takes_the_dates_as_given():
    assert resolve_period("custom", TODAY, "2026-09-15", "2026-09-20") == (
        "2026-09-15",
        "2026-09-20",
    )


def test_a_reversed_custom_range_is_swapped_not_rejected():
    """사용자가 거꾸로 말해도 빈 구간이 되어 조용히 0건이 나오면 안 된다."""
    assert resolve_period("custom", TODAY, "2026-09-20", "2026-09-15") == (
        "2026-09-15",
        "2026-09-20",
    )


def test_custom_without_valid_dates_is_refused():
    with pytest.raises(NotionWriteError, match="기간"):
        resolve_period("custom", TODAY, "다음주", "")


def test_an_unknown_label_falls_back_to_a_wide_window():
    """모르는 라벨이면 넓게 훑는다.

    좁게 잡으면(오늘~2주) 과거 질문을 놓치고 "정보가 없다"고 단정한다.
    실제로 그렇게 8월 일정을 못 찾은 적이 있다.
    """
    start, end = resolve_period("한참 전", TODAY)

    assert start == "2026-06-01"    # 3개월 전 1일
    assert end == "2026-12-31"      # 3개월 후 말일


def test_range_labels_span_several_months():
    """"이전에", "언제였지"처럼 시점이 불분명한 질문에 쓰는 라벨.

    한 달짜리 라벨(last_month 등)만 있으면 모델이 어느 달을 볼지 정하지
    못해 조회를 포기한다. 실제로 "이후경이 참여한 이전 대구 출장"을 물었을
    때 조회조차 하지 않고 "정보가 없다"고 답했다.
    """
    assert resolve_period("past_3_months", TODAY) == ("2026-06-01", TODAY)
    assert resolve_period("next_3_months", TODAY) == (TODAY, "2026-12-31")
    assert resolve_period("around_3_months", TODAY) == ("2026-06-01", "2026-12-31")


def test_a_finished_range_is_flagged_as_past():
    """지난 기간을 물으면 그 사실을 답변에 알려야 한다."""
    assert is_past_range("2026-09-06", TODAY) is True
    assert is_past_range("2026-09-20", TODAY) is False
