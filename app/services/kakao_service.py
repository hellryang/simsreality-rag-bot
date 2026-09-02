"""KakaoWork 데이터 수집·발송.

Notion·Slack·KakaoWork는 각각 독립적으로 직접 벡터 DB에 적재된다.
다른 소스의 중간 경유지로 사용하지 않는다.

--- 수집에 대한 중요한 제약 (공식 문서 확인 결과) ------------------

KakaoWork Web API에는 **대화 내용을 읽어오는 엔드포인트가 없다.**
users.* / conversations.* / messages.send* 만 있고 messages.list 류가 없다.
Callback URL도 "메시지 수신"이 아니라 버튼 클릭·모달 제출만 전달한다.
파일 업로드/다운로드 API도 없다.

따라서 요구사항의 "채팅방 대화 읽기", "주고받는 파일 읽기"를 봇이 자동으로
수행할 수는 없다. 대신 **사용자가 봇에게 명시적으로 제출한 내용**
(모달에 붙여넣은 대화, 업로드 페이지로 올린 파일)을 KakaoWork 소스의
문서로 적재한다. 이것도 KakaoWork에서 출발한 데이터이므로 세 소스 독립
적재 원칙에 어긋나지 않는다.

근거 문서:
  https://docs.kakaoi.ai/kakao_work/webapireference/
  https://docs.kakaoi.ai/kakao_work/botdevguide/bot_dev/
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.core.security import mask_secret
from app.models.schemas import Chunk, Document
from app.services.embedder import chunk_documents
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

BASE_URL = "https://api.kakaowork.com/v1"
_MAX_RETRY = 4
_TIMEOUT_SEC = 10.0

# 저장 시각은 한국 시간(KST)으로 남긴다. UTC로 남기면 사용자가 보는 시각과
# 9시간 어긋나 혼란스럽다(예: 21시에 저장했는데 12시로 표시).
KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    """지금을 KST ISO 문자열로. 저장 시각 표기를 한 곳에서 통일한다."""
    return datetime.now(KST).isoformat(timespec="seconds")

# 버튼을 눌렀을 때 무슨 일이 일어나는지
ACTION_CALL_MODAL = "call_modal"  # Request URL로 모달 JSON을 요청
ACTION_SUBMIT = "submit_action"  # Callback URL로 값 전달
ACTION_OPEN_BROWSER = "open_system_browser"

# 모달 입력칸 이름. 콜백에서 이 이름으로 값을 찾는다.
FIELD_QUESTION = "question"
FIELD_CHAT_LOG = "chat_log"
# "내 저장 관리" 모달에서 삭제할 문서를 고르는 Select 칸 이름.
FIELD_DELETE_TARGET = "delete_target"

# 버튼 식별자. 버튼의 action.value로 나갔다가 request_modal 페이로드의
# value로 되돌아온다. 세 군데(버튼 생성·모달 응답·라우팅)에서 같은 값을
# 써야 하므로 상수로 묶는다.
BUTTON_ASK = "ask_question"
BUTTON_CHAT_LOG = "submit_chat_log"
BUTTON_UPLOAD = "upload_file"
BUTTON_MANAGE = "manage_docs"  # [내 저장 조회] — 목록 Select 모달을 연다
# 상세 메시지의 [삭제] 버튼. value에 "delete:<chunk_id>"를 실어 어떤 글을
# 지울지 알려준다. submit_action이라 누르면 콜백으로 value가 온다.
ACTION_DELETE_PREFIX = "delete:"


class KakaoWorkError(RuntimeError):
    """API가 success: false를 돌려줬을 때."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# --- 저수준 호출 -----------------------------------------------------


async def _call(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """KakaoWork Web API 호출 + 지수 백오프 재시도.

    지수 백오프는 실패할 때마다 대기 시간을 2배씩 늘려가며 다시 시도하는
    방식이다. 429(요청 과다)나 5xx는 곧바로 재시도하면 또 막히기 때문에
    간격을 벌린다.

    모든 응답은 {"success": true, ...} / {"success": false, "error": {...}}
    봉투에 싸여 오므로 여기서 벗겨서 돌려준다.
    """
    headers = {
        "Authorization": f"Bearer {settings.kakaowork_app_key}",
        "Content-Type": "application/json",
    }
    delay = 1.0

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=_TIMEOUT_SEC) as client:
        for attempt in range(1, _MAX_RETRY + 1):
            try:
                response = await client.request(
                    method, path, headers=headers, json=json, params=params
                )
            except httpx.RequestError as exc:
                if attempt == _MAX_RETRY:
                    logger.error("KakaoWork 연결 실패: %s", exc)
                    raise KakaoWorkError("connection_error", str(exc)) from exc
                logger.warning("KakaoWork 연결 실패. %.1f초 후 재시도 %d/%d", delay, attempt, _MAX_RETRY)
                await asyncio.sleep(delay)
                delay *= 2
                continue

            # 401/403은 몇 번을 다시 보내도 결과가 같으므로 즉시 중단한다.
            if response.status_code in (401, 403):
                logger.error(
                    "KakaoWork 인증 거부 (status=%s). App Key(%s)가 맞는지, "
                    "봇이 활성 상태인지 관리자에서 확인하세요.",
                    response.status_code,
                    mask_secret(settings.kakaowork_app_key),
                )
                raise KakaoWorkError("invalid_authentication", "App Key가 거부되었습니다.")

            # 분당 호출 한도를 넘기면 429가 오고 retry-after 헤더가 붙는다.
            if response.status_code == 429 and attempt < _MAX_RETRY:
                wait = float(response.headers.get("retry-after", delay))
                logger.warning("KakaoWork rate limit. %.1f초 후 재시도 %d/%d", wait, attempt, _MAX_RETRY)
                await asyncio.sleep(wait)
                delay *= 2
                continue

            if response.status_code >= 500 and attempt < _MAX_RETRY:
                logger.warning(
                    "KakaoWork 서버 오류 (status=%s). %.1f초 후 재시도 %d/%d",
                    response.status_code, delay, attempt, _MAX_RETRY,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue

            response.raise_for_status()
            payload = response.json()
            if not payload.get("success", False):
                error = payload.get("error") or {}
                raise KakaoWorkError(
                    error.get("code", "unknown"),
                    error.get("message", "알 수 없는 오류"),
                )
            return payload

    raise RuntimeError("도달할 수 없는 분기")  # 방어용


# --- 조회 -----------------------------------------------------------


async def get_bot_info() -> dict[str, Any]:
    """App Key가 살아 있는지 확인하는 용도로 쓰기 좋다."""
    return (await _call("GET", "/bots.info"))["info"]


async def find_user_by_email(email: str) -> dict[str, Any]:
    """이메일로 멤버를 찾는다. 업로드 링크를 만들려면 user id가 필요하다."""
    return (await _call("GET", "/users.find_by_email", params={"email": email}))["user"]


# 콜백에는 발신자 이름이 없고 user_id(숫자)만 온다. 저장할 때 사람이 읽는
# 이름을 붙이려면 users.info로 조회해야 한다. 같은 사람이 연달아 말하는
# 일이 잦으므로 조회 결과를 캐시해 API 호출을 줄인다.
_user_name_cache: dict[str, str] = {}


async def get_user_name(user_id: str | int) -> str:
    """user_id로 표시 이름을 얻는다. 실패하면 user_id 문자열을 그대로 쓴다.

    이름 조회 실패로 저장 자체를 막지는 않는다. 이름이 없어도 대화 내용은
    남기는 편이 낫다.
    """
    uid = str(user_id)
    if uid in _user_name_cache:
        return _user_name_cache[uid]
    try:
        user = (await _call("GET", "/users.info", params={"user_id": uid}))["user"]
        name = str(user.get("name") or user.get("nickname") or uid)
    except (KakaoWorkError, KeyError):
        logger.warning("사용자 이름 조회 실패: user_id=%s", uid)
        name = uid
    _user_name_cache[uid] = name
    return name


# 방 이름도 자주 반복되므로 캐시한다. 이름을 못 얻으면(안 지은 방 등)
# 방 번호를 그대로 쓴다.
_room_name_cache: dict[str, str] = {}


async def get_room_name(conversation_id: str | int) -> str:
    """conversation_id로 방 이름을 얻는다. 없으면 번호를 그대로 돌려준다.

    conversations.list에서 그 방을 찾아 name을 쓴다. 이름을 안 지은 그룹방은
    name이 비어 있으므로 번호로 대신한다. 관리자 페이지가 생기면 사람이
    읽는 라벨로 덮어쓴다.
    """
    cid = str(conversation_id)
    if cid in _room_name_cache:
        return _room_name_cache[cid]
    try:
        result = await _call("GET", "/conversations.list")
        for room in result.get("conversations", []):
            if str(room.get("id")) == cid and room.get("name"):
                _room_name_cache[cid] = str(room["name"])
                return _room_name_cache[cid]
    except KakaoWorkError:
        logger.warning("방 이름 조회 실패: conversation_id=%s", cid)
    _room_name_cache[cid] = cid  # 이름을 못 얻으면 번호로 대신
    return cid


async def open_conversation(user_id: str | int) -> dict[str, Any]:
    """봇과 해당 멤버의 1:1 대화방을 연다(이미 있으면 기존 방)."""
    return (await _call("POST", "/conversations.open", json={"user_id": str(user_id)}))[
        "conversation"
    ]


# --- 발송 -----------------------------------------------------------


async def send_message(
    conversation_id: str | int,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """대화방에 메시지를 보낸다.

    text는 알림 미리보기와 블록을 못 그리는 환경의 대체 문구로 쓰이므로
    blocks를 넣더라도 함께 채운다.
    """
    body: dict[str, Any] = {"conversation_id": str(conversation_id), "text": text}
    if blocks:
        body["blocks"] = blocks
    return (await _call("POST", "/messages.send", json=body))["message"]


async def send_message_by_email(
    email: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """대화방을 따로 열지 않고 이메일로 바로 보낸다."""
    body: dict[str, Any] = {"email": email, "text": text}
    if blocks:
        body["blocks"] = blocks
    return (await _call("POST", "/messages.send_by_email", json=body))["message"]


async def reply_to_user(user_id: str, text: str) -> None:
    """콜백에는 답을 실을 수 없으므로 따로 메시지를 보낸다.

    답장 실패로 요청 전체를 실패시키지 않는다. 이미 사용자의 질문은
    처리됐고, 여기서 예외를 올리면 카카오워크가 콜백을 재전송한다.
    """
    try:
        conversation = await open_conversation(user_id)
        await send_message(conversation["id"], text)
    except KakaoWorkError as exc:
        logger.error("답장 전송 실패 (user=%s): %s", user_id, exc)


# --- Block Kit 조립 --------------------------------------------------


def _text_block(content: str) -> dict[str, Any]:
    """말풍선용 텍스트. 모달 안에서는 쓸 수 없다(_label_block을 쓴다)."""
    return {"type": "text", "text": content, "markdown": True}


def _label_block(content: str) -> dict[str, Any]:
    """모달용 설명 문구. 최대 200자.

    모달에는 말풍선용 블록(text/header/divider/action…)을 넣을 수 없다.
    Label/Input/Select 세 가지만 허용된다. text 블록을 넣으면 모달을
    불러오는 단계에서 실패한다.

    사양: https://docs.kakaoi.ai/kakao_work/blockkit/labelblock/
    """
    return {"type": "label", "text": content, "markdown": False}


def _button(
    label: str, action_type: str, action_name: str = "", value: str = "", style: str = "default"
) -> dict[str, Any]:
    """버튼 블록.

    동작 정보는 평평하게 두면 안 되고 `action` 객체 안에 중첩해야 한다.
    평평한 형태(action_type/action_name)로 보내면 messages.send가
    [invalid_parameter] "요청한 블록 정보가 올바르지 않습니다"로 거부한다.
    실물 API 응답으로 확인했다.

    `value`는 필수다. 비어 있으면 action_name으로 채운다. 카카오워크는
    어떤 버튼이 눌렸는지를 페이로드의 `value`로 알려주므로(실물 확인),
    이렇게 해두면 action_name이 그대로 되돌아온다.

    label은 20자까지, 한 action 블록에 버튼은 2~3개까지다.

    사양: https://docs.kakaoi.ai/kakao_work/blockkit/buttonblock/
    """
    action: dict[str, Any] = {"type": action_type, "value": value or action_name}
    if action_name:
        action["name"] = action_name
    return {"type": "button", "text": label, "style": style, "action": action}


def welcome_blocks(upload_url: str = "") -> tuple[str, list[dict[str, Any]]]:
    """봇이 먼저 보내는 안내 메시지.

    KakaoWork 봇은 사용자가 채팅창에 그냥 친 문장을 받을 수 없다.
    자유 문장은 [질문하기] → 모달 입력칸을 거쳐야 들어온다.
    파일은 API 자체가 없어 [파일 올리기] → 웹 업로드로 받는다.
    """
    buttons = [
        _button("질문하기", ACTION_CALL_MODAL, action_name=BUTTON_ASK, style="primary"),
        _button("대화 정리 요청", ACTION_CALL_MODAL, action_name=BUTTON_CHAT_LOG),
        _button("내 저장 조회", ACTION_CALL_MODAL, action_name=BUTTON_MANAGE),
    ]
    body = (
        "무엇이든 물어보세요. Notion·Slack·KakaoWork에 쌓인 문서에서 찾아 "
        "출처와 함께 답해드립니다."
    )

    if upload_url:
        buttons.append(
            _button("파일 올리기", ACTION_OPEN_BROWSER, action_name=BUTTON_UPLOAD, value=upload_url)
        )
        body += "\n문서를 학습시키려면 [파일 올리기]를 눌러주세요."

    blocks = [
        {"type": "header", "text": "업무 도우미", "style": "blue"},
        _text_block(body),
        {"type": "divider"},
        {"type": "action", "elements": buttons},
    ]
    return "업무 도우미", blocks


def question_modal(value: str = BUTTON_ASK) -> dict[str, Any]:
    """[질문하기] 버튼을 눌렀을 때 Request URL이 돌려줄 모달.

    `value`는 **필수**다. 빠뜨리면 200을 돌려줘도 카카오워크가 모달 생성을
    포기하고 "불러오는데 실패"로 끝난다. 여기 담은 값은 사용자가 모달을
    제출할 때 콜백 페이로드의 value로 되돌아온다.
    """
    return {
        "view": {
            "title": "질문 입력",
            "accept": "질문하기",
            "decline": "취소",
            "value": value,
            "blocks": [
                _label_block("궁금한 내용을 적어주세요."),
                {
                    "type": "input",
                    "name": FIELD_QUESTION,
                    "required": True,
                    "placeholder": "예) 이번 달 배포 일정 알려줘",
                },
            ],
        }
    }


def chat_log_modal(value: str = BUTTON_CHAT_LOG) -> dict[str, Any]:
    """대화 내용을 붙여넣어 문서로 남기는 모달.

    봇이 채팅방을 읽을 수 없으므로, 남기고 싶은 대화는 사용자가 복사해
    넣는다. 카카오워크에 대화 캡처·복사 기능이 있어 조작 자체는 간단하다.
    """
    return {
        "view": {
            "title": "대화 내용 저장",
            "accept": "저장하기",
            "decline": "취소",
            "value": value,
            "blocks": [
                _label_block("저장할 대화 내용을 붙여넣어 주세요. 나중에 검색됩니다."),
                {
                    "type": "input",
                    "name": FIELD_CHAT_LOG,
                    "required": True,
                    "placeholder": "채팅방에서 복사한 대화 내용",
                },
            ],
        }
    }


# Select는 최대 30개까지. 그보다 많이 저장한 사람은 최근 것만 보여준다.
MANAGE_LIST_LIMIT = 25


def manage_docs_modal(items: list[dict[str, Any]], value: str = BUTTON_MANAGE) -> dict[str, Any]:
    """내가 저장한 글 목록에서 하나를 고르는 모달(조회 1단계).

    카카오워크는 모달→모달 연결이 안 되므로, 여기서 고른 글의 상세는 모달이
    아니라 봇 메시지로 보여준다(내용 전체 + [삭제] 버튼). 그 편이 긴 내용도
    잘리지 않아 오히려 낫다.

    저장한 글이 없으면 Select 대신 안내만 보여준다(Select는 옵션이 비면
    카카오워크가 거부한다).
    """
    if not items:
        return {
            "view": {
                "title": "내 저장 조회",
                "accept": "확인",
                "decline": "닫기",
                "value": value,
                "blocks": [_label_block("이 방에 저장한 글이 아직 없습니다.")],
            }
        }

    options = []
    for i, item in enumerate(items[:MANAGE_LIST_LIMIT]):
        # 목록 라벨: 내용 (날짜). 같은 방에서 저장한 것만 보여주므로 방 이름은
        # 붙이지 않는다. 내용이 잘리지 않게 앞부분을 넉넉히 두고 날짜는 분까지만.
        preview = " ".join(item["text"].split())[:40]
        date = item.get("msg_date", "")[:16].replace("T", " ")
        label = f"{preview} ({date})" if date else preview
        # value에는 순번(문자열)만 넣는다. 카카오워크가 긴 value를 거부할 수
        # 있어, 상세를 보여줄 때 순번으로 목록을 다시 조회해 그 글을 찾는다.
        options.append({"text": label[:100], "value": str(i)})

    return {
        "view": {
            "title": "내 저장 조회",
            "accept": "내용 보기",
            "decline": "취소",
            "value": value,
            "blocks": [
                _label_block("내용을 볼 글을 선택하세요."),
                {
                    "type": "select",
                    "name": FIELD_DELETE_TARGET,
                    "required": True,
                    "options": options,
                    "placeholder": "글을 선택",
                },
            ],
        }
    }


def doc_detail_blocks(item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """조회에서 고른 글의 상세를 보여주는 메시지(모달 아님).

    모달→모달 연결이 안 되므로 상세는 봇 메시지로 보낸다. 메시지는 길이
    제한이 덜해 내용 전체를 잘림 없이 보여줄 수 있고, [삭제] 버튼을 함께
    붙일 수 있다. 버튼 value에 chunk_id를 실어 어떤 글을 지울지 알린다.
    """
    room = item.get("room_label", "")
    date = item.get("msg_date", "")[:16].replace("T", " ")
    meta = " · ".join(part for part in (room, date, item.get("sender", "")) if part)

    body = f"📄 저장된 글\n{meta}\n\n{item['text']}"
    blocks = [
        {"type": "header", "text": "저장된 글", "style": "blue"},
        _text_block(f"{meta}\n\n{item['text']}"),
        {"type": "divider"},
        {
            # 카카오워크는 action 블록에 버튼이 최소 2개 있어야 한다(실측).
            # 삭제 옆에 닫기를 둔다. 닫기는 아무 동작 없이 넘어간다.
            "type": "action",
            "elements": [
                _button(
                    "삭제",
                    ACTION_SUBMIT,
                    action_name="delete_doc",
                    value=f"{ACTION_DELETE_PREFIX}{item['chunk_id']}",
                    style="danger",
                ),
                _button(
                    "닫기",
                    ACTION_SUBMIT,
                    action_name="close",
                    value="close",
                ),
            ],
        },
    ]
    return body, blocks


# --- 수집 (사용자가 제출한 내용을 문서로) ----------------------------


def build_document(
    text: str,
    title: str,
    created_at: str = "",
    submitted_by: str = "",
    room_label: str = "",
    sender: str = "",
    msg_date: str = "",
) -> Document:
    """KakaoWork에서 온 내용을 Document로 만든다.

    source를 "kakaowork"로 고정한다. 이 값이 있어야 답변에 "카카오워크에서
    가져온 내용"이라는 출처가 붙는다.

    sender/msg_date는 메시지 한 건이 한 문서일 때(xlsx 적재)만 채운다.
    출처 표시에서 "어느 방에서 언제 누가"를 파싱 없이 보여주기 위한 것.

    url이 비어 있는 이유: KakaoWork에는 메시지·파일을 가리키는 공개 링크가
    없다. schemas._build_chunk_id는 url이 없으면 title로 식별하므로,
    title이 제출 건마다 달라지도록 호출부에서 시각·파일명을 넣어 준다.
    """
    return Document(
        text=text,
        source="kakaowork",
        url="",
        title=title,
        created_at=created_at or now_kst(),
        submitted_by=submitted_by,
        room_label=room_label,
        sender=sender,
        msg_date=msg_date,
    )


def ingest_documents(documents: list[Document], persist_dir: str | None = None) -> int:
    """문서를 조각내어 벡터 DB에 넣는다. 넣은 조각 수를 돌려준다.

    임베딩 계산은 CPU를 쓰는 동기 작업이라 async로 만들지 않았다.
    FastAPI에서는 BackgroundTasks로 넘기면 스레드풀에서 돌아간다.
    """
    if not documents:
        return 0

    chunks: list[Chunk] = chunk_documents(documents)
    VectorStore(persist_dir=persist_dir).add(chunks)
    logger.info("KakaoWork 문서 %d건 → 조각 %d개 적재", len(documents), len(chunks))
    return len(chunks)


async def collect_kakao_documents() -> list[Document]:
    """수집 인터페이스 (build_db.py의 collect_all에서 부르는 자리).

    항상 빈 목록을 돌려준다. KakaoWork에는 대화·파일을 읽어오는 API가
    없어서 배치 수집으로 가져올 것이 없기 때문이다. KakaoWork 소스의
    문서는 사용자가 봇에 제출하는 시점에 ingest_documents()로 들어간다.

    build_db.py에서 asyncio.gather에 나란히 붙여도 깨지지 않도록 빈
    목록을 돌려주는 형태만 맞춰 둔다.
    """
    logger.info(
        "KakaoWork는 배치 수집 대상이 아닙니다. "
        "대화·파일 조회 API가 없어 사용자가 봇에 제출한 내용만 적재됩니다."
    )
    return []
