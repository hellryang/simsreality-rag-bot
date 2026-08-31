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
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.models.schemas import Answer
from app.services import kakao_service, kakao_upload
from app.services.qa_pipeline import answer_question, format_answer

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
    logger.info("Callback 수신: user=%s fields=%s", user_id, list(inputs))
    logger.debug("Callback 원본: %s", payload)

    if not inputs:
        # 모달을 거치지 않은 단순 버튼 클릭. 지금은 기록만 한다.
        return {"status": "ignored"}

    if not user_id:
        logger.error("사용자를 특정하지 못해 답장할 수 없습니다. 원본: %s", payload)
        return {"status": "no_user"}

    question = inputs.get(kakao_service.FIELD_QUESTION, "").strip()
    chat_log = inputs.get(kakao_service.FIELD_CHAT_LOG, "").strip()

    if question:
        background_tasks.add_task(_handle_question, user_id, question)
    elif chat_log:
        background_tasks.add_task(_handle_chat_log, user_id, chat_log)
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


async def _handle_plain_message(
    payload: dict[str, Any],
    text: str,
    conversation_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """봇을 호출한 메시지를 처리한다.

        "저장 <내용>"  → <내용>을 벡터 DB에 저장한다.
        그 외          → 지금은 무시한다(질문 처리는 [질문하기] 모달이 담당).
    """
    user_id = _dig(payload, "user_id", "react_user_id")
    logger.info(
        "일반 메시지 수신: conv=%s user_id=%s text=%r", conversation_id, user_id, text
    )

    # "저장" 또는 "저장 <내용>" 형태인지 본다. "저장"만 있으면 안내한다.
    if text == CMD_SAVE or text.startswith(CMD_SAVE + " "):
        content = text[len(CMD_SAVE):].strip()
        if not content:
            await _reply_to_room(conversation_id, "저장할 내용을 함께 적어주세요. 예) 저장 다음 주 화요일 3시 킥오프 회의")
            return {"status": "empty_save"}

        background_tasks.add_task(_store_message, conversation_id, content)
        return {"status": "stored"}

    return {"status": "ignored"}


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
    stamped = datetime.now(timezone.utc).isoformat(timespec="seconds")
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


async def _handle_chat_log(user_id: str, chat_log: str) -> None:
    """붙여넣은 대화 내용을 KakaoWork 소스 문서로 적재한다."""
    stamped = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # 시각을 함께 넣어 제목이 겹치지 않게 한다. chunk_id가 제목에서 나오므로
    # (schemas._build_chunk_id) 제목이 같으면 앞서 저장한 문서를 덮어쓴다.
    title = f"카카오워크 대화: {_headline(chat_log)} ({stamped})"
    document = kakao_service.build_document(
        text=chat_log,
        title=title,
        created_at=stamped,
        submitted_by=user_id,
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
