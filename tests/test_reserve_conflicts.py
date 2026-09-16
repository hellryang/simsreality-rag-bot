"""예약 겹침 알림 테스트 — 담당: 마준서

예약할 때 같은 시간에 같은 사람이 이미 다른 일정에 들어가 있으면 완료 DM에
알린다. 등록은 막지 않는다(확인 버튼 없음). 사람이 겹치지 않으면 같은
시간이어도 알리지 않는다. 비교하는 참석자는 입력에서 뽑은 사람뿐이다.

실제 Notion API도 Claude API도 부르지 않는다.
"""
import pytest

from app.api import kakao_events
from app.services.notion_service import attendee_names, find_conflicts

TODAY = "2026-09-15"


# --- 참석자 이름 해석 -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("김민준,이후경", {"김민준", "이후경"}),
        ("김민준, 이후경", {"김민준", "이후경"}),
        ("김민준 이후경", {"김민준", "이후경"}),  # 쉼표 없이 적어도
        ("강태준,홍길동(태원전장)", {"강태준", "홍길동"}),  # 소속은 버림
        ("김명환 대표님", {"김명환"}),  # 직함은 버림
        ("홍길동 상무", {"홍길동"}),
        ("김명환님", {"김명환"}),
        ("", set()),
    ],
)
def test_attendee_text_becomes_names(text, expected):
    assert attendee_names(text) == expected


# --- 겹침 판단 ------------------------------------------------------


def _existing(**fields):
    base = {"name": "기존 회의", "date": "2026-09-22", "end_date": "", "time": "",
            "attendees": "", "url": ""}
    base.update(fields)
    return base


def test_same_time_and_same_person_is_a_conflict():
    new = {"date": "2026-09-22", "time": "14:00", "attendees": "김민준, 김명환"}
    other = _existing(time="14:00-15:30", attendees="김민준,박도윤")

    [conflict] = find_conflicts(new, [other])

    assert conflict["shared"] == ["김민준"]
    assert conflict["kind"] == "time"


def test_same_time_but_different_people_is_not_a_conflict():
    """시나리오의 벤더 미팅처럼 사람이 안 겹치면 알리지 않는다(사용자 결정)."""
    new = {"date": "2026-09-22", "time": "14:00", "attendees": "김명환"}
    other = _existing(time="14:00-15:30", attendees="김민준,박도윤")

    assert find_conflicts(new, [other]) == []


def test_same_person_at_a_different_time_is_not_a_conflict():
    new = {"date": "2026-09-22", "time": "10:00-11:00", "attendees": "김민준"}
    other = _existing(time="14:00-15:30", attendees="김민준")

    assert find_conflicts(new, [other]) == []


def test_touching_edges_do_not_overlap():
    """15:30에 끝나는 회의 바로 뒤 15:30 시작은 겹침이 아니다."""
    new = {"date": "2026-09-22", "time": "15:30-16:00", "attendees": "김민준"}
    other = _existing(time="14:00-15:30", attendees="김민준")

    assert find_conflicts(new, [other]) == []


def test_a_start_only_time_is_treated_as_one_hour():
    # 새 일정 14:00 → 14:00~15:00, 기존 14:30~16:00 → 겹침
    new = {"date": "2026-09-22", "time": "14:00", "attendees": "이후경"}
    other = _existing(time="14:30-16:00", attendees="이후경")
    assert len(find_conflicts(new, [other])) == 1

    # 새 일정 14:00 → 15:00까지, 기존 15:00~ → 안 겹침
    other = _existing(time="15:00", attendees="이후경")
    assert find_conflicts(new, [other]) == []


def test_an_untimed_event_on_the_same_day_is_flagged_as_untimed():
    new = {"date": "2026-09-22", "time": "14:00", "attendees": "이후경"}
    other = _existing(name="태원전장 출장", time="", attendees="이후경")

    [conflict] = find_conflicts(new, [other])

    assert conflict["kind"] == "untimed"


def test_multi_day_ranges_are_compared_by_span():
    new = {"date": "2026-10-01", "end_date": "2026-10-03", "time": "", "attendees": "이후경"}
    inside = _existing(date="2026-10-02", time="10:00-11:00", attendees="이후경")
    before = _existing(date="2026-09-30", time="10:00-11:00", attendees="이후경")

    assert [c["event"] for c in find_conflicts(new, [inside, before])] == [inside]


def test_an_existing_multi_day_event_covers_the_new_date():
    new = {"date": "2026-10-02", "time": "10:00", "attendees": "이후경"}
    trip = _existing(date="2026-10-01", end_date="2026-10-03", attendees="이후경")

    assert len(find_conflicts(new, [trip])) == 1


def test_titles_and_affiliations_do_not_hide_the_same_person():
    new = {"date": "2026-09-22", "time": "10:00", "attendees": "홍길동 상무"}
    other = _existing(time="10:00-11:00", attendees="강태준,홍길동(태원전장)")

    assert find_conflicts(new, [other])[0]["shared"] == ["홍길동"]


def test_no_attendees_means_nothing_to_compare():
    new = {"date": "2026-09-22", "time": "14:00", "attendees": ""}
    other = _existing(time="14:00-15:30", attendees="김민준")

    assert find_conflicts(new, [other]) == []


# --- 등록 흐름 · DM ---------------------------------------------------


def _patch(monkeypatch, extracted_events, existing=None, query_error=False):
    created: list[dict] = []
    queried: list[tuple[str, str]] = []

    async def extracted(chat_log, today, choices=None):
        return extracted_events

    async def fake_query(start, end, database_id=None):
        queried.append((start, end))
        if query_error:
            raise RuntimeError("노션 조회 실패")
        return existing or []

    async def fake_create(event, database_id=None):
        created.append(event)
        return "https://notion.so/new"

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "query_calendar_events", fake_query)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)
    return created, queried


async def test_a_conflict_still_registers_and_the_dm_warns(monkeypatch):
    created, queried = _patch(
        monkeypatch,
        [{"name": "김명환 대표 회의", "date_type": "next_week", "weekday": "화",
          "time": "14:00", "attendees": "김민준, 김명환"}],
        existing=[_existing(name="물류센서 벤더 미팅", time="14:00-15:30",
                            attendees="김민준,박도윤", url="https://notion.so/old")],
    )

    joined = "\n".join(await kakao_events._register_notion_events("...", TODAY))

    assert len(created) == 1  # 겹쳐도 등록한다
    assert queried == [("2026-09-22", "2026-09-22")]  # 계산된 날짜로 조회
    assert "1건을 등록했습니다" in joined
    assert "참석자 일정이 겹칩니다 (등록은 완료됨)" in joined
    assert "김명환 대표 회의 ↔ 물류센서 벤더 미팅" in joined
    assert "14:00-15:30" in joined
    assert "겹치는 참석자 김민준" in joined
    assert "https://notion.so/old" in joined


async def test_no_conflict_means_no_warning(monkeypatch):
    _patch(
        monkeypatch,
        [{"name": "회의", "date_type": "next_week", "weekday": "화",
          "time": "14:00", "attendees": "김명환"}],
        existing=[_existing(time="14:00-15:30", attendees="김민준,박도윤")],
    )

    joined = "\n".join(await kakao_events._register_notion_events("...", TODAY))

    assert "1건을 등록했습니다" in joined
    assert "겹칩니다" not in joined


async def test_without_attendees_notion_is_not_queried(monkeypatch):
    created, queried = _patch(
        monkeypatch,
        [{"name": "회의", "date_type": "tomorrow", "time": "14:00"}],
    )

    await kakao_events._register_notion_events("...", TODAY)

    assert queried == []
    assert len(created) == 1


async def test_a_failed_check_does_not_block_registration(monkeypatch):
    created, _ = _patch(
        monkeypatch,
        [{"name": "회의", "date_type": "tomorrow", "time": "14:00", "attendees": "김민준"}],
        query_error=True,
    )

    joined = "\n".join(await kakao_events._register_notion_events("...", TODAY))

    assert len(created) == 1
    assert "겹치는 일정을 확인하지 못했습니다" in joined
