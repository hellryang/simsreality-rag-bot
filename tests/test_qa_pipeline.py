"""qa_pipeline 테스트 — 검색과 답변을 잇는 조립부.

이 테스트는 **API 키 없이** 전부 돌아야 한다. 임베딩과 벡터 검색은 내 컴퓨터에서
실행되므로 네트워크가 필요 없고, Claude를 부르는 경로는 호출 전에 끊기는
경우만 확인한다. 테스트가 네트워크를 타면 잘못 만든 것이다.
"""
import pytest

from app.models.schemas import NO_CONTEXT_ANSWER, Answer, Chunk, Document, SearchHit

DEPLOY_TEXT = (
    "배포 환경 결정. 백엔드 서버는 Railway 무료 티어에 올리기로 했다. "
    "Render도 검토했으나 무료 플랜에서 슬립 시간이 길어 제외했다."
)
MEETING_TEXT = (
    "담양 대면회의 일정. 점심은 근처 국수집에서 먹기로 했고 "
    "왕복 차비는 운영비에서 정산한다."
)


@pytest.fixture
def filled_store(tmp_path):
    """문서 두 건이 들어 있는 임시 벡터 DB를 만든다.

    tmp_path는 pytest가 테스트마다 새로 만들어 주는 빈 폴더다.
    실제 chroma_data를 건드리지 않으려고 쓴다.
    """
    from app.services.vector_store import VectorStore

    store = VectorStore(persist_dir=str(tmp_path))
    for text, title in [(DEPLOY_TEXT, "배포 결정"), (MEETING_TEXT, "회의 일정")]:
        document = Document(
            text=text, source="notion",
            url=f"https://notion.so/{title}", title=title,
        )
        store.add([Chunk.from_document(document, text=text, index=0)])
    return str(tmp_path)


async def test_empty_store_skips_the_claude_call(tmp_path):
    """벡터 DB가 비어 있으면 Claude를 부르지 않는다.

    근거가 하나도 없는데 모델을 부르면 지어낸 답이 나오고 돈도 나간다.
    이 테스트가 통과한다는 것은 ANTHROPIC_API_KEY 없이도 이 경로가
    안전하게 끝난다는 뜻이다.
    """
    from app.services.qa_pipeline import answer_question

    answer = await answer_question("배포 언제 하나요", persist_dir=str(tmp_path))

    assert isinstance(answer, Answer)
    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


def test_search_documents_returns_hits_without_claude(filled_store):
    """검색만 따로 쓸 수 있어야 한다.

    Claude 키가 없어도 '문서가 제대로 적재됐는지'는 확인할 수 있어야
    적재와 답변 문제를 나눠서 볼 수 있다.
    """
    from app.services.qa_pipeline import search_documents

    hits = search_documents("백엔드를 어디에 올리나요", top_k=1, persist_dir=filled_store)

    assert len(hits) == 1
    assert isinstance(hits[0], SearchHit)
    assert hits[0].chunk.title == "배포 결정"


def test_search_returns_empty_list_when_store_is_empty(tmp_path):
    """빈 DB에 검색해도 예외 없이 빈 목록을 돌려준다."""
    from app.services.qa_pipeline import search_documents

    assert search_documents("아무 질문", persist_dir=str(tmp_path)) == []
