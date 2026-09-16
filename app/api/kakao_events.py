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
from app.services import claude_service, kakao_service, kakao_upload, notion_service
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
    # 두므로 여기서 "ask_question" / "reserve_schedule"이 그대로 잡힌다.
    action_name = _dig(payload, "value", "action_name")
    logger.info("Request URL 호출: action=%s", action_name)

    if action_name == kakao_service.BUTTON_RESERVE:
        return kakao_service.reserve_modal(projects=await _reserve_projects())
    return kakao_service.question_modal()


async def _reserve_projects() -> tuple[str, ...]:
    """예약 모달의 프로젝트 선택 상자에 넣을 목록.

    버튼을 누를 때마다 노션에서 읽는다(force). 모달은 그때그때 만들어 보내는
    JSON이라, 노션에 프로젝트가 늘면 선택 상자도 함께 늘어난다.
    조회가 실패하면 마지막으로 읽어 둔 목록으로 모달을 띄운다 - 선택지를 못
    읽었다고 예약 자체를 막지 않는다.
    """
    try:
        choices = await notion_service.calendar_choices(force=True)
    except Exception:
        logger.warning("프로젝트 선택지를 읽지 못했습니다.", exc_info=True)
        return ()
    return tuple(choices.get(notion_service.PROP_PROJECT, ()))


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

    if not inputs:
        # 모달을 거치지 않은 단순 버튼 클릭. 지금은 기록만 한다.
        return {"status": "ignored"}

    if not user_id:
        logger.error("사용자를 특정하지 못해 답장할 수 없습니다. 원본: %s", payload)
        return {"status": "no_user"}

    question = inputs.get(kakao_service.FIELD_QUESTION, "").strip()
    reserve_text = inputs.get(kakao_service.FIELD_RESERVE, "").strip()

    if question:
        background_tasks.add_task(_handle_question, user_id, question)
    elif reserve_text:
        # 등록 결과 DM이 실패하면 모달을 띄운 방으로 대신 보낸다.
        conversation_id = _dig(payload, "message.conversation_id", "conversation_id")
        # 사용자가 고른 프로젝트. 고르지 않았으면 빈 문자열이고, 그때는 문장에서
        # 모델이 판단한 값을 쓴다.
        project = inputs.get(kakao_service.FIELD_PROJECT, "").strip()
        background_tasks.add_task(
            _handle_reserve, user_id, reserve_text, conversation_id, project
        )
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
# 노션에 유형·프로젝트명을 추가한 뒤 10분을 기다리지 않고 바로 쓰고 싶을 때.
CMD_REFRESH = {"새로고침", "refresh"}


async def _handle_plain_message(
    payload: dict[str, Any],
    text: str,
    conversation_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """봇을 호출한 메시지를 처리한다.

        "메뉴" / "도움말"  → [질문하기]·[예약하기] 버튼 메뉴를 띄운다.
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

    if text.strip() in CMD_REFRESH:
        background_tasks.add_task(_refresh_choices, conversation_id)
        return {"status": "refresh"}

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
        "'메뉴'를 입력하면 질문하기·예약하기 버튼을 띄웁니다. "
        "바로 저장하려면 '저장 <내용>'을 보내주세요.",
    )
    return {"status": "hint"}


async def _refresh_choices(conversation_id: str) -> None:
    """노션의 유형·프로젝트명 선택지를 지금 다시 읽는다.

    평소에는 10분마다 저절로 반영되지만, 노션에 항목을 추가하고 바로
    예약해야 할 때 쓴다.
    """
    notion_service.clear_choices_cache()
    try:
        choices = await notion_service.calendar_choices(force=True)
    except Exception:
        logger.exception("선택지 새로고침 실패")
        await _reply_to_room(conversation_id, "선택지를 새로 읽지 못했습니다.")
        return

    await _reply_to_room(
        conversation_id,
        "노션 선택지를 새로 읽었습니다.\n"
        f" · 유형 {len(choices.get(notion_service.PROP_TYPE, ()))}개\n"
        f" · 프로젝트명 {len(choices.get(notion_service.PROP_PROJECT, ()))}개",
    )


async def _show_menu(conversation_id: str) -> None:
    """[질문하기]·[예약하기] 버튼이 담긴 업무 도우미 메뉴를 방에 보낸다."""
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
    """질문 → Claude가 기간 분류 → 노션 캘린더 조회 → 답변 DM 발송."""
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


async def _handle_reserve(
    user_id: str, text: str, conversation_id: str = "", project: str = ""
) -> None:
    """[예약하기] 모달에 적은 한 줄을 노션 캘린더에 등록한다.

    사용자는 "9월 15일 3시 킥오프 회의 본관 3층 대회의실"처럼 한 줄만 적는다.
    거기서 이벤트명·날짜·시간·장소를 갈라내는 것은 Claude가 한다(Tool Use로
    스키마를 고정해 받는다). 규칙 기반으로는 한국어 어순·조사 때문에 장소와
    참석자를 가르기가 사실상 불가능하다.

    결과는 성공이든 실패든 DM으로 알린다. 카카오워크는 모달을 연달아 띄울 수
    없어 등록 전에 확인 화면을 만들 수 없기 때문이다. 대신 등록된 내용과
    노션 링크를 보내 틀린 경우 노션에서 고치게 한다.
    """
    today = kakao_service.now_kst()[:10]
    lines = await _register_notion_events(text, today, project)
    await _reply(user_id, conversation_id, "\n".join(lines))


async def _find_conflicts(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    """등록할 일정과 참석자·시간이 겹치는 기존 일정. 확인하지 못하면 None.

    겹침 확인은 부가 기능이다. 노션 조회가 실패해도 등록은 계속한다.
    """
    if not notion_service.attendee_names(event.get("attendees", "")):
        return []  # 비교할 참석자가 없으면 조회할 필요도 없다
    start = event.get("date", "")
    end = event.get("end_date") or start
    try:
        existing = await notion_service.query_calendar_events(start, end)
    except Exception:
        logger.warning("겹침 확인 실패 (date=%s)", start, exc_info=True)
        return None
    return notion_service.find_conflicts(event, existing)


def _overlap_lines(event: dict[str, Any], conflicts: list[dict[str, Any]] | None) -> list[str]:
    """겹침 알림 줄. 확인하지 못했으면 그 사실을 적는다."""
    name = event.get("name", "")
    if conflicts is None:
        return [f" · {name}: 겹치는 일정을 확인하지 못했습니다"]

    lines: list[str] = []
    for conflict in conflicts:
        other = conflict["event"]
        when = other.get("date", "")
        when = f"{when}({notion_service.weekday_label(when)})" if when else ""
        if other.get("end_date") and other["end_date"] != other.get("date"):
            when += f" ~ {other['end_date']}"
        time = other.get("time") or "시간 미기재"
        shared = ", ".join(conflict["shared"])
        lines.append(f" · {name} ↔ {other.get('name', '')} / {when} {time} / 겹치는 참석자 {shared}")
        if other.get("url"):
            lines.append(f"   {other['url']}")
    return lines


async def _register_notion_events(
    chat_log: str, today: str, project: str = ""
) -> list[str]:
    """자유 문장에서 일정을 뽑아 노션 캘린더에 등록하고, 결과 문구를 돌려준다.

    Args:
        chat_log: 사용자가 모달에 적은 문장
        today: 오늘 날짜(YYYY-MM-DD). "다음주 화요일" 계산 기준.
        project: 모달에서 고른 프로젝트. 고르지 않았으면 빈 문자열이고,
            그때는 프로젝트명을 비워 둔다(문장에서 짐작하지 않는다).

    Returns:
        사용자에게 보여줄 결과 줄 목록. 예외를 밖으로 던지지 않는다.
        백그라운드 작업이라 여기서 터지면 사용자는 아무 응답도 못 받는다.

    모델이 YYYY-MM-DD를 지키지 않는 경우가 있으므로 create_calendar_event가
    형식을 한 번 더 검증한다. 형식이 틀린 건만 건너뛰고 나머지는 등록한다.
    """
    # 유형·프로젝트명 선택지는 노션에서 읽는다(10분 캐시). 노션에 항목이
    # 늘어도 코드를 고치지 않게 하려는 것이다. 읽지 못하면 기본값으로 돈다.
    choices = await notion_service.calendar_choices()

    try:
        events = await claude_service.extract_schedule_events(chat_log, today, choices)
    except Exception:
        logger.exception("일정 추출 실패")
        return ["등록에 실패했습니다. (일정을 읽어내지 못했습니다)"]

    if not events:
        return [
            "일정을 알아보지 못했습니다.",
            "날짜를 포함해서 다시 적어주세요. 예) 9월 15일 3시 킥오프 회의",
        ]

    registered: list[str] = []
    skipped: list[str] = []
    overlaps: list[str] = []

    for event in events:
        try:
            # 프로젝트는 모달에서 고른 값만 쓴다. 고르지 않았으면 비워 둔다
            # (사용자 결정). 문장에서 짐작한 값으로 채우면 틀렸을 때 그대로 남는다.
            event = {**event, "project": project}
            # 날짜 계산은 여기서 한다. 모델은 "다음 주 화요일"을 분류만 했다.
            event = notion_service.prepare_reserved_event(event, today, choices)
            # 겹침은 등록 **전에** 조회한다. 등록 뒤에 보면 방금 넣은 일정이
            # 자기 자신과 겹친다고 나온다. 겹쳐도 등록은 한다(알림만).
            conflicts = await _find_conflicts(event)
            url = await notion_service.create_calendar_event(event)
        except notion_service.NotionWriteError as exc:
            logger.warning("노션 일정 등록 건너뜀: %s", exc)
            skipped.append(f"{event.get('name', '(이름 없음)')} — {exc}")
            continue
        except Exception:
            logger.exception("노션 일정 등록 실패")
            skipped.append(f"{event.get('name', '(이름 없음)')} — 등록 중 오류")
            continue

        # 요일과 참석자를 함께 보여 준다. 날짜가 틀려도 사용자가 DM만 보고
        # 알아챌 수 있게 하는 안전장치다("다음 주 화요일"인데 (수)가 찍히면 보인다).
        start = event.get("date", "")
        when = f"{start}({notion_service.weekday_label(start)})" if start else ""
        if event.get("end_date"):
            end = event["end_date"]
            when += f" ~ {end}({notion_service.weekday_label(end)})"
        if start and start < today:
            when += " · 지난 날짜"
        attendees = event.get("attendees", "")
        detail = " / ".join(
            part
            for part in (
                when,
                event.get("time", ""),
                event.get("place", ""),
                f"참석 {attendees}" if attendees else "",
                # 프로젝트를 잘못 고르면 여기서 보인다(날짜 요일 표시와 같은 장치).
                event.get("project", ""),
            )
            if part
        )
        registered.append(f" · {event.get('name', '')} / {detail}")
        overlaps.extend(_overlap_lines(event, conflicts))
        if url and len(registered) == 1:
            # 링크는 하나만 붙인다. 여러 건이어도 같은 캘린더라 한 번이면 된다.
            registered.append(f"   {url}")

    lines: list[str] = []
    if registered:
        count = len([item for item in registered if item.startswith(" · ")])
        lines.append(f"노션 캘린더에 {count}건을 등록했습니다.")
        lines.extend(registered)
    if overlaps:
        lines.append("")
        lines.append("⚠ 참석자 일정이 겹칩니다 (등록은 완료됨):")
        lines.extend(overlaps)
    if skipped:
        lines.append(f"등록하지 못한 일정 {len(skipped)}건:")
        lines.extend(f" · {item}" for item in skipped)
    return lines


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
