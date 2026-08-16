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


def _verify(token: str | None) -> None:
    """콜백 토큰 검사.

    KakaoWork가 요청에 서명을 붙이는지, 붙인다면 헤더 이름이 무엇인지는
    공식 문서에서 확인하지 못했다(추측해서 단정하지 않는다). 그래서 Slack의
    signing secret 검증에 해당하는 자리를 공유 비밀 헤더로 대신한다.

    관리자 화면에서 커스텀 헤더를 넣을 수 없다면 URL에 비밀 경로를 섞는
    방식으로 바꾼다. 콜백은 공개망에 열리므로 무방비로 두면 안 된다.
    """
    expected = settings.kakaowork_callback_token
    if expected and token != expected:
        logger.warning("콜백 토큰 불일치. 요청을 거부합니다.")
        raise HTTPException(status_code=401, detail="invalid callback token")


# --- 콜백 페이로드 해석 ----------------------------------------------


def _dig(payload: dict[str, Any], *keys: str) -> str:
    """여러 후보 키를 순서대로 찾아 첫 번째로 잡히는 값을 문자열로.

    KakaoWork는 콜백 페이로드의 전체 필드를 공개 문서에 싣지 않는다.
    스키마를 못 박는 대신 흔한 위치를 훑고, 원본은 로그로 남긴다.
    실제 페이로드를 한 번 받아 본 뒤 이 후보 목록을 정리하면 된다.
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
    """모달 입력값을 {이름: 값}으로 모은다."""
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
    x_callback_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """버튼(action_type=call_modal)을 누르면 카카오워크가 여기를 호출한다.

    응답으로 준 모달이 사용자 화면에 뜨고, 거기 입력한 값이
    /kakao/callback으로 넘어온다.
    """
    _verify(x_callback_token)
    action_name = _dig(payload, "action_name", "value", "type")
    logger.info("Request URL 호출: action=%s", action_name)

    if action_name == "submit_chat_log":
        return kakao_service.chat_log_modal()
    return kakao_service.question_modal()


# --- Callback URL ----------------------------------------------------


@router.post("/callback")
async def callback_url(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    x_callback_token: str | None = Header(default=None),
) -> dict[str, str]:
    """버튼 클릭 결과와 모달 제출 결과가 들어온다.

    Claude 호출과 임베딩은 수 초가 걸리므로 여기서 기다리지 않는다.
    """
    _verify(x_callback_token)

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


async def _handle_chat_log(user_id: str, chat_log: str) -> None:
    """붙여넣은 대화 내용을 KakaoWork 소스 문서로 적재한다."""
    stamped = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = kakao_service.build_document(
        text=chat_log,
        title=f"카카오워크 대화 ({stamped})",
        created_at=stamped,
    )
    try:
        chunks = kakao_service.ingest_documents([document])
        await kakao_service.reply_to_user(
            user_id, f"대화 내용을 저장했습니다. ({chunks}개 조각) 이제 검색됩니다."
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
    document = kakao_service.build_document(text=text, title=filename)
    try:
        chunks = kakao_service.ingest_documents([document])
        await kakao_service.reply_to_user(
            user_id, f"'{filename}' 처리가 끝났습니다. ({chunks}개 조각) 이제 검색됩니다."
        )
    except Exception:
        logger.exception("업로드 적재 실패 (user=%s, file=%s)", user_id, filename)
        await kakao_service.reply_to_user(user_id, f"'{filename}' 처리에 실패했습니다.")
