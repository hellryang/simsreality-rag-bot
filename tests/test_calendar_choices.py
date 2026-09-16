"""유형·프로젝트명 선택지 테스트 — 담당: 마준서

노션 select는 없는 이름을 받으면 새 선택지를 만들어 버린다. 그래서 모델이
고른 값을 그대로 보내지 않고 목록과 대조한다.

목록을 코드에 적어 두면 노션에 항목이 늘 때마다 코드를 고쳐야 한다. 그래서
노션에서 읽고 10분간 기억한다. 노션을 못 읽어도 예약은 계속돼야 하므로
이전 목록이나 코드의 기본값으로 넘어간다.

실제 Notion API도 Claude API도 부르지 않는다.
"""
import pytest

from app.api import kakao_events
from app.services import notion_service
from app.services.claude_service import SCHEDULE_TOOL, build_schedule_tool
from app.services.notion_service import (
    CALENDAR_PROJECTS,
    CALENDAR_TYPES,
    PROP_PROJECT,
    PROP_TYPE,
    calendar_choices,  # conftest가 이름을 바꿔치기하기 전의 진짜 함수
    prepare_reserved_event,
)

TODAY = "2026-09-16"
NOTION_CHOICES = {
    PROP_TYPE: ("회의", "출장", "신규유형"),
    PROP_PROJECT: ("스마트물류센터 디지털트윈 플랫폼 구축", "신규 프로젝트"),
}


@pytest.fixture(autouse=True)
def clean_cache():
    notion_service.clear_choices_cache()
    yield
    notion_service.clear_choices_cache()


def _tool_enum(tool, field):
    return tool["input_schema"]["properties"]["events"]["items"]["properties"][field]["enum"]


# --- 모델에게 주는 선택지 -------------------------------------------


def test_the_tool_offers_the_notion_choices():
    """노션에 프로젝트를 추가하면 모델이 고를 수 있어야 한다."""
    tool = build_schedule_tool(NOTION_CHOICES)

    assert _tool_enum(tool, "project") == ["스마트물류센터 디지털트윈 플랫폼 구축", "신규 프로젝트"]
    assert _tool_enum(tool, "type") == ["회의", "출장", "신규유형"]


def test_the_default_tool_is_untouched():
    """선택지를 못 읽었을 때 쓰는 기본 양식이 오염되면 안 된다."""
    build_schedule_tool(NOTION_CHOICES)

    assert _tool_enum(SCHEDULE_TOOL, "project") == list(CALENDAR_PROJECTS)
    assert _tool_enum(SCHEDULE_TOOL, "type") == list(CALENDAR_TYPES)
    assert build_schedule_tool(None) is SCHEDULE_TOOL


# --- 등록 전 검증 ---------------------------------------------------


def test_a_project_in_the_list_is_kept():
    prepared = prepare_reserved_event(
        {"name": "회의", "date_type": "tomorrow", "project": "신규 프로젝트"},
        TODAY,
        NOTION_CHOICES,
    )

    assert prepared["project"] == "신규 프로젝트"


def test_a_project_outside_the_list_is_cleared():
    """노션에 없는 이름을 보내면 새 선택지가 생긴다. 그래서 비운다."""
    prepared = prepare_reserved_event(
        {"name": "회의", "date_type": "tomorrow", "project": "없는 프로젝트",
         "type": "없는유형"},
        TODAY,
        NOTION_CHOICES,
    )

    assert prepared["project"] == ""
    assert prepared["type"] == ""


def test_without_choices_the_builtin_list_is_used():
    prepared = prepare_reserved_event(
        {"name": "회의", "date_type": "tomorrow", "project": CALENDAR_PROJECTS[0]}, TODAY
    )

    assert prepared["project"] == CALENDAR_PROJECTS[0]


# --- 선택지 조회와 캐시 ---------------------------------------------


async def test_choices_are_read_from_notion_and_remembered(monkeypatch):
    calls = []

    async def fake_read():
        calls.append(1)
        return NOTION_CHOICES

    monkeypatch.setattr(notion_service, "_read_choices", fake_read)

    first = await calendar_choices()
    second = await calendar_choices()

    assert first[PROP_PROJECT] == NOTION_CHOICES[PROP_PROJECT]
    assert second == first
    assert len(calls) == 1  # 두 번째는 기억해 둔 값을 쓴다


async def test_the_cache_expires(monkeypatch):
    calls = []

    async def fake_read():
        calls.append(1)
        return NOTION_CHOICES

    monkeypatch.setattr(notion_service, "_read_choices", fake_read)
    monkeypatch.setattr(notion_service, "CHOICES_TTL_SEC", 0)

    await calendar_choices()
    await calendar_choices()

    assert len(calls) == 2


async def test_force_reads_again(monkeypatch):
    calls = []

    async def fake_read():
        calls.append(1)
        return NOTION_CHOICES

    monkeypatch.setattr(notion_service, "_read_choices", fake_read)

    await calendar_choices()
    await calendar_choices(force=True)

    assert len(calls) == 2


async def test_a_notion_failure_falls_back_to_the_builtin_list(monkeypatch):
    async def boom():
        raise RuntimeError("노션 죽음")

    monkeypatch.setattr(notion_service, "_read_choices", boom)

    choices = await calendar_choices()

    assert choices[PROP_TYPE] == CALENDAR_TYPES
    assert choices[PROP_PROJECT] == CALENDAR_PROJECTS


async def test_a_later_failure_keeps_the_last_good_list(monkeypatch):
    """한 번 읽어 둔 목록이 있으면 조회가 실패해도 그걸 쓴다."""

    async def ok():
        return NOTION_CHOICES

    monkeypatch.setattr(notion_service, "_read_choices", ok)
    await calendar_choices()

    async def boom():
        raise RuntimeError("노션 죽음")

    monkeypatch.setattr(notion_service, "_read_choices", boom)
    monkeypatch.setattr(notion_service, "CHOICES_TTL_SEC", 0)

    assert (await calendar_choices())[PROP_PROJECT] == NOTION_CHOICES[PROP_PROJECT]


# --- 등록 흐름 · DM ---------------------------------------------------


async def test_the_project_is_saved_and_shown_in_the_dm(monkeypatch):
    created: list[dict] = []

    async def extracted(chat_log, today, choices=None):
        assert choices  # 선택지를 모델에게 넘겨야 프로젝트를 고를 수 있다
        return [{"name": "정밀유도무기 프로젝트 회의", "date_type": "tomorrow",
                 "time": "16:00",
                 "project": "정밀유도무기 디지털트윈 시뮬레이션 개발"}]

    async def fake_create(event, database_id=None):
        created.append(event)
        return "https://notion.so/new"

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)

    joined = "\n".join(await kakao_events._register_notion_events("...", TODAY))

    assert created[0]["project"] == "정밀유도무기 디지털트윈 시뮬레이션 개발"
    assert "정밀유도무기 디지털트윈 시뮬레이션 개발" in joined


# --- 새로고침 명령 ----------------------------------------------------


async def test_the_refresh_command_rereads_the_choices(monkeypatch):
    sent: list[str] = []
    calls = []

    async def fake_read():
        calls.append(1)
        return NOTION_CHOICES

    async def fake_reply(conversation_id, text):
        sent.append(text)

    monkeypatch.setattr(notion_service, "_read_choices", fake_read)
    monkeypatch.setattr(kakao_events, "_reply_to_room", fake_reply)
    # conftest가 calendar_choices를 고정값으로 바꿔 두므로 진짜 함수로 되돌린다.
    monkeypatch.setattr(notion_service, "calendar_choices", calendar_choices)

    await calendar_choices()  # 한 번 기억시켜 둔다
    await kakao_events._refresh_choices("conv-1")

    assert len(calls) == 2  # 캐시를 버리고 다시 읽는다
    assert "유형 3개" in sent[0]
    assert "프로젝트명 2개" in sent[0]


def test_refresh_is_a_known_command():
    assert "새로고침" in kakao_events.CMD_REFRESH
