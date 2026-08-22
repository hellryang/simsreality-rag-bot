"""kakao_service 단위 테스트.

외부 호출은 httpx.MockTransport로 가짜 응답을 돌려준다. 실제 KakaoWork에
요청을 보내지 않으므로 App Key 없이도 돈다.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.schemas import Document
from app.services import kakao_service
from app.services.kakao_service import KakaoWorkError


# 패치하기 전의 진짜 클래스. 팩토리 안에서 httpx.AsyncClient를 그대로 부르면
# 방금 바꿔치기한 팩토리를 다시 부르게 되어 무한 재귀가 난다.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_transport(monkeypatch, handler):
    """kakao_service가 쓰는 AsyncClient를 가짜 전송으로 바꿔치기한다."""

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler),
            base_url=kwargs.get("base_url", kakao_service.BASE_URL),
        )

    monkeypatch.setattr(kakao_service.httpx, "AsyncClient", factory)


# --- API 호출 --------------------------------------------------------


async def test_send_message_unwraps_the_success_envelope(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"success": True, "message": {"id": "m-1"}})

    mock_transport(monkeypatch, handler)

    message = await kakao_service.send_message("c-9", "안녕하세요")

    assert message == {"id": "m-1"}


async def test_error_envelope_becomes_an_exception(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "error": {"code": "missing_parameter", "message": "conversation_id 없음"},
            },
        )

    mock_transport(monkeypatch, handler)

    with pytest.raises(KakaoWorkError) as exc_info:
        await kakao_service.send_message("c-9", "안녕하세요")

    assert exc_info.value.code == "missing_parameter"


async def test_auth_failure_is_not_retried(monkeypatch):
    """401은 몇 번을 다시 보내도 결과가 같으므로 즉시 중단해야 한다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"success": False})

    mock_transport(monkeypatch, handler)

    with pytest.raises(KakaoWorkError, match="invalid_authentication"):
        await kakao_service.get_bot_info()

    assert len(calls) == 1


async def test_rate_limit_is_retried(monkeypatch):
    """429는 retry-after만큼 쉬고 다시 시도한다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"success": False})
        return httpx.Response(200, json={"success": True, "info": {"title": "jnu"}})

    mock_transport(monkeypatch, handler)

    info = await kakao_service.get_bot_info()

    assert info["title"] == "jnu"
    assert len(calls) == 2


async def test_reply_to_user_swallows_api_errors(monkeypatch):
    """답장 실패로 콜백 처리를 실패시키면 카카오워크가 재전송한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": False, "error": {"code": "nope", "message": "실패"}}
        )

    mock_transport(monkeypatch, handler)

    await kakao_service.reply_to_user("user-1", "답변")  # 예외가 새어 나오면 실패


# --- 문서 변환 -------------------------------------------------------


def test_build_document_marks_the_source_as_kakaowork():
    document = kakao_service.build_document(text="회의 내용", title="카카오워크 대화")

    assert isinstance(document, Document)
    assert document.source == "kakaowork"
    assert document.created_at  # 인용 메타데이터라 비면 안 된다


def test_documents_with_different_titles_get_different_chunk_ids():
    """제목이 같으면 덮어쓰고, 다르면 따로 쌓여야 한다.

    KakaoWork는 url이 없어서 chunk_id 식별이 title에 걸린다.
    """
    from app.models.schemas import Chunk

    first = kakao_service.build_document("내용", "카카오워크 대화 (2026-08-16T01:00:00+00:00)")
    second = kakao_service.build_document("내용", "카카오워크 대화 (2026-08-16T02:00:00+00:00)")
    same = kakao_service.build_document("다른 내용", "보고서.pdf")
    same_again = kakao_service.build_document("수정된 내용", "보고서.pdf")

    assert Chunk.from_document(first, "내용", 0).chunk_id != Chunk.from_document(second, "내용", 0).chunk_id
    assert (
        Chunk.from_document(same, "다른 내용", 0).chunk_id
        == Chunk.from_document(same_again, "수정된 내용", 0).chunk_id
    )


async def test_collect_returns_empty_because_there_is_no_read_api():
    """KakaoWork에는 대화·파일 조회 API가 없다. 배치 수집 대상이 아니다."""
    assert await kakao_service.collect_kakao_documents() == []


def test_ingest_documents_on_empty_input_does_nothing():
    assert kakao_service.ingest_documents([]) == 0


# --- Block Kit -------------------------------------------------------


def test_welcome_blocks_hide_the_upload_button_without_a_url():
    _, blocks = kakao_service.welcome_blocks()
    actions = [b for b in blocks if b["type"] == "action"][0]

    assert all(e["action"].get("name") != "upload_file" for e in actions["elements"])


def test_welcome_blocks_show_the_upload_button_with_a_url():
    _, blocks = kakao_service.welcome_blocks("https://example.com/kakao/upload?token=x")
    actions = [b for b in blocks if b["type"] == "action"][0]
    upload = [e for e in actions["elements"] if e["action"].get("name") == "upload_file"][0]

    assert upload["action"]["type"] == kakao_service.ACTION_OPEN_BROWSER
    assert upload["action"]["value"].startswith("https://")


def test_buttons_nest_their_action_and_always_carry_a_value():
    """평평한 action_type/action_name으로 보내면 카카오워크가 400으로 거부한다.

    실물 API에서 [invalid_parameter]를 맞고 확인한 사양이라 회귀를 막아 둔다.
    사양: https://docs.kakaoi.ai/kakao_work/blockkit/buttonblock/
    """
    _, blocks = kakao_service.welcome_blocks()
    buttons = [b for b in blocks if b["type"] == "action"][0]["elements"]

    assert buttons, "웰컴 메시지에는 버튼이 있어야 한다"
    for button in buttons:
        assert "action_type" not in button and "action_name" not in button
        assert button["style"] in ("default", "primary", "danger")
        assert len(button["text"]) <= 20
        # value는 필수다. 비어 있으면 블록 전체가 거부된다.
        assert button["action"]["value"]


def test_question_modal_has_a_text_input():
    view = kakao_service.question_modal()["view"]
    names = [b["name"] for b in view["blocks"] if b["type"] == "input"]

    assert names == [kakao_service.FIELD_QUESTION]


def test_chat_log_modal_has_a_text_input():
    view = kakao_service.chat_log_modal()["view"]
    names = [b["name"] for b in view["blocks"] if b["type"] == "input"]

    assert names == [kakao_service.FIELD_CHAT_LOG]


@pytest.mark.parametrize(
    "build", [kakao_service.question_modal, kakao_service.chat_log_modal]
)
def test_modals_carry_every_required_view_field(build):
    """필수 필드가 하나라도 빠지면 200을 돌려줘도 "모달 불러오기 실패"가 된다.

    value를 빠뜨려서 실제로 겪은 문제라 회귀를 막아 둔다.
    사양: https://docs.kakaoi.ai/kakao_work/webapireference/reactive/
    """
    view = build()["view"]

    for field in ("title", "accept", "decline", "value", "blocks"):
        assert view.get(field), f"view.{field}가 비어 있으면 모달이 뜨지 않는다"


@pytest.mark.parametrize(
    "build", [kakao_service.question_modal, kakao_service.chat_log_modal]
)
def test_modals_use_only_modal_blocks(build):
    """모달에는 label/input/select만 넣을 수 있다.

    말풍선용 블록(text, header, divider, action…)을 넣으면 거부된다.
    """
    blocks = build()["view"]["blocks"]

    assert blocks
    assert all(b["type"] in ("label", "input", "select") for b in blocks)
