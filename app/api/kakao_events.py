"""KakaoWork Webhook 수신.

토큰 환경변수화 + 서명/출처 검증 원칙은 Slack과 동일하게 적용한다.

--- 엔드포인트 ------------------------------------------------------

  POST /kakao/request   Request URL  : 버튼을 눌렀을 때 띄울 모달 JSON 응답
  POST /kakao/callback  Callback URL : 버튼 클릭·모달 제출 결과 수신
  GET  /kakao/upload    업로드 폼 (파일 API가 없어 웹으로 받는다)
  POST /kakao/upload    업로드 수신 → 텍스트 추출 → 벡터 DB 적재

--- 왜 이런 모양인가 ------------------------------------------------

KakaoWork 봇은 사용자가 채팅창에 그냥 친 문장을 받을 수 없다. Callback URL은
`submit_action` 버튼 클릭과 모달 제출만 전달한다. 그래서 자유 문장은
[질문하기] 버튼 → 모달 입력칸을 거쳐 들어온다.

--- 즉시 ack 패턴 ---------------------------------------------------

Slack은 3초 내 200 응답이 없으면 재전송한다. KakaoWork의 정확한 타임아웃은
공식 문서에서 확인하지 못했으므로, 같은 사고를 막기 위해 동일하게
**즉시 200을 돌려주고 실제 처리(Claude 호출, 임베딩)는 백그라운드**로 뺀다.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.models.schemas import Answer
from app.services import kakao_service, kakao_upload
from app.services.qa_pipeline import answer_question, format_answer
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kakao", tags=["kakaowork"])


# --- 요청 검증 -------------------------------------------------------


def _verify(*candidates: str | None) -> None:
    """우리가 등록한 URL로 들어온 요청인지 확인한다.

    **KakaoWork는 인증 헤더도 서명도 붙이지 않는다**(공식 문서 확인). 보내는
    헤더는 Content-Type 하나뿐이다. 그래서 Slack의 signing secret에 해당하는
    검증이 성립하지 않는다.

    대신 관리자에 등록하는 **URL의 쿼리스트링에 공유 비밀을 섞는다.**
    카카오워크는 등록된 URL을 그대로 호출하므로 토큰이 함께 넘어온다.

        Request URL   https://<도메인>/kakao/request?token=<비밀>
        Callback URL  https://<도메인>/kakao/callback?token=<비밀>

    헤더(X-Callback-Token)도 계속 받아준다. curl이나 /docs로 직접 찔러
    테스트할 때 편하기 때문이다.

    콜백은 공개망에 열려 있다. KAKAOWORK_CALLBACK_TOKEN을 비우면 검증이
    통째로 꺼지므로, 배포 환경에서는 반드시 채운다.

    사양: https://docs.kakaoi.ai/kakao_work/webapireference/reactive/
    """
    expected = settings.kakaowork_callback_token
    if not expected:
        return
    if any(candidate == expected for candidate in candidates):
        return

    logger.warning("콜백 토큰 불일치. 요청을 거부합니다.")
    raise HTTPException(status_code=401, detail="invalid callback token")


# --- 콜백 페이로드 해석 ----------------------------------------------


def _dig(payload: dict[str, Any], *keys: str) -> str:
    """여러 후보 키를 순서대로 찾아 첫 번째로 잡히는 값을 문자열로.

    실물 페이로드로 확인한 필드는 아래 두 개이며, 각 호출부에서 첫 번째
    후보로 두었다. 뒤에 남은 후보들은 사양이 바뀌었을 때를 대비한 여유분이라
    지워도 동작에는 영향이 없다.

        react_user_id   버튼을 누른 사람
        value           버튼의 action.value (어떤 버튼인지)
    """
    for key in keys:
        node: Any = payload
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node not in (None, "", {}, []):
            return str(node)
    return ""


def _collect_inputs(payload: dict[str, Any]) -> dict[str, str]:
    """모달 입력값을 {이름: 값}으로 모은다.

    실물로 확인한 형태는 `actions`이고, 키는 Input Block의 name이다.

        "actions": {"question": "3차 코칭에 대한 정보 알려줘"}
    """
    for key in ("actions", "inputs", "values", "submit_values"):
        node = payload.get(key)
        if isinstance(node, dict) and node:
            return {str(k): _flatten(v) for k, v in node.items()}
    return {}


def _flatten(value: Any) -> str:
    """{"value": "..."} 처럼 한 겹 더 싸여 오는 경우를 벗긴다."""
    if isinstance(value, dict):
        for key in ("value", "text", "selected_value"):
            if key in value:
                return str(value[key])
        return str(value)
    if isinstance(value, list):
        return ", ".join(_flatten(v) for v in value)
    return str(value)


# --- Request URL -----------------------------------------------------


@router.post("/request")
async def request_url(
    payload: dict[str, Any],
    token: str = "",
    x_callback_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """버튼(action.type=call_modal)을 누르면 카카오워크가 여기를 호출한다.

    응답으로 준 모달이 사용자 화면에 뜨고, 거기 입력한 값이
    /kakao/callback으로 넘어온다.

    페이로드 형태(공식 문서 확인):
        {"type": "request_modal", "value": "<버튼의 action.value>",
         "action_time": ..., "message": {...}, "react_user_id": ...}
    """
    _verify(token, x_callback_token)
    # 어느 버튼인지는 value로 온다. _button()이 value에 action_name을 넣어
    # 두므로 여기서 "ask_question" / "submit_chat_log"가 그대로 잡힌다.
    action_name = _dig(payload, "value", "action_name")
    logger.info("Request URL 호출: action=%s", action_name)

    if action_name == kakao_service.BUTTON_CHAT_LOG:
        return kakao_service.chat_log_modal()
    if action_name == kakao_service.BUTTON_MANAGE:
        # 모달을 여는 시점에도 react_user_id가 온다(실측). 그 사람이 이 방에서
        # 저장한 글만 골라 Select에 채운다. 콜백엔 방 번호가 오므로 이름으로
        # 바꿔 room_label(이름으로 저장돼 있음)과 맞춘다.
        user_id = _dig(payload, "react_user_id", "message.user_id")
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        room = await kakao_service.get_room_name(conversation_id) if conversation_id else None
        items = VectorStore().list_by_submitter(user_id, room_label=room) if user_id else []
        return kakao_service.manage_docs_modal(items)
    return kakao_service.question_modal()


# --- Callback URL ----------------------------------------------------


@router.post("/callback")
async def callback_url(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    token: str = "",
    x_callback_token: str | None = Header(default=None),
) -> dict[str, str]:
    """버튼 클릭 결과와 모달 제출 결과가 들어온다.

    Claude 호출과 임베딩은 수 초가 걸리므로 여기서 기다리지 않는다.

    페이로드 형태(공식 문서 확인):
        {"type": "submission", "actions": {"<input name>": "<입력값>"},
         "value": "<모달을 띄운 버튼의 value>", "react_user_id": ...}
    """
    _verify(token, x_callback_token)

    # 콜백은 두 종류다.
    #   1) 모달 제출 : "actions"에 입력값이 들어 있다.
    #   2) 일반 메시지: "text"에 사용자가 친 문장이 들어 있다(actions 없음).
    # 일반 메시지를 먼저 갈라낸다. 저장 대상 방이면 그 문장을 벡터 DB에 넣는다.
    text = _dig(payload, "text").strip()
    conversation_id = _dig(payload, "conversation_id")
    if text and not payload.get("actions"):
        return await _handle_plain_message(payload, text, conversation_id, background_tasks)

    inputs = _collect_inputs(payload)
    user_id = _dig(payload, "react_user_id", "user_id", "user.id", "message.user_id")
    value = _dig(payload, "value")
    logger.info("Callback 수신: user=%s value=%s fields=%s", user_id, value, list(inputs))
    logger.debug("Callback 원본: %s", payload)

    # 상세 메시지의 [삭제]/[수정] 버튼(submit_action)은 모달이 아니라 버튼
    # 클릭이라 inputs가 없고 value가 실려 온다. 먼저 갈라낸다.
    if value.startswith(kakao_service.ACTION_DELETE_PREFIX):
        chunk_id = value[len(kakao_service.ACTION_DELETE_PREFIX):]
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        background_tasks.add_task(_handle_delete, user_id, chunk_id, conversation_id)
        return {"status": "ok"}
    if value.startswith(kakao_service.ACTION_EDIT_PREFIX):
        # 수정은 아직 미구현. 카카오워크가 모달 기본값·모달 체이닝을 지원하지
        # 않아, 기존 내용을 채워 보여주는 방식을 정하는 중이다(웹 리다이렉션
        # 또는 카카오워크 문의 결과에 따라). 지금은 안내만 한다.
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        background_tasks.add_task(
            _reply, user_id, conversation_id,
            "수정 기능은 준비 중입니다. 지금은 삭제 후 다시 저장해 주세요.",
        )
        return {"status": "edit_pending"}

    if not inputs:
        # 모달을 거치지 않은 단순 버튼 클릭. 지금은 기록만 한다.
        return {"status": "ignored"}

    if not user_id:
        logger.error("사용자를 특정하지 못해 답장할 수 없습니다. 원본: %s", payload)
        return {"status": "no_user"}

    question = inputs.get(kakao_service.FIELD_QUESTION, "").strip()
    chat_log = inputs.get(kakao_service.FIELD_CHAT_LOG, "").strip()
    select_index = inputs.get(kakao_service.FIELD_DELETE_TARGET, "").strip()

    if question:
        background_tasks.add_task(_handle_question, user_id, question)
    elif chat_log:
        # 방은 message 안에 있다. 모달을 띄운 방을 그대로 room_label로 남긴다.
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        background_tasks.add_task(_handle_chat_log, user_id, chat_log, conversation_id)
    elif select_index:
        # 조회 모달에서 글을 골랐다. 상세 내용을 메시지로 보여준다(삭제 버튼 포함).
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        background_tasks.add_task(_handle_show_detail, user_id, select_index, conversation_id)
    else:
        logger.warning("알 수 없는 입력: %s", list(inputs))
        return {"status": "unknown_input"}

    return {"status": "ok"}


# --- 일반 메시지 (명령형 저장) ---------------------------------------
#
# 카카오워크는 그룹방에서 봇을 호출한 메시지(/봇이름 ○○○)만 콜백으로
# 넘겨준다. 사용자가 그냥 친 일반 대화는 봇 서버로 오지 않는다(실측 확인).
# 그래서 "방을 켜두면 오가는 대화가 자동 저장"되는 방식은 그룹방에서
# 불가능하다. 대신 사용자가 저장할 내용을 명시적으로 봇에게 넘긴다:
#
#     /연습용 저장 다음 주 화요일 3시 킥오프 회의
#         → text = "저장 다음 주 화요일 3시 킥오프 회의"
#         → "저장 " 뒤의 내용을 벡터 DB에 넣는다.

CMD_SAVE = "저장"
# 버튼 메뉴를 다시 부르는 명령어. 카카오워크는 봇 이름만(`/연습용`) 치면
# 콜백을 보내지 않고 "내용을 입력하라"는 자체 UI를 띄운다. 그래서 뒤에
# 붙일 키워드가 필요하다: `/연습용 메뉴`.
CMD_MENU = {"메뉴", "도움말", "menu", "help"}


async def _handle_plain_message(
    payload: dict[str, Any],
    text: str,
    conversation_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """봇을 호출한 메시지를 처리한다.

        "메뉴" / "도움말"  → [질문하기]·[대화 정리 요청] 버튼 메뉴를 띄운다.
        "저장 <내용>"      → <내용>을 벡터 DB에 저장한다.
        그 외              → 무엇을 할 수 있는지 짧게 안내한다.
    """
    user_id = _dig(payload, "user_id", "react_user_id")
    logger.info(
        "일반 메시지 수신: conv=%s user_id=%s text=%r", conversation_id, user_id, text
    )

    # "메뉴" / "도움말" — 버튼 메뉴를 띄운다.
    if text.strip() in CMD_MENU:
        await _show_menu(conversation_id)
        return {"status": "menu"}

    # "저장 <내용>" — 내용을 벡터 DB에 저장한다.
    if text.startswith(CMD_SAVE + " "):
        content = text[len(CMD_SAVE):].strip()
        if content:
            background_tasks.add_task(_store_message, conversation_id, content)
            return {"status": "stored"}

    # 알 수 없는 입력. 무엇을 할 수 있는지 알려준다(메뉴 자체를 띄우진 않아
    # 잡담마다 버튼이 쏟아지는 것을 막는다).
    await _reply_to_room(
        conversation_id,
        "'메뉴'를 입력하면 질문하기·대화 정리 버튼을 띄웁니다. "
        "바로 저장하려면 '저장 <내용>'을 보내주세요.",
    )
    return {"status": "hint"}


async def _show_menu(conversation_id: str) -> None:
    """[질문하기]·[대화 정리 요청] 버튼이 담긴 업무 도우미 메뉴를 방에 보낸다."""
    text, blocks = kakao_service.welcome_blocks()
    try:
        await kakao_service.send_message(conversation_id, text, blocks)
    except kakao_service.KakaoWorkError as exc:
        logger.error("메뉴 표시 실패 (conv=%s): %s", conversation_id, exc)


async def _reply_to_room(conversation_id: str, text: str) -> None:
    """메시지가 온 방에 직접 답한다.

    일반 메시지 콜백의 user_id는 실제 발신자가 아니라 봇 자신으로 온다(실측).
    그래서 reply_to_user(사용자에게 DM)는 봇이 자기한테 보내려다 실패한다.
    봇은 그 방의 멤버이므로 방(conversation_id)에는 직접 보낼 수 있다.
    """
    try:
        await kakao_service.send_message(conversation_id, text)
    except kakao_service.KakaoWorkError as exc:
        logger.error("방 답장 실패 (conv=%s): %s", conversation_id, exc)


async def _store_message(conversation_id: str, text: str) -> None:
    """'저장 <내용>'으로 넘어온 내용을 벡터 DB에 한 건 넣는다.

    일반 메시지 콜백에는 실제 발신자 정보가 없다(user_id가 봇으로 온다).
    그래서 발신자는 남기지 못하고 내용과 방·시각만 저장한다.
    """
    stamped = kakao_service.now_kst()
    line = f"[{stamped}] {text}"

    document = kakao_service.build_document(
        text=line,
        # 저장 건마다 시각을 넣어 제목이 겹치지 않게 한다(겹치면 덮어써진다).
        title=f"카카오워크 저장 - {conversation_id} ({stamped})",
        created_at=stamped,
        room_label=conversation_id,
    )
    try:
        kakao_service.ingest_documents([document])
        await _reply_to_room(conversation_id, f"저장했습니다: {text[:40]}")
    except Exception:
        logger.exception("메시지 저장 실패 (conversation_id=%s)", conversation_id)
        await _reply_to_room(conversation_id, "저장에 실패했습니다.")


async def _handle_question(user_id: str, question: str) -> None:
    """질문 → 벡터 검색 → Claude → 출처가 붙은 답변 발송."""
    try:
        answer: Answer = await answer_question(question)
        await kakao_service.reply_to_user(user_id, format_answer(answer))
    except Exception:
        # 백그라운드 태스크에서 예외가 나면 조용히 사라지므로 반드시 남긴다.
        logger.exception("질문 처리 실패 (user=%s)", user_id)
        await kakao_service.reply_to_user(
            user_id, "답변을 만들지 못했습니다. 잠시 후 다시 시도해 주세요."
        )


HEADLINE_MAX = 30


def _headline(text: str) -> str:
    """본문 첫 줄을 제목에 쓸 만한 길이로 다듬는다.

    제목이 시각뿐이면(`카카오워크 대화 (2026-08-22T05:52:17+00:00)`) 나중에
    목록에서 어느 것이 무엇인지 알 수 없고, 지우고 싶은 문서를 특정할 수도
    없다. 사용자는 시각이 아니라 내용을 기억한다.
    """
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:HEADLINE_MAX] + ("…" if len(line) > HEADLINE_MAX else "")
    return "제목 없음"


async def _handle_chat_log(user_id: str, chat_log: str, conversation_id: str = "") -> None:
    """모달에 넣은 내용을 KakaoWork 소스 문서로 적재한다.

    사용자가 자기 발언을 직접 넣는 것을 전제로 한다. 그러면 제출자(모달을
    낸 사람)가 곧 작성자이므로, xlsx 적재와 같은 형태(방·작성자·날짜·내용)로
    저장할 수 있다. 작성자 이름은 users.info로 조회해 붙인다.
    """
    stamped = kakao_service.now_kst()
    sender = await kakao_service.get_user_name(user_id) if user_id else ""
    # 방 번호 대신 이름(백석대 등)을 남긴다. 이름을 못 얻으면 번호를 쓴다.
    room = await kakao_service.get_room_name(conversation_id) if conversation_id else ""
    # 시각을 함께 넣어 제목이 겹치지 않게 한다. chunk_id가 제목에서 나오므로
    # (schemas._build_chunk_id) 제목이 같으면 앞서 저장한 문서를 덮어쓴다.
    title = f"카카오워크 대화: {_headline(chat_log)} ({stamped})"
    document = kakao_service.build_document(
        text=chat_log,
        title=title,
        created_at=stamped,
        # sender(이름)는 출처 표시에, submitted_by(ID)는 "내 저장 관리"에서
        # 본인 문서를 필터·삭제하는 데 쓴다. 이름은 동명이인 위험이 있어
        # 필터에는 ID를 쓴다. 자기 발언 입력이 전제라 둘은 같은 사람을 가리킨다.
        submitted_by=user_id,
        room_label=room,
        sender=sender,
        msg_date=stamped,
    )
    try:
        chunks = kakao_service.ingest_documents([document])
        await kakao_service.reply_to_user(
            user_id,
            f"대화 내용을 저장했습니다. ({chunks}개 조각) 이제 검색됩니다.\n"
            f"제목: {title}",
        )
    except Exception:
        logger.exception("대화 내용 적재 실패 (user=%s)", user_id)
        await kakao_service.reply_to_user(user_id, "저장에 실패했습니다.")


async def _handle_show_detail(user_id: str, select_index: str, conversation_id: str = "") -> None:
    """조회 모달에서 고른 글의 상세를 메시지로 보여준다([삭제] 버튼 포함).

    select_index는 조회 모달이 보여준 목록의 순번이다. 모달을 열 때와 똑같이
    (같은 사용자·같은 방) 목록을 다시 조회해 그 글을 찾는다.
    """
    store = VectorStore()
    room = await kakao_service.get_room_name(conversation_id) if conversation_id else None
    items = store.list_by_submitter(user_id, room_label=room)
    try:
        item = items[int(select_index)]
    except (ValueError, IndexError):
        await _reply(user_id, conversation_id, "글을 찾지 못했습니다.")
        return

    body, blocks = kakao_service.doc_detail_blocks(item)
    if conversation_id:
        try:
            await kakao_service.send_message(conversation_id, body, blocks)
            return
        except kakao_service.KakaoWorkError as exc:
            logger.error("상세 표시 실패 (conv=%s): %s", conversation_id, exc)
    # 방을 못 쓰면 최소한 텍스트로라도 알린다.
    await _reply(user_id, conversation_id, body)


async def _handle_delete(user_id: str, chunk_id: str, conversation_id: str = "") -> None:
    """상세 메시지의 [삭제] 버튼으로 고른 글을 삭제한다.

    버튼 value에서 온 chunk_id가 정말 이 사용자의 것인지 다시 확인한 뒤
    지운다(남의 글이 지워지지 않도록 방어).
    """
    store = VectorStore()
    room = await kakao_service.get_room_name(conversation_id) if conversation_id else None
    # 삭제하기 전에 그 글 내용을 확보해 둔다(삭제 후엔 못 읽으므로).
    mine = {item["chunk_id"]: item for item in store.list_by_submitter(user_id, room_label=room)}
    target = mine.get(chunk_id)
    if target is None:
        logger.warning("본인 글이 아니어서 삭제 거부: user=%s chunk=%s", user_id, chunk_id)
        await _reply(user_id, conversation_id, "삭제할 글을 찾지 못했습니다.")
        return

    removed = store.delete_by_ids([chunk_id])
    if removed:
        preview = " ".join(target["text"].split())[:40]
        await _reply(user_id, conversation_id, f"삭제했습니다:\n{preview}")
    else:
        await _reply(user_id, conversation_id, "삭제할 글을 찾지 못했습니다.")


async def _reply(user_id: str, conversation_id: str, text: str) -> None:
    """조회·삭제 결과를 본인 DM으로 알린다(그룹방을 지저분하게 하지 않도록).

    DM 발송은 reply_to_user가 conversations.open으로 1:1 방을 열어 보낸다.
    다만 콜백의 user_id가 봇으로 오는 경우 DM이 실패할 수 있어, 실패하면
    메시지가 온 방으로 보낸다(그래야 사용자가 결과를 못 보는 일이 없다).
    """
    if user_id:
        ok = await kakao_service.try_dm(user_id, text)
        if ok:
            return
    if conversation_id:
        await _reply_to_room(conversation_id, text)


# --- 파일 업로드 -----------------------------------------------------


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, "Malgun Gothic", sans-serif; max-width: 34rem;
         margin: 3rem auto; padding: 0 1.25rem; line-height: 1.6; color: #222; }}
  h1 {{ font-size: 1.25rem; }}
  .box {{ border: 1px solid #ddd; border-radius: 12px; padding: 1.25rem; }}
  button {{ background: #1b64da; color: #fff; border: 0; border-radius: 8px;
            padding: .6rem 1.1rem; font-size: 1rem; cursor: pointer; }}
  .muted {{ color: #666; font-size: .9rem; }}
  .error {{ color: #c0392b; }}
</style></head>
<body><h1>{title}</h1><div class="box">{body}</div></body></html>"""
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_form(token: str = "") -> HTMLResponse:
    try:
        kakao_upload.verify_upload_token(token)
    except kakao_upload.UploadTokenError as exc:
        return _page("링크를 사용할 수 없습니다", f'<p class="error">{exc}</p>')

    limit_mb = kakao_upload.MAX_UPLOAD_BYTES // (1024 * 1024)
    return _page(
        "문서 올리기",
        f"""<form method="post" action="/kakao/upload" enctype="multipart/form-data">
  <input type="hidden" name="token" value="{token}">
  <p><input type="file" name="file" required></p>
  <p><button type="submit">올리기</button></p>
</form>
<p class="muted">지원 형식: {', '.join(sorted(kakao_upload.SUPPORTED_SUFFIXES))} (최대 {limit_mb}MB)<br>
hwp는 PDF로 저장해서 올려주세요.</p>""",
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    background_tasks: BackgroundTasks,
    token: str = Form(...),
    file: UploadFile = File(...),
) -> HTMLResponse:
    try:
        user_id = kakao_upload.verify_upload_token(token)
    except kakao_upload.UploadTokenError as exc:
        return _page("링크를 사용할 수 없습니다", f'<p class="error">{exc}</p>')

    data = await file.read()
    if len(data) > kakao_upload.MAX_UPLOAD_BYTES:
        limit_mb = kakao_upload.MAX_UPLOAD_BYTES // (1024 * 1024)
        return _page("파일이 너무 큽니다", f'<p class="error">{limit_mb}MB 이하만 올릴 수 있습니다.</p>')

    filename = kakao_upload.safe_filename(file.filename or "upload")
    try:
        text = kakao_upload.extract_text(filename, data)
    except kakao_upload.UnsupportedFileType as exc:
        return _page("처리할 수 없는 파일입니다", f'<p class="error">{exc}</p>')

    # 임베딩은 수십 초가 걸릴 수 있으므로 사용자를 기다리게 하지 않는다.
    background_tasks.add_task(_ingest_upload, user_id, filename, text)

    return _page(
        "올렸습니다",
        f"<p><b>{filename}</b>을 처리하고 있습니다.</p>"
        '<p class="muted">완료되면 카카오워크로 알려드립니다. 창을 닫으셔도 됩니다.</p>',
    )


async def _ingest_upload(user_id: str, filename: str, text: str) -> None:
    """업로드된 문서를 벡터 DB에 넣고 사용자에게 알린다.

    같은 파일명으로 다시 올리면 chunk_id가 같아 덮어쓰기(upsert)가 된다.
    """
    document = kakao_service.build_document(
        text=text, title=filename, submitted_by=user_id
    )
    try:
        chunks = kakao_service.ingest_documents([document])
        await kakao_service.reply_to_user(
            user_id, f"'{filename}' 처리가 끝났습니다. ({chunks}개 조각) 이제 검색됩니다."
        )
    except Exception:
        logger.exception("업로드 적재 실패 (user=%s, file=%s)", user_id, filename)
        await kakao_service.reply_to_user(user_id, f"'{filename}' 처리에 실패했습니다.")
