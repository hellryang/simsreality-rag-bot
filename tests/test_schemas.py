"""공용 스키마 계약 테스트.

이 파일은 4명이 각자 파일을 동시에 고쳐도 서로 안 깨지도록
'주고받는 데이터의 모양'을 못박아 두는 역할을 한다.
여기 테스트가 깨지면 = 누군가 남의 코드를 조용히 망가뜨린 것이다.
"""
import pytest
from pydantic import ValidationError

from app.models.schemas import NO_CONTEXT_ANSWER, Answer, Chunk, Citation, Document


# notion_service.collect_notion_documents()가 실제로 돌려주는 모양.
# 이 dict를 바꾸려면 notion_service와 이 테스트를 같은 PR에서 함께 고쳐야 한다.
NOTION_DOC = {
    "text": "8월 정기회의\n배포는 Railway 무료 티어로 진행하기로 했다.",
    "source": "notion",
    "url": "https://www.notion.so/abc123",
    "title": "8월 정기회의",
    "created_at": "2026-08-10T09:00:00.000Z",
}


def test_collected_dict_validates_as_document():
    """수집 함수의 반환 dict가 Document 스키마와 1:1로 맞는지 확인한다.

    세 소스(notion/slack/kakaowork)의 수집 함수는 모두 이 모양을 지켜야 한다.
    """
    doc = Document.model_validate(NOTION_DOC)

    assert doc.source == "notion"
    assert doc.title == "8월 정기회의"
    assert doc.url == "https://www.notion.so/abc123"


def test_unknown_source_is_rejected():
    """source에 오타가 나면 벡터 DB에 들어가기 전에 걸러야 한다.

    'slak' 같은 오타가 그대로 적재되면 소스별 필터링과 인용이 조용히 망가진다.
    """
    with pytest.raises(ValidationError):
        Document.model_validate({**NOTION_DOC, "source": "slak"})


def test_blank_text_is_rejected():
    """빈 문서를 임베딩하면 의미 없는 벡터가 검색 결과를 오염시킨다."""
    with pytest.raises(ValidationError):
        Document.model_validate({**NOTION_DOC, "text": "   "})


def test_chunk_id_is_deterministic():
    """chunk_id는 '결정적(deterministic)'이어야 한다.

    주기 수집(APScheduler)이 같은 문서를 다시 읽었을 때 id가 매번 달라지면
    벡터 DB에 같은 내용이 계속 쌓인다. id가 같으면 덮어쓰기(upsert)로 끝난다.
    """
    doc = Document.model_validate(NOTION_DOC)

    first = Chunk.from_document(doc, text="앞부분", index=0)
    again = Chunk.from_document(doc, text="앞부분", index=0)
    other = Chunk.from_document(doc, text="뒷부분", index=1)

    assert first.chunk_id == again.chunk_id
    assert first.chunk_id != other.chunk_id


def test_metadata_carries_citation_fields():
    """인용(citation) 기능이 이 메타데이터에 통째로 의존한다.

    ChromaDB의 metadata는 문자열/숫자만 담을 수 있으므로 평평한 dict여야 한다.
    """
    doc = Document.model_validate(NOTION_DOC)
    chunk = Chunk.from_document(doc, text="앞부분", index=0)

    metadata = chunk.metadata()

    assert set(metadata) >= {"source", "url", "title", "created_at"}
    assert metadata["source"] == "notion"
    assert all(isinstance(v, (str, int, float)) for v in metadata.values())


def test_no_context_answer_is_standardized():
    """'문서를 못 찾았을 때 뭐라고 답할지'를 한 곳에서만 정한다.

    claude_service와 qa_pipeline이 각자 다른 문구를 쓰면 평가 기준이 흔들린다.
    """
    answer = Answer.no_context()

    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


def test_citations_are_numbered_from_one():
    """답변 본문의 [1], [2] 표기와 목록 순서가 어긋나면 출처가 뒤바뀐다.

    번호는 넘겨받은 순서 = 유사도가 높은 순서대로 붙어야 한다.
    """
    docs = [
        Document.model_validate({**NOTION_DOC, "url": f"https://notion.so/{i}", "title": f"문서{i}"})
        for i in range(3)
    ]
    chunks = [Chunk.from_document(doc, text="조각", index=0) for doc in docs]

    citations = Citation.from_chunks(chunks)

    assert [c.number for c in citations] == [1, 2, 3]
    assert [c.title for c in citations] == ["문서0", "문서1", "문서2"]


def test_citations_are_deduplicated_per_document():
    """한 문서가 3조각 걸렸다고 출처를 3번 적으면 답변이 지저분해진다."""
    doc = Document.model_validate(NOTION_DOC)
    other = Document.model_validate({**NOTION_DOC, "url": "https://www.notion.so/def456"})

    chunks = [
        Chunk.from_document(doc, text="조각0", index=0),
        Chunk.from_document(doc, text="조각1", index=1),
        Chunk.from_document(other, text="조각0", index=0),
    ]

    citations = Citation.from_chunks(chunks)

    assert len(citations) == 2
