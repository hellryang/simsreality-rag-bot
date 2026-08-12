"""인터페이스 계약 테스트 — 이번 주(8/13~8/16) 과제 명세서.

네 명이 서로 다른 파일을 동시에 만든다. 각자 마음대로 함수 이름과 반환값을
정하면 주말 통합 때 전부 안 맞는다. 그래서 '어떤 함수가 무엇을 돌려줘야
하는지'를 코드로 먼저 못박아 둔 것이 이 파일이다.

--------------------------------------------------------------------
작업 방법 (중요)
--------------------------------------------------------------------
1. 아래에서 자기 이름이 붙은 테스트를 찾는다.
2. 그 테스트가 요구하는 함수를 담당 파일에 구현한다.
3. `pytest tests/test_contracts.py -q` 를 돌린다.
4. 통과하면 그 테스트 위의 `@pytest.mark.xfail(...)` **한 줄을 지운다.**
   지우지 않으면 XPASS(예상 밖 통과)로 실패 처리된다. 일부러 그렇게 해뒀다.
   "구현 끝났다"는 신고를 그 한 줄 삭제로 하는 것이다.
5. 커밋 → `feature/기능명` 브랜치로 push → develop에 PR.

테스트를 통과시키려고 **테스트를 고치지 않는다.** 계약을 바꿔야 한다고
생각되면 먼저 팀 채널에 올려서 합의한 뒤 이 파일을 고치는 PR을 따로 낸다.

import를 함수 안에서 하는 이유: 아직 빈 파일이라 맨 위에서 import하면
테스트 수집 단계에서 통째로 에러가 나 다른 사람 테스트까지 못 돌린다.
"""
import pytest

from app.models.schemas import NO_CONTEXT_ANSWER, Answer, Chunk, Document, SearchHit

# 300자보다 확실히 긴 본문. 글자마다 값이 달라야 겹침(overlap)을 확인할 수 있다.
LONG_TEXT = "".join(str(i % 10) for i in range(900))

SAMPLE_DOCUMENT = Document(
    text=LONG_TEXT,
    source="notion",
    url="https://www.notion.so/abc123",
    title="긴 회의록",
    created_at="2026-08-10T09:00:00.000Z",
)


# ====================================================================
# 마준서 — app/services/embedder.py
# ====================================================================

@pytest.mark.xfail(strict=True, reason="마준서: chunk_document 구현 후 이 줄 삭제")
def test_long_document_splits_into_chunks_of_300():
    """청킹(chunking): 긴 글을 검색 단위로 자르는 것.

    통째로 임베딩하면 벡터 하나에 온갖 주제가 섞여 검색이 뭉개진다.
    확정 규칙은 chunk 300 / overlap 50 이다.
    """
    from app.services.embedder import chunk_document

    chunks = chunk_document(SAMPLE_DOCUMENT)

    assert len(chunks) > 1
    assert all(isinstance(c, Chunk) for c in chunks)
    assert all(len(c.text) <= 300 for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


@pytest.mark.xfail(strict=True, reason="마준서: chunk_document 구현 후 이 줄 삭제")
def test_adjacent_chunks_overlap_by_50():
    """겹침(overlap)이 없으면 경계에 걸친 문장이 잘려서 검색이 안 된다.

    앞 조각의 끝 50자가 뒤 조각의 앞 50자와 같아야 한다.
    """
    from app.services.embedder import chunk_document

    chunks = chunk_document(SAMPLE_DOCUMENT)

    assert chunks[0].text[-50:] == chunks[1].text[:50]


@pytest.mark.xfail(strict=True, reason="마준서: chunk_document 구현 후 이 줄 삭제")
def test_short_document_stays_one_chunk():
    """300자가 안 되는 짧은 메시지를 억지로 쪼개면 안 된다."""
    from app.services.embedder import chunk_document

    short = Document(text="배포는 Railway로 간다", source="slack", title="공지")
    chunks = chunk_document(short)

    assert len(chunks) == 1
    assert chunks[0].text == "배포는 Railway로 간다"


@pytest.mark.xfail(strict=True, reason="마준서: embed_texts 구현 후 이 줄 삭제")
def test_embed_texts_returns_one_vector_per_input():
    """임베딩(embedding): 문장을 숫자 배열로 바꿔 의미를 비교할 수 있게 하는 것.

    첫 실행 때 모델 약 500MB를 내려받으므로 이 테스트만 느릴 수 있다.
    """
    from app.services.embedder import embed_texts

    vectors = embed_texts(["배포 일정이 어떻게 되나요", "회의록 정리"])

    assert len(vectors) == 2
    assert len(vectors[0]) == len(vectors[1])
    assert all(isinstance(v, float) for v in vectors[0])


# ====================================================================
# 마준서 — app/services/vector_store.py
# (4~5주차에 pgvector로 갈아끼울 지점이므로 호출부는 이 인터페이스만 쓴다)
# ====================================================================

@pytest.mark.xfail(strict=True, reason="마준서: VectorStore 구현 후 이 줄 삭제")
def test_search_finds_chunk_by_meaning(tmp_path):
    """키워드가 정확히 안 겹쳐도 의미가 비슷하면 찾아와야 한다.

    tmp_path는 pytest가 테스트마다 만들어 주는 임시 폴더다.
    진짜 chroma_data를 건드리지 않으려고 쓴다.
    """
    from app.services.vector_store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    store.add([
        Chunk.from_document(
            Document(text="배포는 Railway 무료 티어를 쓴다", source="notion",
                     url="https://notion.so/1", title="배포 결정"),
            text="배포는 Railway 무료 티어를 쓴다", index=0,
        ),
        Chunk.from_document(
            Document(text="점심은 담양에서 먹는다", source="slack",
                     url="https://slack.com/1", title="잡담"),
            text="점심은 담양에서 먹는다", index=0,
        ),
    ])

    hits = store.search("서버 어디에 올리나요", top_k=1)

    assert len(hits) == 1
    assert isinstance(hits[0], SearchHit)
    assert hits[0].chunk.title == "배포 결정"


@pytest.mark.xfail(strict=True, reason="마준서: VectorStore 구현 후 이 줄 삭제")
def test_adding_same_chunk_twice_upserts(tmp_path):
    """주기 수집이 같은 문서를 매번 다시 읽는다. 그때마다 쌓이면 안 된다.

    chunk_id가 같으면 덮어쓰기(upsert)로 처리되어야 한다.
    """
    from app.services.vector_store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    chunk = Chunk.from_document(SAMPLE_DOCUMENT, text="같은 내용", index=0)

    store.add([chunk])
    store.add([chunk])

    assert store.count() == 1


# ====================================================================
# 이인아 — app/services/claude_service.py
# ====================================================================

@pytest.mark.xfail(strict=True, reason="이인아: build_system_prompt 구현 후 이 줄 삭제")
def test_system_prompt_states_citation_rules():
    """시스템 프롬프트(system prompt): 모델에게 미리 주는 역할·규칙 지시문.

    '컨텍스트 밖 이야기 금지 / 문장마다 출처 번호 / 근거 없으면 정해진 문구'
    이 세 가지가 우리 서비스의 답변 품질 기준이다.
    """
    from app.services.claude_service import build_system_prompt

    prompt = build_system_prompt()

    assert NO_CONTEXT_ANSWER in prompt
    assert "출처" in prompt


async def test_empty_hits_skip_the_claude_call():
    """근거가 없는데 모델을 부르면 지어낸 답(환각)이 나오고 돈도 나간다.

    이 테스트는 API 키 없이 돌아야 한다. 네트워크를 타면 잘못 만든 것이다.
    """
    pytest.importorskip("anthropic", reason="이인아: pip install anthropic 후 활성화")
    from app.services.claude_service import answer_with_citations

    answer = await answer_with_citations("배포 일정 알려줘", hits=[])

    assert isinstance(answer, Answer)
    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


# ====================================================================
# 송준호 — app/services/slack_service.py
# ====================================================================

@pytest.mark.xfail(strict=True, reason="송준호: scrub_pii 구현 후 이 줄 삭제")
def test_phone_number_is_masked():
    """메신저 로그에는 개인정보가 섞인다. 벡터 DB에 그대로 들어가면 안 된다.

    한 번 임베딩되면 지우기 까다로우므로 수집 단계에서 거른다.
    """
    from app.services.slack_service import scrub_pii

    scrubbed = scrub_pii("급하면 010-1234-5678로 연락주세요")

    assert "010-1234-5678" not in scrubbed
    assert "연락주세요" in scrubbed


@pytest.mark.xfail(strict=True, reason="송준호: scrub_pii 구현 후 이 줄 삭제")
def test_email_address_is_masked():
    """멘토 이메일이 답변에 그대로 인용되는 사고를 막는다."""
    from app.services.slack_service import scrub_pii

    scrubbed = scrub_pii("문의는 hoo@simsreality.com 으로")

    assert "hoo@simsreality.com" not in scrubbed


@pytest.mark.xfail(strict=True, reason="송준호: scrub_pii 구현 후 이 줄 삭제")
def test_ordinary_sentence_is_untouched():
    """과하게 지우면 검색할 내용 자체가 사라진다."""
    from app.services.slack_service import scrub_pii

    original = "내일 3시에 회의실에서 봅시다"

    assert scrub_pii(original) == original
