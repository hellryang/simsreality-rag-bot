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
from app.core.config import settings
from app.services import kakao_service, notion_service
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


def _patch_register(monkeypatch, event):
    created: list[dict] = []

    async def extracted(chat_log, today, choices=None):
        return [event]

    async def fake_create(prepared, database_id=None):
        created.append(prepared)
        return "https://notion.so/new"

    monkeypatch.setattr(kakao_events.claude_service, "extract_schedule_events", extracted)
    monkeypatch.setattr(kakao_events.notion_service, "create_calendar_event", fake_create)
    return created


async def test_the_selected_project_is_saved_and_shown_in_the_dm(monkeypatch):
    """모달에서 고른 프로젝트를 그대로 쓴다."""
    created = _patch_register(
        monkeypatch, {"name": "프로젝트 회의", "date_type": "tomorrow", "time": "16:00"}
    )

    joined = "\n".join(
        await kakao_events._register_notion_events(
            "...", TODAY, "정밀유도무기 디지털트윈 시뮬레이션 개발"
        )
    )

    assert created[0]["project"] == "정밀유도무기 디지털트윈 시뮬레이션 개발"
    assert "정밀유도무기 디지털트윈 시뮬레이션 개발" in joined


async def test_without_a_selection_the_project_is_left_empty(monkeypatch):
    """고르지 않으면 비워 둔다. 모델이 문장에서 짐작한 값도 쓰지 않는다(사용자 결정)."""
    created = _patch_register(
        monkeypatch,
        {"name": "프로젝트 회의", "date_type": "tomorrow", "time": "16:00",
         "project": "스마트물류센터 디지털트윈 플랫폼 구축"},
    )

    await kakao_events._register_notion_events("...", TODAY)

    assert created[0]["project"] == ""


# --- 모달 선택 상자 ---------------------------------------------------


def _select_block(modal):
    blocks = modal["view"]["blocks"]
    return next(b for b in blocks if b.get("type") == "select")


def test_the_modal_offers_the_notion_projects():
    """선택지는 노션에서 읽은 목록이라, 프로젝트가 늘면 옵션도 늘어난다."""
    modal = kakao_service.reserve_modal(projects=("가 프로젝트", "나 프로젝트"))
    select = _select_block(modal)

    assert select["name"] == kakao_service.FIELD_PROJECT
    assert select["required"] is False
    assert [o["value"] for o in select["options"]] == ["가 프로젝트", "나 프로젝트"]


def test_the_modal_has_no_select_when_choices_are_unavailable():
    """노션을 못 읽었을 때 빈 선택 상자를 띄우지 않는다. 입력칸만 남는다."""
    modal = kakao_service.reserve_modal()

    assert all(b.get("type") != "select" for b in modal["view"]["blocks"])


def test_too_many_projects_are_cut_to_the_platform_limit():
    """카카오워크 select는 옵션 30개까지다(공식 문서)."""
    modal = kakao_service.reserve_modal(projects=tuple(f"P{i}" for i in range(40)))

    assert len(_select_block(modal)["options"]) == kakao_service.SELECT_OPTION_LIMIT


async def test_the_reserve_modal_reads_projects_from_notion(monkeypatch):
    async def fake_read():
        return NOTION_CHOICES

    monkeypatch.setattr(notion_service, "_read_choices", fake_read)
    monkeypatch.setattr(notion_service, "calendar_choices", calendar_choices)
    monkeypatch.setattr(settings, "kakaowork_callback_token", None)  # 토큰 검증 끔

    modal = await kakao_events.request_url(
        {"value": kakao_service.BUTTON_RESERVE}, token=""
    )

    options = [o["value"] for o in _select_block(modal)["options"]]
    assert options == list(NOTION_CHOICES[PROP_PROJECT])


async def test_a_notion_failure_still_opens_the_modal(monkeypatch):
    async def boom():
        raise RuntimeError("노션 죽음")

    monkeypatch.setattr(notion_service, "_read_choices", boom)
    monkeypatch.setattr(notion_service, "calendar_choices", calendar_choices)
    monkeypatch.setattr(settings, "kakaowork_callback_token", None)  # 토큰 검증 끔

    modal = await kakao_events.request_url(
        {"value": kakao_service.BUTTON_RESERVE}, token=""
    )

    # 기본 목록으로라도 띄운다. 선택지를 못 읽었다고 예약을 막지 않는다.
    assert [o["value"] for o in _select_block(modal)["options"]] == list(CALENDAR_PROJECTS)


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
