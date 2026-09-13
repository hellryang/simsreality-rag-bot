"""qa_pipeline 테스트 — 검색과 답변을 잇는 조립부.

이 테스트는 **API 키 없이** 전부 돌아야 한다. 임베딩과 벡터 검색은 내 컴퓨터에서
실행되므로 네트워크가 필요 없고, Claude를 부르는 경로는 호출 전에 끊기는
경우만 확인한다. 테스트가 네트워크를 타면 잘못 만든 것이다.
"""
import pytest

from app.core.config import settings
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


async def test_empty_store_skips_the_claude_call(tmp_path, monkeypatch):
    """벡터 DB가 비어 있으면 Claude를 부르지 않는다.

    근거가 하나도 없는데 모델을 부르면 지어낸 답이 나오고 돈도 나간다.
    이 테스트가 통과한다는 것은 ANTHROPIC_API_KEY 없이도 이 경로가
    안전하게 끝난다는 뜻이다.

    use_vector_search를 명시적으로 켜는 이유: 이 가드는 벡터 검색 경로의
    것이다. 개발자마다 .env가 달라 테스트 결과가 갈리면 안 되므로
    로컬 설정에 기대지 않고 여기서 못 박는다.
    """
    monkeypatch.setattr(settings, "use_vector_search", True)
    from app.services.qa_pipeline import answer_question

    answer = await answer_question("배포 언제 하나요", persist_dir=str(tmp_path))

    assert isinstance(answer, Answer)
    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


async def test_with_vector_search_off_the_store_is_never_touched(tmp_path, monkeypatch):
    """벡터 검색을 끄면 VectorStore를 만들지도 않아야 한다.

    만들기만 해도 ChromaDB 폴더가 생기고, search()를 부르면 임베딩 모델
    (torch 포함 약 390MB)이 프로세스에 올라온다. 그 비용을 안 내는 것이
    이 스위치의 요점이다.
    """
    monkeypatch.setattr(settings, "use_vector_search", False)

    # VectorStore는 함수 안에서 지연 import 한다(벡터 패키지를 설치하지 않은
    # 배포에서도 앱이 기동해야 한다). 그래서 모듈 속성이 아니라 원본을 막는다.
    def explode(*args, **kwargs):
        raise AssertionError("벡터 검색이 꺼져 있으면 VectorStore를 만들면 안 된다")

    monkeypatch.setattr("app.services.vector_store.VectorStore", explode)

    called = {}

    async def fake_answer(question, hits, **kwargs):
        called["hits"] = hits
        called["allow_calendar"] = kwargs.get("allow_calendar")
        return Answer(text="답변", citations=[])

    monkeypatch.setattr("app.services.qa_pipeline.answer_with_citations", fake_answer)

    from app.services.qa_pipeline import answer_question

    await answer_question("다음주 빈 날", persist_dir=str(tmp_path))

    # 근거 조각은 비지만, 캘린더 도구는 켜져 있어야 답할 수 있다.
    assert called["hits"] == []
    assert called["allow_calendar"] is True


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


# --- 근거 없음 선언 뒤에 설명이 붙는 경우 ----------------------------


def _citation():
    from app.models.schemas import Citation

    return Citation(number=1, title="회의 내용", url="https://example.com", source="notion")


def test_bare_no_context_answer_drops_its_citations():
    from app.models.schemas import NO_CONTEXT_ANSWER
    from app.services.claude_service import _finalize

    answer = _finalize(NO_CONTEXT_ANSWER, [_citation()])

    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


def test_explanation_after_the_no_context_line_keeps_citations():
    """모순되는 첫 문장만 떼고 설명과 출처는 남긴다.

    모델이 규칙 3을 지키다 말고 설명을 덧붙이면, 그대로 두었을 때
    "찾지 못했습니다"라고 하면서 출처를 다는 모순이 생긴다.
    """
    from app.models.schemas import NO_CONTEXT_ANSWER
    from app.services.claude_service import _finalize

    citations = [_citation()]
    answer = _finalize(f"{NO_CONTEXT_ANSWER}\n\n[1]에는 제목만 있습니다.", citations)

    assert not answer.text.startswith(NO_CONTEXT_ANSWER)
    assert answer.text == "[1]에는 제목만 있습니다."
    assert answer.citations == citations


def test_no_context_line_with_only_whitespace_after_it_is_still_no_context():
    from app.models.schemas import NO_CONTEXT_ANSWER
    from app.services.claude_service import _finalize

    answer = _finalize(f"{NO_CONTEXT_ANSWER}\n\n  ", [_citation()])

    assert answer.text == NO_CONTEXT_ANSWER
    assert answer.citations == []


def test_a_normal_answer_is_left_alone():
    from app.services.claude_service import _finalize

    citations = [_citation()]
    answer = _finalize("3주차 회의는 8월 12일이었습니다. [1]", citations)

    assert answer.text == "3주차 회의는 8월 12일이었습니다. [1]"
    assert answer.citations == citations


# --- 인용된 출처만 남기기 --------------------------------------------


def _c(n, title=""):
    from app.models.schemas import Citation
    return Citation(number=n, title=title or f"문서{n}", url=f"http://x/{n}", source="notion")


def test_keeps_only_cited_and_renumbers():
    """답변에 실제 인용된 출처만 남기고 1부터 다시 번호를 매긴다."""
    from app.services.claude_service import _keep_cited_only

    cites = [_c(1), _c(2), _c(3), _c(4), _c(5)]
    text = "회의가 있습니다. [3] 안건 정리. [3] 제목 확인. [2]"

    new_text, new_cites = _keep_cited_only(text, cites)

    # [3]→[1], [2]→[2] 로 당겨짐
    assert new_text == "회의가 있습니다. [1] 안건 정리. [1] 제목 확인. [2]"
    assert [c.number for c in new_cites] == [1, 2]
    # 원래 3번 문서가 새 1번
    assert new_cites[0].title == "문서3"
    assert new_cites[1].title == "문서2"


def test_keeps_all_when_no_citation_marks():
    """본문에 [n] 표기가 없으면 기존 출처를 그대로 둔다."""
    from app.services.claude_service import _keep_cited_only

    cites = [_c(1), _c(2)]
    text = "출처 표기가 없는 답변입니다."

    new_text, new_cites = _keep_cited_only(text, cites)

    assert new_text == text
    assert len(new_cites) == 2


def test_drops_unrelated_sources():
    """인용 안 된 무관한 출처(top-k에 딸려온 것)는 제거된다."""
    from app.services.claude_service import _keep_cited_only

    cites = [_c(1, "관련"), _c(2, "무관")]
    text = "답변입니다. [1]"

    _, new_cites = _keep_cited_only(text, cites)

    assert len(new_cites) == 1
    assert new_cites[0].title == "관련"
