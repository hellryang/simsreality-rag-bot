"""Claude API 호출.

시스템 프롬프트에는 항상 "제공된 컨텍스트 범위 안에서만 답하고, 각 문장 뒤에
출처 번호를 붙이며, 근거가 없으면 '관련 문서를 찾지 못했습니다'라고 답한다"를
포함시킨다. 모델 문자열은 app.core.config.Settings에서만 관리한다.
"""
from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache

import anthropic

from app.core.config import settings
from app.models.schemas import NO_CONTEXT_ANSWER, Answer, Chunk, Citation, SearchHit

logger = logging.getLogger(__name__)

# 답변 하나에 이 정도면 충분하다. 너무 크게 잡으면 모델이 장황해지고 비용도 는다.
MAX_TOKENS = 2000

# 요약·인용처럼 사실을 옮기는 작업은 낮은 온도가 맞다. 높이면 없는 말을 지어낸다.
# (temperature는 Haiku 4.5에서 사용 가능하다. Opus 4.7 이후 모델에서는 제거되었으므로
#  상위 모델로 바꿀 때는 이 줄을 함께 확인해야 한다.)
TEMPERATURE = 0.3

_MAX_RETRY = 3


@lru_cache(maxsize=1)
def _get_client() -> anthropic.AsyncAnthropic:
    """비동기 클라이언트를 한 번만 만들어 재사용한다.

    import 시점이 아니라 처음 호출할 때 만든다. 그래야 키가 없는 환경에서도
    이 모듈을 import하는 것만으로는 터지지 않는다.
    """
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def build_system_prompt(today: str = "") -> str:
    """답변 규칙을 고정하는 시스템 프롬프트.

    시스템 프롬프트(system prompt)는 모델에게 미리 주는 역할·규칙 지시문이다.
    사용자 질문마다 반복해서 붙이지 않아도 매 호출에 적용된다.

    today를 주면 캘린더 도구를 쓸 수 있다는 규칙이 덧붙는다. 상대 표현
    ("다음주")을 날짜로 바꾸려면 오늘이 며칠인지 알아야 하기 때문이다.
    """
    prompt = (
        "너는 회사 내부 문서를 근거로만 답하는 사내 업무 도우미다.\n"
        "\n"
        "규칙:\n"
        "1. 아래 제공된 컨텍스트 안에 있는 내용만으로 답한다. "
        "컨텍스트에 없는 사실을 지식으로 채워 넣지 않는다.\n"
        "2. 답변의 각 문장 끝에 근거가 된 문서의 출처 번호를 [1], [2] 형태로 붙인다. "
        "한 문장이 여러 문서에 근거하면 [1][2]처럼 이어서 쓴다.\n"
        f"3. 컨텍스트에 근거가 없으면 지어내지 말고 정확히 "
        f"'{NO_CONTEXT_ANSWER}'라고만 답한다.\n"
        "4. 한국어로, 군더더기 없이 답한다."
    )

    if not today:
        return prompt

    return prompt + (
        "\n"
        "\n"
        f"오늘은 {today}이다. '내일', '다음주', '이번 달' 같은 상대 표현은 "
        "이 날짜를 기준으로 계산한다.\n"
        "\n"
        "일정 관련 규칙:\n"
        "5. 일정·회의·예약·빈 날에 대한 질문이면 lookup_calendar 도구로 "
        "노션 캘린더를 먼저 조회한다. 컨텍스트만 보고 추측하지 않는다.\n"
        "6. 도구가 돌려준 결과는 확인된 사실이므로 그대로 근거로 쓴다. "
        "이때는 출처 번호를 붙이지 않고, 규칙 3의 '근거 없음'에도 해당하지 않는다.\n"
        "7. 도구가 '일정이 없는 날'을 알려주면 그 목록을 그대로 전한다. "
        "날짜를 직접 계산하거나 빼거나 더하지 않는다.\n"
        "8. 도구가 조회에 실패했다고 하면 실패했다고 알린다. 임의로 답하지 않는다."
    )


def build_context(hits: list[SearchHit]) -> tuple[str, list[Citation]]:
    """검색 결과를 모델에 넣을 컨텍스트 문자열과 출처 목록으로 만든다.

    본문에 붙는 번호와 출처 목록의 번호가 반드시 같아야 한다.
    그래서 출처 목록을 먼저 만들고, 그 번호를 본문 블록에 되붙인다.
    같은 문서에서 조각이 여러 개 걸려도 번호는 하나로 합쳐진다.
    """
    chunks: list[Chunk] = [hit.chunk for hit in hits]
    citations = Citation.from_chunks(chunks)

    # (소스, 문서 식별자) → 출처 번호
    numbers = {(c.source, c.url or c.title): c.number for c in citations}

    blocks: list[str] = []
    for chunk in chunks:
        number = numbers[(chunk.source, chunk.url or chunk.title)]
        blocks.append(f"[{number}] {chunk.title} ({chunk.source})\n{chunk.text}")

    return "\n\n".join(blocks), citations


async def _request(**kwargs):
    """messages.create를 지수 백오프로 감싼 것. 세 호출부가 공유한다.

    SDK도 429·5xx를 자동으로 두 번 재시도하지만 그걸로도 안 되는 경우가
    있어 한 겹 더 감싼다. 401·404처럼 다시 보내도 결과가 같은 오류는
    즉시 중단한다.
    """
    delay = 1.0
    for attempt in range(1, _MAX_RETRY + 1):
        try:
            return await _get_client().messages.create(**kwargs)
        except anthropic.NotFoundError:
            # 대부분 모델 문자열 오타다. 재시도해도 똑같다.
            logger.error(
                "모델을 찾을 수 없습니다: %s — config.py의 anthropic_model을 확인하세요.",
                settings.anthropic_model,
            )
            raise
        except anthropic.AuthenticationError:
            logger.error("ANTHROPIC_API_KEY가 잘못되었습니다.")
            raise
        except (anthropic.RateLimitError, anthropic.APIStatusError,
                anthropic.APIConnectionError) as exc:
            if attempt == _MAX_RETRY:
                logger.error("Claude 호출이 %d회 모두 실패했습니다.", _MAX_RETRY)
                raise
            logger.warning(
                "Claude 호출 실패 (%s). %.1f초 후 재시도 %d/%d",
                type(exc).__name__, delay, attempt, _MAX_RETRY,
            )
            await asyncio.sleep(delay)
            delay *= 2

    raise RuntimeError("도달할 수 없는 분기")  # 방어용


def _text_of(response) -> str:
    """응답에서 text 블록만 이어붙인다. 거절이면 표준 문구를 돌려준다."""
    if response.stop_reason == "refusal":
        logger.warning("Claude가 답변을 거절했습니다.")
        return NO_CONTEXT_ANSWER
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


async def _create_message(system: str, user_content: str) -> str:
    """Claude를 호출하고 답변 텍스트만 꺼낸다."""
    response = await _request(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return _text_of(response)


# --- 캘린더 조회 도구 ------------------------------------------------
#
# "다음주 빈 날 알려줘" 같은 질문은 벡터 검색으로 풀 수 없다. 검색은 있는
# 것 중 비슷한 것을 top-k개 주는 도구인데, 빈 날은 `범위의 모든 날 −
# 일정이 있는 날`이라는 차집합이라 한쪽 집합이 전부 있어야 계산된다.
#
# 그래서 노션 캘린더를 날짜 범위로 직접 조회하는 도구를 모델에게 쥐여준다.
# 모델은 "언제부터 언제까지 볼지"만 정하고, **일정 수집과 빈 날 계산은
# 파이썬이 한다**(날짜 산술은 LLM이 가장 자주 틀리는 작업이다).

# 도구 호출이 무한히 이어지지 않도록 상한을 둔다.
MAX_TOOL_ROUNDS = 3

CALENDAR_TOOL: dict = {
    "name": "lookup_calendar",
    "description": (
        "노션 캘린더에서 지정한 기간의 일정과 '일정이 하나도 없는 날'을 조회한다. "
        "일정·회의·예약·빈 날·언제 시간이 되는지에 대한 질문이면 "
        "추측하지 말고 반드시 이 도구를 먼저 호출한다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {
                "type": "string",
                "description": "조회 시작일. YYYY-MM-DD 형식.",
            },
            "end_date": {
                "type": "string",
                "description": "조회 종료일(포함). YYYY-MM-DD 형식.",
            },
        },
        "required": ["start_date", "end_date"],
    },
}


async def _run_calendar_tool(tool_input: dict) -> str:
    """lookup_calendar 도구를 실제로 수행하고 결과를 문자열로 돌려준다.

    모델에게 돌려줄 값이므로 실패해도 예외를 올리지 않는다. 예외를 올리면
    대화가 끊겨 사용자는 아무 답도 못 받는다. 실패 사유를 글로 적어 주면
    모델이 그것을 근거로 사정을 설명할 수 있다.
    """
    from app.services import notion_service

    start = str(tool_input.get("start_date", "")).strip()
    end = str(tool_input.get("end_date", "")).strip()

    try:
        events = await notion_service.query_calendar_events(start, end)
        free_days = notion_service.compute_free_days(start, end, events)
    except notion_service.NotionWriteError as exc:
        return f"조회 실패: {exc}"
    except Exception:
        logger.exception("캘린더 조회 중 오류")
        return "조회 실패: 캘린더를 읽는 중 오류가 발생했습니다."

    lines = [f"조회 기간: {start} ~ {end}", f"일정 {len(events)}건"]
    for event in events:
        when = event["date"]
        if event.get("end_date") and event["end_date"] != event["date"]:
            when += f"~{event['end_date']}"
        detail = " / ".join(
            part
            for part in (event.get("time"), event.get("place"), event.get("type"))
            if part
        )
        lines.append(f"  - {when} {event['name']}{' / ' + detail if detail else ''}")

    if free_days:
        labels = ", ".join(f"{d['date']}({d['weekday']})" for d in free_days)
        lines.append(f"일정이 없는 날 {len(free_days)}일: {labels}")
    else:
        lines.append("일정이 없는 날: 없음 (모든 날에 일정이 있음)")

    return "\n".join(lines)


async def _answer_with_tools(system: str, user_content: str) -> str:
    """캘린더 도구를 쓸 수 있게 하고, 도구 호출이 끝날 때까지 이어서 부른다.

    Tool Use는 한 번에 끝나지 않는다. 모델이 도구를 쓰겠다고 하면(stop_reason
    == "tool_use") 우리가 실제로 수행한 뒤 그 결과를 tool_result로 다시 넣어
    호출해야 최종 답변이 나온다(CLAUDE.md 7장의 멀티턴 루프).
    """
    messages: list[dict] = [{"role": "user", "content": user_content}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = await _request(
            model=settings.anthropic_model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=system,
            tools=[CALENDAR_TOOL],
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _text_of(response)

        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("캘린더 도구 호출: %s", block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": await _run_calendar_tool(block.input),
                }
            )
        messages.append({"role": "user", "content": results})

    # 상한에 걸렸다. 도구 없이 한 번 더 불러 지금까지의 내용으로 답하게 한다.
    logger.warning("도구 호출이 %d회를 넘어 중단합니다.", MAX_TOOL_ROUNDS)
    response = await _request(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=system,
        messages=messages,
    )
    return _text_of(response)


async def answer_with_citations(
    question: str,
    hits: list[SearchHit],
    *,
    allow_calendar: bool = False,
    today: str = "",
) -> Answer:
    """검색된 조각을 근거로 출처가 붙은 답변을 만든다.

    Args:
        question: 사용자 질문
        hits: 벡터 검색 결과 (유사도 높은 순)
        allow_calendar: 노션 캘린더 조회 도구를 쓸 수 있게 할지.
            켜면 검색 결과가 없어도 모델을 부른다. "다음주 빈 날"처럼
            벡터 DB에 근거가 있을 수 없는 질문이 있기 때문이다.
        today: 오늘 날짜(YYYY-MM-DD). 상대 표현 계산 기준.

    Returns:
        본문과 출처 목록이 담긴 Answer.

    allow_calendar를 기본값 False로 둔 이유: 근거가 없는데 모델을 부르면
    지어낸 답이 나오고 돈도 나간다는 원칙(tests/test_contracts.py)을 그대로
    지키기 위해서다. 캘린더가 필요한 호출부만 명시적으로 켠다.
    """
    if not hits and not allow_calendar:
        logger.info("검색 결과가 없어 Claude를 호출하지 않습니다.")
        return Answer.no_context()

    if hits:
        context, citations = build_context(hits)
        user_content = (
            f"다음은 사내 문서에서 검색한 컨텍스트다.\n\n"
            f"{context}\n\n"
            f"---\n질문: {question}"
        )
    else:
        # 캘린더 질문은 벡터 DB에 근거가 없는 것이 정상이다.
        citations = []
        user_content = (
            f"사내 문서에서 검색된 컨텍스트가 없다.\n\n"
            f"---\n질문: {question}"
        )

    system = build_system_prompt(today)
    if allow_calendar:
        text = await _answer_with_tools(system, user_content)
    else:
        text = await _create_message(system, user_content)

    return _finalize(text, citations)


def _finalize(text: str, citations: list[Citation]) -> Answer:
    """모델 출력을 사용자에게 보여줄 답변으로 다듬는다.

    규칙 3은 근거가 없으면 NO_CONTEXT_ANSWER "라고만" 답하라고 하지만,
    모델이 그 문장을 말한 뒤 설명을 덧붙이는 일이 실제로 있다. 그대로 두면
    "관련 문서를 찾지 못했습니다"라고 선언하면서 출처를 4건 다는 모순된
    답변이 나간다.

    이때 뒤에 붙은 설명은 컨텍스트에 근거한 유용한 정보인 경우가 많다
    (예: "[1]에는 제목만 있고 본문이 없다"). 그래서 설명을 버리는 대신
    모순되는 첫 문장만 떼어내고 출처는 유지한다.

    프롬프트로 모델 출력을 못 박는 것은 신뢰할 수 없으므로 코드에서 막는다.
    """
    stripped = text.strip()

    if stripped == NO_CONTEXT_ANSWER:
        return Answer.no_context()

    if stripped.startswith(NO_CONTEXT_ANSWER):
        stripped = stripped[len(NO_CONTEXT_ANSWER) :].lstrip()
        # 문장부호만 남는 등 실질 내용이 없으면 근거 없음으로 처리한다.
        if not stripped:
            return Answer.no_context()

    stripped, citations = _keep_cited_only(stripped, citations)
    return Answer(text=stripped, citations=citations)


def _keep_cited_only(text: str, citations: list[Citation]) -> tuple[str, list[Citation]]:
    """답변에 실제로 인용된 출처만 남기고 번호를 1부터 다시 매긴다.

    검색은 유사도 top-k를 뽑지만 Claude가 다 쓰는 것은 아니다. 인용 안 된
    출처(질문과 스친 무관한 것 포함)를 그대로 두면 답변과 출처가 어긋나 보인다.
    그래서 본문의 [숫자]를 훑어 쓰인 것만 남기고, [3]만 쓰였으면 [1]로 당긴다.
    """
    used = [int(n) for n in re.findall(r"\[(\d+)\]", text)]
    if not used:
        # 인용 표기가 하나도 없으면(모델이 [n]을 안 붙임) 기존 출처를 유지한다.
        return text, citations

    # 등장 순서대로 중복 없이. 이 순서가 새 번호(1,2,3…)가 된다.
    order: list[int] = []
    for n in used:
        if n not in order:
            order.append(n)

    old_to_new = {old: i + 1 for i, old in enumerate(order)}
    by_number = {c.number: c for c in citations}

    new_citations: list[Citation] = []
    for old in order:
        c = by_number.get(old)
        if c is not None:
            new_citations.append(c.model_copy(update={"number": old_to_new[old]}))

    # 본문의 [old]를 [new]로 바꾼다. 여러 자리 충돌을 피해 임시 토큰을 거친다.
    new_text = text
    for old, new in old_to_new.items():
        new_text = new_text.replace(f"[{old}]", f"[#{new}#]")
    new_text = re.sub(r"\[#(\d+)#\]", r"[\1]", new_text)

    return new_text, new_citations


# --- 대화에서 일정 뽑아내기 (봇 → 노션 등록용) -----------------------
#
# 사용자가 붙여넣은 대화에서 일정을 구조화해 뽑는다. Tool Use를 쓰는 이유는
# 모델이 자유 문장 대신 **정해진 스키마의 JSON**을 돌려주게 만들기 위해서다
# (CLAUDE.md 7장의 Structured Output). 자유 텍스트를 받아 우리가 파싱하면
# 형식이 흔들려서 노션에 넣는 단계에서 깨진다.
#
# 날짜는 반드시 YYYY-MM-DD로 받는다. 노션 date 속성이 그 형식만 받기 때문이다.
# "다음주 화요일" 같은 상대 표현을 계산하려면 오늘이 며칠인지 알려줘야 하므로
# today를 시스템 프롬프트에 넣는다.

SCHEDULE_TOOL: dict = {
    "name": "register_schedule",
    "description": (
        "대화 내용에서 찾아낸 일정을 노션 캘린더에 등록한다. "
        "일정이 하나도 없으면 events를 빈 배열로 돌려준다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "description": "대화에서 찾은 일정 목록. 없으면 빈 배열.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "일정 이름"},
                        "date": {
                            "type": "string",
                            "description": "시작 날짜. 반드시 YYYY-MM-DD 형식.",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "종료 날짜(여러 날에 걸칠 때만). YYYY-MM-DD.",
                        },
                        "time": {
                            "type": "string",
                            "description": "시간. 예: 15:00 또는 10:00-11:30",
                        },
                        "place": {"type": "string", "description": "장소"},
                        "attendees": {
                            "type": "string",
                            "description": "참석자. 여러 명이면 쉼표로 구분.",
                        },
                        "type": {
                            "type": "string",
                            "description": "유형. 예: 회의, 발표, 마감, 교육",
                        },
                        "memo": {
                            "type": "string",
                            "description": "일정에 대한 짧은 설명.",
                        },
                    },
                    "required": ["name", "date"],
                },
            }
        },
        "required": ["events"],
    },
}


def build_schedule_system_prompt(today: str) -> str:
    """일정 추출용 시스템 프롬프트.

    답변용(build_system_prompt)과 규칙이 다르다. 저쪽의 핵심이 '인용'이라면
    여기서는 **없는 일정을 지어내지 않는 것**이 핵심이다. 잘못 뽑으면 노션에
    엉뚱한 일정이 등록되고, 사용자는 그게 왜 생겼는지 모른다.
    """
    return f"""너는 사내 대화에서 일정을 뽑아내는 도구다.

오늘은 {today}이다. '내일', '다음주 화요일' 같은 상대 표현은 이 날짜를
기준으로 계산해 YYYY-MM-DD로 바꾼다.

규칙:
1. 대화에 실제로 언급된 일정만 뽑는다. 추측해서 만들지 않는다.
2. 날짜를 특정할 수 없는 일정은 아예 제외한다. 임의의 날짜를 지어내지 않는다.
3. 이미 지나간 일을 회고하는 문장은 일정이 아니다. 앞으로 할 일만 뽑는다.
4. 일정이 하나도 없으면 events를 빈 배열로 돌려준다."""


async def extract_schedule_events(chat_log: str, today: str) -> list[dict]:
    """대화 내용에서 일정을 구조화해 뽑는다.

    Args:
        chat_log: 사용자가 모달에 붙여넣은 대화 원문
        today: 오늘 날짜(YYYY-MM-DD). 상대 표현을 계산하는 기준이 된다.

    Returns:
        [{"name","date","time","place","attendees","type","memo"}, ...]
        일정이 없거나 모델이 도구를 쓰지 않으면 빈 목록.

    호출하는 쪽에서 날짜 형식을 **한 번 더 검증해야 한다.** 프롬프트로
    형식을 지시했다고 해서 모델이 항상 지킨다는 보장은 없다.
    """
    if not chat_log.strip():
        return []

    response = await _request(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=build_schedule_system_prompt(today),
        tools=[SCHEDULE_TOOL],
        # 모델이 설명만 늘어놓지 않고 반드시 도구를 쓰게 강제한다.
        tool_choice={"type": "tool", "name": SCHEDULE_TOOL["name"]},
        messages=[{"role": "user", "content": chat_log}],
    )

    for block in response.content:
        if block.type == "tool_use":
            events = block.input.get("events", [])
            return events if isinstance(events, list) else []

    logger.warning("일정 추출: 모델이 도구를 사용하지 않았습니다.")
    return []
