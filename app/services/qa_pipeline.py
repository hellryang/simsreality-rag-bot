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
from datetime import datetime

from app.core.config import settings, setup_cli_logging
from app.models.schemas import Answer, SearchHit
from app.services.claude_service import answer_with_citations
from app.services.kakao_service import KST

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
    from app.services.vector_store import VectorStore

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
    hits: list[SearchHit] = []

    if settings.use_vector_search:
        # 저장소가 통째로 비어 있으면 아직 준비가 안 된 상태다. 이때는
        # 캘린더 도구도 켜지 않고 끊는다. build_db.py를 아직 안 돌린 환경
        # (그리고 API 키 없이 도는 테스트)에서 모델을 부르지 않기 위한 장치다.
        from app.services.vector_store import VectorStore

        store = VectorStore(persist_dir=persist_dir)
        if store.count() == 0:
            logger.warning("벡터 DB가 비어 있습니다. build_db.py를 먼저 실행하세요.")
            return Answer.no_context()

        hits = store.search(question, top_k=top_k)
        logger.info("질문 '%s' → 근거 조각 %d건", question, len(hits))
    else:
        # VectorStore를 만들지도 않는다. 여기서 search()를 부르지 않으면
        # 임베딩 모델이 프로세스에 올라오지 않는다(그게 이 스위치의 요점).
        logger.info("벡터 검색 꺼짐. 노션 캘린더 조회만으로 답합니다.")

    # 캘린더 조회 도구를 켠다. "다음주 빈 날"처럼 **벡터 DB에 근거가 있을 수
    # 없는** 질문이 있어서, 이 질문에 걸린 조각이 없어도 모델을 부른다.
    # (빈 날은 문서로 존재하지 않으므로 검색으로는 절대 안 나온다.)
    # 오늘 날짜는 '다음주' 같은 상대 표현을 날짜로 바꾸는 데 쓰인다.
    today = datetime.now(KST).date().isoformat()
    return await answer_with_citations(
        question, hits, allow_calendar=True, today=today
    )


def _clean(text: str, limit: int = 60) -> str:
    """미리보기용으로 줄바꿈을 없애고 길이를 자른다."""
    one_line = " ".join(text.split())
    return one_line[:limit] + ("…" if len(one_line) > limit else "")


def _citation_line(citation) -> str:
    """출처 한 건을 '어느 방/문서에서 언제 누가 쓴 내용인지'로 보여준다.

    카카오워크는 방·날짜·작성자·내용 미리보기를, 노션은 제목·URL을 보여준다.
    사용자가 답변의 근거를 바로 확인할 수 있게 하는 것이 목적이다(신뢰성).
    """
    head = f"  [{citation.number}] "

    if citation.source == "kakaowork":
        # 방 · 날짜 · 작성자
        meta = " · ".join(
            part for part in (citation.room_label, citation.msg_date, citation.sender) if part
        )
        lines = [head + (meta or "카카오워크")]
        # 본문 미리보기에서 이미 앞에 붙은 [날짜] 작성자: 는 떼어내고 내용만.
        body = citation.preview
        if "] " in body:
            body = body.split("] ", 1)[1]
        if ": " in body:
            body = body.split(": ", 1)[1]
        if body.strip():
            lines.append(f'      "{_clean(body)}"')
        return "\n".join(lines)

    # 노션·슬랙 등 URL이 있는 출처
    tail = f" — {citation.url}" if citation.url else ""
    return head + citation.title + tail


def format_hits(hits: list[SearchHit]) -> str:
    """검색 결과를 터미널에서 읽기 좋게 만든다. Claude를 부르지 않는다."""
    if not hits:
        return "찾은 조각이 없습니다. build_db.py를 먼저 실행했는지 확인하세요."

    lines = [f"찾은 조각: {len(hits)}건", ""]
    for hit in hits:
        preview = hit.chunk.text[:100].replace("\n", " ")
        lines.append(f"  유사도 {hit.score:.3f}  [{hit.chunk.title}]")
        lines.append(f"    {preview}...")
        lines.append(f"    출처: {hit.chunk.url}")
        lines.append("")
    return "\n".join(lines)


def format_answer(answer: Answer) -> str:
    """답변과 출처 목록을 터미널·카카오워크에서 읽기 좋게 만든다."""
    lines = [answer.text]

    if answer.citations:
        lines.append("")
        lines.append("출처:")
        for citation in answer.citations:
            lines.append(_citation_line(citation))

    return "\n".join(lines)


def run_chat(top_k: int = DEFAULT_TOP_K) -> None:
    """질문을 계속 받는 대화형 모드.

    **왜 필요한가**: 질문 한 번에 명령 한 번씩 실행하면 그때마다 임베딩 모델
    약 500MB를 새로 읽는다(10~20초). 프로그램이 끝나면 메모리에 올린 모델도
    같이 사라지기 때문이다. 켜둔 채로 질문을 받으면 이 비용을 한 번만 낸다.

    검색 품질을 확인하려고 질문 표현을 이리저리 바꿔볼 때 특히 편하다.
    """
    import asyncio

    from app.services.embedder import embed_texts

    print("임베딩 모델을 준비합니다. 처음 한 번만 걸립니다...")
    embed_texts(["준비"])  # 여기서 미리 메모리에 올려 첫 질문도 빠르게 만든다

    stored = VectorStore().count()
    print(f"\n벡터 DB에 조각 {stored}개가 있습니다.")
    if stored == 0:
        print("[!] 비어 있습니다. 먼저 `python build_db.py` 를 실행하세요.")

    print("\n질문을 입력하세요. 끝내려면 exit 또는 Ctrl+C.")
    print("질문 앞에 ? 를 붙이면 검색 결과만 봅니다 (Claude를 부르지 않아 무료).\n")

    while True:
        try:
            # lstrip("﻿")는 BOM 제거다. 파일을 파이프로 넣어 실행할 때
            # 맨 앞에 눈에 안 보이는 문자가 붙어 ? 판정이 어긋나는 일이 있다.
            question = input("질문> ").lstrip("﻿").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            return

        if not question:
            continue
        if question.lower() in ("exit", "quit", "종료"):
            print("종료합니다.")
            return

        # ? 로 시작하면 검색만. 적재가 잘 됐는지 볼 때 쓴다.
        if question.startswith("?"):
            print()
            print(format_hits(search_documents(question[1:].strip(), top_k=top_k)))
            continue

        try:
            answer = asyncio.run(answer_question(question, top_k=top_k))
        except KeyboardInterrupt:
            print("\n취소했습니다.")
            continue
        except Exception as exc:
            # 대화형이므로 한 번 실패했다고 세션을 끝내지 않는다.
            # 키 오류·요금 한도·네트워크 문제 모두 여기로 온다.
            print(f"\n[!] 답변 생성 실패: {type(exc).__name__}: {exc}")
            print("    ANTHROPIC_API_KEY와 네트워크를 확인하세요. "
                  "검색만 하려면 질문 앞에 ? 를 붙이세요.\n")
            continue

        print()
        print(format_answer(answer))
        print()


if __name__ == "__main__":
    # 단독 실행 확인용
    #   python -m app.services.qa_pipeline "배포는 어디에 하나요"           (Claude 키 필요)
    #   python -m app.services.qa_pipeline --search "배포는 어디에 하나요"   (키 없이 검색만)
    #   python -m app.services.qa_pipeline --chat                          (대화형)
    import asyncio
    import sys

    setup_cli_logging()

    args = sys.argv[1:]

    if "--chat" in args:
        run_chat()
        raise SystemExit(0)

    search_only = "--search" in args
    if search_only:
        args.remove("--search")

    if not args:
        print('사용법: python -m app.services.qa_pipeline "질문"')
        print('       python -m app.services.qa_pipeline --search "질문"   (키 없이 검색만)')
        print('       python -m app.services.qa_pipeline --chat           (대화형, 모델 1회 로딩)')
        raise SystemExit(1)

    user_question = " ".join(args)

    if search_only:
        print(f"\n질문: {user_question}\n")
        print(format_hits(search_documents(user_question)))
    else:
        result = asyncio.run(answer_question(user_question))
        print(f"\n질문: {user_question}\n")
        print(format_answer(result))
        print()
