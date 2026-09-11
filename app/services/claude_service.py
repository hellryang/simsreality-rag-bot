"""Claude API 호출.

시스템 프롬프트에는 항상 "제공된 컨텍스트 범위 안에서만 답하고, 각 문장 뒤에
출처 번호를 붙이며, 근거가 없으면 '관련 문서를 찾지 못했습니다'라고 답한다"를
포함시킨다. 모델 문자열은 app.core.config.Settings에서만 관리한다.
"""
from __future__ import annotations

import asyncio
import logging
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


def build_system_prompt() -> str:
    """답변 규칙을 고정하는 시스템 프롬프트.

    시스템 프롬프트(system prompt)는 모델에게 미리 주는 역할·규칙 지시문이다.
    사용자 질문마다 반복해서 붙이지 않아도 매 호출에 적용된다.

    이 세 줄이 우리 서비스의 답변 품질 기준 전부다.
    """
    return (
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


async def _create_message(system: str, user_content: str) -> str:
    """Claude를 호출하고 답변 텍스트만 꺼낸다. 실패하면 지수 백오프로 재시도한다.

    SDK도 429·5xx를 자동으로 두 번 재시도하지만, 그걸로도 안 되는 경우가 있어
    한 겹 더 감싼다. 401·404처럼 다시 보내도 결과가 같은 오류는 즉시 중단한다.
    """
    client = _get_client()
    delay = 1.0

    for attempt in range(1, _MAX_RETRY + 1):
        try:
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )

            # 안전 정책상 모델이 답변을 거절하는 경우가 있다. content가 비어 있어
            # 바로 [0]으로 접근하면 터지므로 stop_reason을 먼저 본다.
            if response.stop_reason == "refusal":
                logger.warning("Claude가 답변을 거절했습니다.")
                return NO_CONTEXT_ANSWER

            # content는 블록 목록이다. 우리가 쓸 것은 type이 "text"인 것뿐이다.
            texts = [block.text for block in response.content if block.type == "text"]
            return "\n".join(texts).strip()

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


async def answer_with_citations(question: str, hits: list[SearchHit]) -> Answer:
    """검색된 조각을 근거로 출처가 붙은 답변을 만든다.

    Args:
        question: 사용자 질문
        hits: 벡터 검색 결과 (유사도 높은 순)

    Returns:
        본문과 출처 목록이 담긴 Answer.
    """
    # 근거가 없는데 모델을 부르면 지어낸 답이 나오고 돈도 나간다.
    # 여기서 먼저 끊는 것이 환각 방지의 첫 번째 장치다.
    if not hits:
        logger.info("검색 결과가 없어 Claude를 호출하지 않습니다.")
        return Answer.no_context()

    context, citations = build_context(hits)
    user_content = (
        f"다음은 사내 문서에서 검색한 컨텍스트다.\n\n"
        f"{context}\n\n"
        f"---\n질문: {question}"
    )

    text = await _create_message(build_system_prompt(), user_content)

    # 모델이 근거 없음을 선언했다면 출처를 붙이지 않는다.
    if text.strip() == NO_CONTEXT_ANSWER:
        return Answer.no_context()

    return Answer(text=text, citations=citations)


async def summarize_work_request(request: str) -> str:
    """카카오워크 업무 요청을 Notion에 저장할 요약문으로 정리한다."""
    system = (
        "너는 업무 기록 정리 도우미다. 사용자가 보낸 업무 요청만 근거로 "
        "제목과 요약을 한국어로 작성한다. 없는 일정·담당자·기한은 만들지 않는다. "
        "다음 형식을 지킨다.\n제목: ...\n요약: ..."
    )
    return await _create_message(system, request)
