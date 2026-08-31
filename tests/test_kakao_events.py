"""kakao_events 엔드포인트 테스트.

Claude 호출과 임베딩은 느리고 돈이 들어서 전부 가짜로 바꾼다.
여기서 보는 것은 "요청이 올바른 처리로 연결되는가"뿐이다.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kakao_events
from app.core.config import settings
from app.services import kakao_service, kakao_upload

TOKEN = "test-callback-token"


@pytest.fixture(autouse=True)
def callback_token(monkeypatch):
    monkeypatch.setattr(settings, "kakaowork_callback_token", TOKEN)
    monkeypatch.setattr(settings, "kakaowork_public_url", "https://tunnel.example.com")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(kakao_events.router)
    return TestClient(app)


@pytest.fixture
def handled(monkeypatch):
    """백그라운드 처리 대신 호출 기록만 남긴다."""
    calls = []

    async def fake_question(user_id, question):
        calls.append(("question", user_id, question))

    async def fake_chat_log(user_id, chat_log):
        calls.append(("chat_log", user_id, chat_log))

    async def fake_upload(user_id, filename, text):
        calls.append(("upload", user_id, filename, text))

    monkeypatch.setattr(kakao_events, "_handle_question", fake_question)
    monkeypatch.setattr(kakao_events, "_handle_chat_log", fake_chat_log)
    monkeypatch.setattr(kakao_events, "_ingest_upload", fake_upload)
    return calls


def auth():
    return {"X-Callback-Token": TOKEN}


# --- 인증 ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/kakao/request", "/kakao/callback"])
def test_endpoints_reject_a_wrong_token(client, path):
    response = client.post(path, json={}, headers={"X-Callback-Token": "nope"})

    assert response.status_code == 401


# --- Request URL (모달) ----------------------------------------------


def test_request_url_returns_the_question_modal(client):
    response = client.post("/kakao/request", json={"action_name": "ask_question"}, headers=auth())

    view = response.json()["view"]
    assert [b["name"] for b in view["blocks"] if b["type"] == "input"] == [
        kakao_service.FIELD_QUESTION
    ]


def test_request_url_returns_the_chat_log_modal(client):
    response = client.post(
        "/kakao/request", json={"action_name": "submit_chat_log"}, headers=auth()
    )

    view = response.json()["view"]
    assert [b["name"] for b in view["blocks"] if b["type"] == "input"] == [
        kakao_service.FIELD_CHAT_LOG
    ]


# --- Callback URL ----------------------------------------------------


def test_callback_routes_a_question(client, handled):
    response = client.post(
        "/kakao/callback",
        json={
            "react_user_id": "user-42",
            "actions": {kakao_service.FIELD_QUESTION: "배포는 어디에 하나요"},
        },
        headers=auth(),
    )

    assert response.json() == {"status": "ok"}
    assert handled == [("question", "user-42", "배포는 어디에 하나요")]


def test_callback_routes_a_chat_log(client, handled):
    client.post(
        "/kakao/callback",
        json={
            "user_id": "user-7",
            "inputs": {kakao_service.FIELD_CHAT_LOG: {"value": "A: 안녕\nB: 반가워"}},
        },
        headers=auth(),
    )

    assert handled == [("chat_log", "user-7", "A: 안녕\nB: 반가워")]


def test_callback_ignores_a_plain_button_click(client, handled):
    response = client.post(
        "/kakao/callback", json={"action_name": "some_button"}, headers=auth()
    )

    assert response.json() == {"status": "ignored"}
    assert handled == []


def test_callback_without_a_user_does_not_process(client, handled):
    response = client.post(
        "/kakao/callback",
        json={"actions": {kakao_service.FIELD_QUESTION: "질문"}},
        headers=auth(),
    )

    assert response.json() == {"status": "no_user"}
    assert handled == []


def test_callback_survives_an_unknown_payload_shape(client, handled):
    response = client.post("/kakao/callback", json={"something": "unexpected"}, headers=auth())

    assert response.status_code == 200
    assert handled == []


# --- 업로드 ----------------------------------------------------------


def test_build_upload_url_uses_the_public_base():
    url = kakao_upload.build_upload_url("user-42")

    assert url.startswith("https://tunnel.example.com/kakao/upload?token=")


def test_upload_form_is_shown_for_a_valid_token(client):
    token = kakao_upload.make_upload_token("user-42")

    response = client.get(f"/kakao/upload?token={token}")

    assert 'enctype="multipart/form-data"' in response.text


def test_upload_form_refuses_a_bad_token(client):
    response = client.get("/kakao/upload?token=forged.1.2")

    assert "링크를 사용할 수 없습니다" in response.text
    assert "<form" not in response.text


def test_upload_accepts_a_text_file(client, handled):
    token = kakao_upload.make_upload_token("user-42")

    response = client.post(
        "/kakao/upload",
        data={"token": token},
        files={"file": ("규정.txt", "환불은 14일 이내".encode("utf-8"), "text/plain")},
    )

    assert "올렸습니다" in response.text
    assert len(handled) == 1
    kind, user_id, filename, text = handled[0]
    assert (kind, user_id, filename) == ("upload", "user-42", "규정.txt")
    assert "환불은 14일 이내" in text


def test_upload_refuses_an_unsupported_type(client, handled):
    token = kakao_upload.make_upload_token("user-42")

    response = client.post(
        "/kakao/upload",
        data={"token": token},
        files={"file": ("그림.png", b"\x89PNG", "image/png")},
    )

    assert "처리할 수 없는 파일입니다" in response.text
    assert handled == []


def test_upload_refuses_a_bad_token(client, handled):
    response = client.post(
        "/kakao/upload",
        data={"token": "forged.1.2"},
        files={"file": ("a.txt", b"hello", "text/plain")},
    )

    assert "링크를 사용할 수 없습니다" in response.text
    assert handled == []


# --- 저장된 문서의 제목 ----------------------------------------------


def test_headline_uses_the_first_non_empty_line():
    """제목이 시각뿐이면 목록에서 어느 것이 무엇인지 알 수 없다."""
    from app.api.kakao_events import _headline

    assert _headline("\n\n7주차 회의 내용 정리\n둘째 줄") == "7주차 회의 내용 정리"


def test_headline_truncates_a_long_line():
    from app.api.kakao_events import HEADLINE_MAX, _headline

    result = _headline("가" * 100)

    assert result == "가" * HEADLINE_MAX + "…"


def test_headline_falls_back_when_there_is_no_text():
    from app.api.kakao_events import _headline

    assert _headline("   \n\n  ") == "제목 없음"


# --- 일반 메시지 (명령형 저장: "저장 <내용>") -----------------------


@pytest.fixture
def stored(monkeypatch):
    """실제 저장 대신 호출 기록만 남긴다."""
    calls = []

    async def fake_store(conversation_id, text):
        calls.append((conversation_id, text))

    monkeypatch.setattr(kakao_events, "_store_message", fake_store)
    return calls


@pytest.fixture(autouse=True)
def _no_reply(monkeypatch):
    async def fake_reply(conversation_id, text):
        pass

    monkeypatch.setattr(kakao_events, "_reply_to_room", fake_reply)


def _msg(text, cid="room-1", uid="u-1"):
    return {"text": text, "conversation_id": cid, "user_id": uid}


def test_save_command_stores_its_content(client, stored):
    r = client.post(
        "/kakao/callback", json=_msg("저장 다음 주 화요일 3시 킥오프 회의"), headers=auth()
    )

    assert r.json()["status"] == "stored"
    assert stored == [("room-1", "다음 주 화요일 3시 킥오프 회의")]


def test_save_keyword_is_stripped_from_the_content(client, stored):
    """'저장'이라는 명령어 자체는 내용에 포함되면 안 된다."""
    client.post("/kakao/callback", json=_msg("저장 회의록 정리"), headers=auth())

    assert stored == [("room-1", "회의록 정리")]


def test_save_without_content_is_not_stored(client, stored):
    r = client.post("/kakao/callback", json=_msg("저장"), headers=auth())

    assert r.json()["status"] == "empty_save"
    assert stored == []


def test_a_plain_message_without_the_command_is_ignored(client, stored):
    """'저장'으로 시작하지 않는 문장은 저장하지 않는다."""
    r = client.post("/kakao/callback", json=_msg("오늘 점심 뭐 먹지"), headers=auth())

    assert r.json()["status"] == "ignored"
    assert stored == []


def test_a_word_starting_with_save_is_not_a_command(client, stored):
    """'저장'으로 시작하는 다른 단어('저장소')를 명령으로 오인하면 안 된다."""
    r = client.post("/kakao/callback", json=_msg("저장소 정리 완료"), headers=auth())

    assert r.json()["status"] == "ignored"
    assert stored == []
