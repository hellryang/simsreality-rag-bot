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
from app.services.notion_service import CALENDAR_TYPES

logger = logging.getLogger(__name__)

# 답변 하나에 이 정도면 충분하다. 너무 크게 잡으면 모델이 장황해지고 비용도 는다.
MAX_TOKENS = 2000

# 요약·인용처럼 사실을 옮기는 작업은 낮은 온도가 맞다. 높이면 없는 말을
# 지어내고, 기간 라벨 선택도 호출마다 달라진다.
#
# 배포 중 이 줄 때문에 두 번 막혔다. anthropic SDK 1.x 는 temperature 를
# messages.create() 에서 제거했고(TypeError), 그 대안인 output_config.effort 는
# Haiku 4.5 가 지원하지 않는다(400: This model does not support the effort
# parameter). 그래서 requirements.txt 에서 SDK 를 0.121.0 으로 고정해
# 서버를 로컬과 같게 맞췄다 - 그 조합으로 모든 기능을 검증했다.
#
# 상위 모델(Sonnet 5, Opus 5 등)로 올릴 때는 temperature 가 제거되어 있으므로
# 이 줄을 output_config={"effort": ...} 로 바꾸고 SDK 도 함께 올려야 한다.
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
        "따로 기준일을 말하지 않는 한 이 날짜를 기준으로 한다.\n"
        "\n"
        "일정 관련 규칙:\n"
        "5. 일정에 관한 질문이면 lookup_calendar 도구로 노션 캘린더를 먼저 "
        "조회한다. 어떤 일정이 있는지·빈 날뿐 아니라 특정 일정의 참석자·장소·"
        "시간·유형·프로젝트를 묻는 질문도 포함한다. 추측하지 않는다.\n"
        "5-1. 도구의 period에는 '저번 주'->last_week처럼 **라벨만** 고른다. "
        "그 기간이 며칠부터 며칠까지인지는 직접 계산하지 않는다. "
        "사용자가 날짜를 직접 말한 경우에만 custom을 쓴다.\n"
        "5-2. '8/24 기준 다음 주'처럼 오늘이 아닌 기준일이 있으면 period는 "
        "라벨(next_week)로 고르고 base_date에 그 기준일을 YYYY-MM-DD로 적는다. "
        "기준일을 빠뜨리면 오늘 기준으로 엉뚱한 기간이 조회된다.\n"
        "6. 도구가 돌려준 결과는 확인된 사실이므로 그대로 근거로 쓴다. "
        "이때는 출처 번호를 붙이지 않고, 규칙 3의 '근거 없음'에도 해당하지 않는다.\n"
        "7. 도구가 날짜별 가능 여부(전일 가능 / 일부 시간 가능 / 확인 필요)를 "
        "알려주면 그 묶음대로 전한다. 일부 시간 가능인 날은 제외할 시간을 함께 적는다. "
        "날짜를 직접 계산하거나 빼거나 더하지 않는다.\n"
        "8. 도구가 조회에 실패했다고 하면 실패했다고 알린다. 임의로 답하지 않는다.\n"
        "9. 조회 결과에서 찾지 못했고 더 넓은 기간을 볼 필요가 있으면, "
        "'더 조회하겠습니다'라고 말하지 말고 **즉시 도구를 다시 호출한다.** "
        "도구는 한 번의 답변에서 여러 번 호출할 수 있다. 예고만 하고 끝내면 "
        "사용자는 아무 답도 받지 못한다."
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
        "노션 캘린더에서 지정한 기간의 일정을 조회한다. 일정마다 이름·날짜·시간·"
        "장소·유형·참석자·프로젝트·노션 링크가 함께 나오고, 그 기간에 "
        "'일정이 하나도 없는 날' 목록도 같이 나온다. "
        "다음 질문에는 추측하지 말고 반드시 이 도구를 먼저 호출한다: "
        "(1) 어떤 일정이 있는지, 빈 날이 언제인지, 언제 시간이 되는지. "
        "(2) 특정 일정의 **참석자가 누구인지**, 장소가 어디인지, 몇 시인지, "
        "어떤 유형인지, 어느 프로젝트인지. "
        "(3) **특정 인물이 참여한 일정**, 특정 장소에서 한 일정, 특정 프로젝트의 "
        "일정이 언제인지. 이때는 기간을 넓게(past_3_months 등) 잡고 keyword에 "
        "그 사람 이름이나 장소를 넣는다. 그러면 해당하는 일정만 돌아온다.\n"
        "날짜를 모르면 기간을 넓게 잡아 조회한다. 조회해 보지 않고 "
        "'정보가 없다'고 답하면 안 된다 - 실제로는 있는데 놓치는 일이 생긴다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "period": {
                "type": "string",
                "enum": [
                    "today",
                    "tomorrow",
                    "this_week",
                    "last_week",
                    "next_week",
                    "three_months_ago",
                    "two_months_ago",
                    "last_month",
                    "this_month",
                    "next_month",
                    "in_two_months",
                    "in_three_months",
                    "past_3_months",
                    "next_3_months",
                    "around_3_months",
                    "custom",
                ],
                "description": (
                    "조회할 기간. '오늘'→today, '내일'→tomorrow, "
                    "'이번 주'→this_week, '저번 주'/'지난주'→last_week, "
                    "'다음 주'→next_week. "
                    "특정 한 달만 볼 때(그 달 1일~말일): '이번 달'→this_month, "
                    "'지난달'→last_month, '지지난달'→two_months_ago, "
                    "'3개월 전 그 달'→three_months_ago, '다음 달'→next_month, "
                    "'2개월 후'→in_two_months, '3개월 후'→in_three_months. "
                    "여러 달을 한 번에 훑을 때: 과거 전체는 past_3_months(3개월 전~오늘), "
                    "앞으로 전체는 next_3_months, 앞뒤 모두는 around_3_months. "
                    "**날짜가 특정되지 않은 질문에는 반드시 범위 라벨을 쓴다.** "
                    "'이전에', '예전에', '언제였지', '전에 한' → past_3_months. "
                    "사람 이름·장소·프로젝트로 일정을 찾는 질문 → past_3_months 또는 "
                    "around_3_months. 한 달짜리 라벨로 찍어 맞히려 하면 놓친다. "
                    "'9월 15일부터 20일까지'처럼 날짜를 직접 말한 경우에만 custom. "
                    "**날짜를 직접 계산하지 말고 라벨만 고른다.** "
                    "'8/24 기준 다음 주'처럼 오늘이 아닌 날을 기준으로 말하면 "
                    "custom으로 날짜를 계산하지 말고, 같은 라벨(next_week)을 고른 뒤 "
                    "base_date에 기준일을 적는다."
                ),
            },
            "base_date": {
                "type": "string",
                "description": (
                    "라벨을 계산할 기준일. YYYY-MM-DD. 비우면 오늘이 기준이다. "
                    "사용자가 '8/24 기준', '8월 20일로부터'처럼 기준이 되는 "
                    "날짜를 말했을 때만 채운다. 그 날짜를 형식에 맞춰 옮겨 적기만 "
                    "하고 더하거나 빼지 않는다. "
                    "예: '8/24 기준 다음 주' -> period=next_week, base_date=2026-08-24. "
                    "연도를 말하지 않으면 오늘의 연도를 쓴다."
                ),
            },
            "keyword": {
                "type": "string",
                "description": (
                    "일정을 걸러낼 말. 사람 이름·장소·프로젝트·일정 이름 어디에든 "
                    "포함되면 남는다. 예: '이후경', '대구', '정기회의'. "
                    "특정 인물이나 장소로 찾는 질문에는 반드시 채운다 - 기간만 "
                    "넓히면 목록이 수십 건이 되어 놓치기 쉽다. "
                    "전체 목록이 필요할 때만 비워 둔다."
                ),
            },
            "start_date": {
                "type": "string",
                "description": "period가 custom일 때만. YYYY-MM-DD.",
            },
            "end_date": {
                "type": "string",
                "description": "period가 custom일 때만. YYYY-MM-DD(포함).",
            },
        },
        "required": ["period"],
    },
}


def _slot_text(slot: tuple[str, str]) -> str:
    start, end = slot
    return f"{start}~{end}" if end else f"{start}~(끝 시간 미기재)"


def _availability_lines(days: list[dict], keyword: str = "") -> list[str]:
    """날짜별 가능 여부를 세 묶음으로 적는다.

    "9/1은 10~11시 제외 가능"처럼 답하려면 시간을 비교해야 하는데, 그건
    계산이라 여기서 끝낸다. 모델은 묶음을 읽어 옮기기만 한다.

    묶음 이름에 '가능'을 못 박는 이유: 예전에 "선약 있음"과 "비어 있음"
    줄을 모델이 뒤집어 읽어 참여 가능한 날을 거꾸로 답한 적이 있다.
    """
    who = f"{keyword} " if keyword else ""
    free = [d for d in days if d["status"] == "free"]
    partial = [d for d in days if d["status"] == "partial"]
    unknown = [d for d in days if d["status"] == "unknown"]

    lines: list[str] = []
    if free:
        labels = ", ".join(f"{d['date']}({d['weekday']})" for d in free)
        lines.append(f"[전일 가능 - {who}일정 없음] {len(free)}일: {labels}")
    else:
        lines.append(f"[전일 가능 - {who}일정 없음]: 없음 (조회 기간 내내 일정이 있다)")

    if partial:
        lines.append(f"[일부 시간 가능 - {who}아래 시간에만 일정, 그 시간을 빼면 가능]")
        for d in partial:
            slots = ", ".join(_slot_text(slot) for slot in d["busy"])
            lines.append(
                f"  {d['date']}({d['weekday']}): {slots} 제외 가능 ({', '.join(d['names'])})"
            )

    if unknown:
        lines.append(
            f"[확인 필요 - {who}시간이 안 적힌 일정이 있어 가능 여부를 단정할 수 없음]"
        )
        for d in unknown:
            detail = ", ".join(d["untimed"]) + " (시간 미기재)"
            if d["busy"]:
                detail += " / 그 외 " + ", ".join(_slot_text(slot) for slot in d["busy"])
            lines.append(f"  {d['date']}({d['weekday']}): {detail}")

    lines.append(
        "가능한 날을 물으면 위 묶음대로 답한다. '일부 시간 가능'인 날은 전일 가능과 "
        "섞지 말고 제외할 시간을 함께 적는다. '확인 필요'인 날을 가능하다고 단정하지 않는다."
    )
    return lines


async def _run_calendar_tool(tool_input: dict, today: str) -> str:
    """lookup_calendar 도구를 실제로 수행하고 결과를 문자열로 돌려준다.

    기간 라벨을 실제 날짜로 바꾸는 것도, 빈 날을 계산하는 것도 파이썬이 한다.
    모델에게 날짜 산술을 시키면 틀린다 - 실측에서 2026-09-09(수)에 "저번 주"를
    물었더니 모델이 09-01~09-07을 잡았다(정답 08-31~09-06).

    모델에게 돌려줄 값이므로 실패해도 예외를 올리지 않는다. 예외를 올리면
    대화가 끊겨 사용자는 아무 답도 못 받는다. 실패 사유를 글로 적어 주면
    모델이 그것을 근거로 사정을 설명할 수 있다.
    """
    from app.services import notion_service

    # 기준일. 비어 있으면 오늘이다. 값이 있는데 형식이 틀리면 오늘로 대신하지
    # **않는다** - 그러면 "8/24 기준"을 물었는데 9월이 조회되는, 바로 이 기능이
    # 막으려던 오류가 조용히 재현된다. 실패로 돌려주면 모델이 고쳐 다시 부른다.
    base_date = str(tool_input.get("base_date", "") or "").strip()
    if base_date and not notion_service.is_iso_date(base_date):
        return (
            f"조회 실패: 기준일(base_date)은 YYYY-MM-DD 형식이어야 한다. "
            f"받은 값: {base_date}. 형식을 고쳐 다시 호출할 것."
        )
    anchor = base_date or today

    try:
        start, end = notion_service.resolve_period(
            str(tool_input.get("period", "")).strip(),
            anchor,
            str(tool_input.get("start_date", "")).strip(),
            str(tool_input.get("end_date", "")).strip(),
        )
    except notion_service.NotionWriteError as exc:
        return f"조회 실패: {exc}"

    keyword = str(tool_input.get("keyword", "")).strip()

    try:
        events = await notion_service.query_calendar_events(start, end)
        if keyword:
            # 걸러낸 목록으로 계산하면 "그 사람(키워드)이 되는 시간"이 된다.
            # 전체가 비어야 가능한 게 아니라 그 사람만 비어 있으면 된다.
            events = notion_service.filter_events(events, keyword)
        days = notion_service.compute_day_availability(start, end, events)
    except notion_service.NotionWriteError as exc:
        return f"조회 실패: {exc}"
    except Exception:
        logger.exception("캘린더 조회 중 오류")
        return "조회 실패: 캘린더를 읽는 중 오류가 발생했습니다."

    lines = [f"조회 기간: {start} ~ {end} (오늘은 {today})"]
    if base_date and base_date != today:
        lines.append(f"기준일: {base_date} (이 날짜를 기준으로 기간을 계산했다)")
    if keyword:
        lines.append(f"'{keyword}'가 포함된 일정만 골랐다.")
    if notion_service.is_past_range(end, today):
        # 사용자가 "저번 주에 회의 가능한 날"처럼 지난 기간을 묻는 일이 있다.
        # 답은 하되 지난 날이라는 사실을 함께 알려야 헷갈리지 않는다.
        lines.append("주의: 이 기간은 이미 지났다. 답변에서 그 점을 알릴 것.")

    if keyword and not events:
        lines.append(
            f"이 기간에 '{keyword}'가 포함된 일정은 없다. "
            f"다른 기간을 보려면 period를 바꿔 다시 호출할 것."
        )
    lines.append(f"일정 {len(events)}건")
    for event in events:
        when = event["date"]
        if event.get("end_date") and event["end_date"] != event["date"]:
            when += f"~{event['end_date']}"

        # 노션 캘린더의 속성을 전부 실어 보낸다. "누가 참석했어?", "어느
        # 프로젝트 일정이야?" 같은 질문에도 답할 수 있어야 하기 때문이다.
        # 라벨을 붙이는 이유: 값만 나열하면 "부산"이 장소인지 참석자인지
        # 모델이 헷갈린다.
        parts = [
            label + value
            for label, value in (
                ("", event.get("time") or ""),
                ("장소 ", event.get("place") or ""),
                ("유형 ", event.get("type") or ""),
                ("참석 ", event.get("attendees") or ""),
                ("프로젝트 ", event.get("project") or ""),
            )
            if value
        ]
        detail = " / ".join(parts)
        lines.append(f"  - {when} {event['name']}{' / ' + detail if detail else ''}")
        if event.get("url"):
            lines.append(f"      {event['url']}")

    lines.extend(_availability_lines(days, keyword))

    return "\n".join(lines)


async def _answer_with_tools(system: str, user_content: str, today: str) -> str:
    """캘린더 도구를 쓸 수 있게 하고, 도구 호출이 끝날 때까지 이어서 부른다.

    Tool Use는 한 번에 끝나지 않는다. 모델이 도구를 쓰겠다고 하면(stop_reason
    == "tool_use") 우리가 실제로 수행한 뒤 그 결과를 tool_result로 다시 넣어
    호출해야 최종 답변이 나온다(CLAUDE.md 7장의 멀티턴 루프).
    """
    messages: list[dict] = [{"role": "user", "content": user_content}]

    for round_index in range(MAX_TOOL_ROUNDS):
        # 첫 호출은 조회를 강제한다. 벡터 검색을 끈 구성에서는 캘린더가
        # 유일한 근거원인데, 모델이 조회를 건너뛰고 "정보가 없다"고 단정하는
        # 경우가 실제로 있었다(실제로는 있는 일정을 놓쳤다). 프롬프트 지시로는
        # 막히지 않아 구조로 막는다.
        # 두 번째 라운드부터는 auto다. 계속 강제하면 도구만 반복 호출하고
        # 최종 답변을 만들지 못한다.
        forced = round_index == 0
        response = await _request(
            model=settings.anthropic_model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=system,
            tools=[CALENDAR_TOOL],
            tool_choice=(
                {"type": "tool", "name": CALENDAR_TOOL["name"]}
                if forced
                else {"type": "auto"}
            ),
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
                    "content": await _run_calendar_tool(block.input, today),
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
        text = await _answer_with_tools(system, user_content, today)
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
        "사용자가 적은 문장에서 일정을 찾아 노션 캘린더에 등록한다. "
        "날짜는 계산하지 말고 date_type으로 분류한 뒤 문장에 있는 숫자·요일만 "
        "옮겨 적는다. 실제 날짜는 프로그램이 계산한다. "
        "일정이 하나도 없으면 events를 빈 배열로 돌려준다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "description": "문장에서 찾은 일정 목록. 없으면 빈 배열.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "일정 이름"},
                        "date_type": {
                            "type": "string",
                            "enum": [
                                "exact",
                                "today",
                                "tomorrow",
                                "day_after_tomorrow",
                                "days_later",
                                "this_week",
                                "next_week",
                                "week_after_next",
                            ],
                            "description": (
                                "날짜 표현의 종류. '9월 25일'·'9/25'->exact(month, day), "
                                "'오늘'->today, '내일'->tomorrow, '모레'(내일의 다음 날, 이틀 뒤)->day_after_tomorrow, "
                                "'3일 뒤'->days_later(days), '이번 주 금요일'->this_week(weekday), "
                                "'다음 주 화요일'·'차주 화요일'->next_week(weekday), "
                                "'다다음 주 월요일'->week_after_next(weekday). "
                                "**날짜를 직접 계산하지 않는다.**"
                            ),
                        },
                        "year": {
                            "type": "integer",
                            "description": "exact일 때, 사용자가 연도를 말한 경우에만.",
                        },
                        "month": {"type": "integer", "description": "exact일 때 월(1-12)."},
                        "day": {"type": "integer", "description": "exact일 때 일(1-31)."},
                        "days": {
                            "type": "integer",
                            "description": "days_later일 때 며칠 뒤인지. '3일 뒤'->3.",
                        },
                        "weekday": {
                            "type": "string",
                            "enum": ["월", "화", "수", "목", "금", "토", "일"],
                            "description": "this_week/next_week/week_after_next일 때 요일.",
                        },
                        "end_year": {
                            "type": "integer",
                            "description": "여러 날 일정의 종료 연도. 말한 경우에만.",
                        },
                        "end_month": {
                            "type": "integer",
                            "description": "여러 날 일정의 종료 월. 말하지 않았으면 비운다.",
                        },
                        "end_day": {
                            "type": "integer",
                            "description": "여러 날 일정의 종료 일. '25일부터 27일까지'->27.",
                        },
                        "end_weekday": {
                            "type": "string",
                            "enum": ["월", "화", "수", "목", "금", "토", "일"],
                            "description": (
                                "주 단위 표현의 여러 날 일정 종료 요일. "
                                "'다음 주 월요일부터 수요일까지'->수."
                            ),
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
                            "enum": list(CALENDAR_TYPES),
                            "description": "유형. 목록에서 고른다. 맞는 것이 없으면 넣지 않는다.",
                        },
                        "memo": {
                            "type": "string",
                            "description": "일정에 대한 짧은 설명.",
                        },
                    },
                    "required": ["name", "date_type"],
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

    날짜 계산은 시키지 않는다. "다음 주 화요일"을 모델이 YYYY-MM-DD로 바꾸면
    형식은 맞는데 날짜가 틀린 값이 그대로 등록된다. 분류만 받고
    notion_service.resolve_event_date()가 계산한다.
    """
    return f"""너는 사용자가 적은 문장에서 일정을 뽑아내는 도구다.

오늘은 {today}이다. 지나간 일인지 판단할 때만 참고하고, 날짜를 계산하는 데 쓰지 않는다.

규칙:
1. 문장에 실제로 언급된 일정만 뽑는다. 추측해서 만들지 않는다.
2. 날짜 표현이 없는 일정은 제외한다. 임의의 날짜를 지어내지 않는다.
3. 이미 지나간 일을 회고하는 문장은 일정이 아니다. 앞으로 할 일만 뽑는다.
4. 일정이 하나도 없으면 events를 빈 배열로 돌려준다.
5. 날짜는 절대 YYYY-MM-DD로 계산하지 않는다. date_type으로 분류하고,
   문장에 있는 월·일·며칠·요일만 옮겨 적는다. 연도는 사용자가 말했을 때만 적는다.
   '내일'과 '모레'를 구분한다. 모레는 이틀 뒤다(day_after_tomorrow).
6. 유형은 주어진 목록에서 고른다. 맞는 것이 없으면 비워 둔다."""


async def extract_schedule_events(chat_log: str, today: str) -> list[dict]:
    """대화 내용에서 일정을 구조화해 뽑는다.

    Args:
        chat_log: 사용자가 모달에 붙여넣은 대화 원문
        today: 오늘 날짜(YYYY-MM-DD). 상대 표현을 계산하는 기준이 된다.

    Returns:
        [{"name","date_type","month","day","weekday",...,"time","place",
          "attendees","type","memo"}, ...]
        날짜는 계산되지 않은 분류 상태다. notion_service.prepare_reserved_event()로
        실제 날짜를 채운다.
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
