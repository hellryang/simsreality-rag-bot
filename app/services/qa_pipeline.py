"""검색 → 컨텍스트 조립 → Claude 호출 → 출처 인용 답변 생성 파이프라인.

앞에서 만든 부품들을 이어 붙이는 조립부다. 여기서 새로운 로직을 만들지 않고
vector_store와 claude_service를 순서대로 부르기만 한다.

    질문 ──▶ search_documents()  ──▶ answer_with_citations() ──▶ 인용 답변
              (내 컴퓨터, 무료)         (Claude API, 유료)

검색과 답변을 굳이 두 함수로 나눈 이유:
ANTHROPIC_API_KEY가 없어도 "문서가 벡터 DB에 제대로 들어갔는지"는 확인할 수
있어야 한다. 그래야 적재 문제와 답변 문제를 나눠서 볼 수 있다.
"""
from __future__ import annotations

import logging

from app.core.config import setup_cli_logging
from app.models.schemas import Answer, SearchHit
from app.services.claude_service import answer_with_citations
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 몇 개의 조각을 근거로 삼을지. 너무 적으면 근거가 부족하고,
# 너무 많으면 관련 없는 내용까지 섞여 답변 품질이 떨어진다.
DEFAULT_TOP_K = 5


def search_documents(
    question: str,
    top_k: int = DEFAULT_TOP_K,
    persist_dir: str | None = None,
) -> list[SearchHit]:
    """질문과 의미가 가까운 조각을 찾는다. Claude를 부르지 않는다.

    Args:
        question: 사용자 질문
        top_k: 가져올 조각 수
        persist_dir: 벡터 DB 폴더. None이면 `.env`의 CHROMA_PERSIST_DIR

    Returns:
        유사도 높은 순 SearchHit 목록. 저장된 문서가 없으면 빈 목록.
    """
    store = VectorStore(persist_dir=persist_dir)
    return store.search(question, top_k=top_k)


async def answer_question(
    question: str,
    top_k: int = DEFAULT_TOP_K,
    persist_dir: str | None = None,
) -> Answer:
    """질문 하나에 출처가 붙은 답변을 돌려준다.

    검색 결과가 비면 Claude를 아예 호출하지 않는다. 근거 없이 모델을 부르면
    그럴듯한 거짓말(환각)이 나오고 API 비용도 나가기 때문이다.
    """
    hits = search_documents(question, top_k=top_k, persist_dir=persist_dir)
    logger.info("질문 '%s' → 근거 조각 %d건", question, len(hits))

    return await answer_with_citations(question, hits)


def format_answer(answer: Answer) -> str:
    """답변과 출처 목록을 터미널에서 읽기 좋게 만든다."""
    lines = [answer.text]

    if answer.citations:
        lines.append("")
        lines.append("출처:")
        for citation in answer.citations:
            lines.append(f"  [{citation.number}] {citation.title} — {citation.url}")

    return "\n".join(lines)


if __name__ == "__main__":
    # 단독 실행 확인용
    #   python -m app.services.qa_pipeline "배포는 어디에 하나요"           (Claude 키 필요)
    #   python -m app.services.qa_pipeline --search "배포는 어디에 하나요"   (키 없이 검색만)
    import asyncio
    import sys

    setup_cli_logging()

    args = sys.argv[1:]
    search_only = "--search" in args
    if search_only:
        args.remove("--search")

    if not args:
        print('사용법: python -m app.services.qa_pipeline "질문"')
        print('       python -m app.services.qa_pipeline --search "질문"   (키 없이 검색만)')
        raise SystemExit(1)

    user_question = " ".join(args)

    if search_only:
        found = search_documents(user_question)
        print(f"\n질문: {user_question}")
        print(f"찾은 조각: {len(found)}건\n")
        for hit in found:
            preview = hit.chunk.text[:100].replace("\n", " ")
            print(f"  유사도 {hit.score:.3f}  [{hit.chunk.title}]")
            print(f"    {preview}...")
            print(f"    출처: {hit.chunk.url}\n")
    else:
        result = asyncio.run(answer_question(user_question))
        print(f"\n질문: {user_question}\n")
        print(format_answer(result))
        print()
