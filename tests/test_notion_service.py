"""Notion 수집 로직 테스트 — 담당: 임혜량

여기 있는 테스트는 실제 Notion API를 호출하지 않는다.
API 응답을 흉내 낸 dict를 넣어서 '뽑아내는 로직'만 검증한다.
그래서 API 키 없이도, 인터넷 없이도, 몇 밀리초 만에 돌아간다.
"""
from app.models.schemas import Document
from app.services.notion_service import (
    _extract_child_pages,
    _extract_plain_text,
    _extract_title,
)


def test_rich_text_fragments_are_joined():
    """Notion은 글자 스타일이 바뀔 때마다 텍스트를 쪼개서 보낸다.

    '배포는 **Railway**로'라고 쓰면 3조각으로 나뉘어 오므로 다시 붙여야 한다.
    """
    rich_text = [
        {"plain_text": "배포는 "},
        {"plain_text": "Railway"},
        {"plain_text": "로 진행한다"},
    ]

    assert _extract_plain_text(rich_text) == "배포는 Railway로 진행한다"


def test_empty_rich_text_returns_empty_string():
    """빈 문단이 들어와도 예외 없이 넘어가야 수집이 중간에 멈추지 않는다."""
    assert _extract_plain_text([]) == ""


def test_title_is_found_by_type_not_name():
    """제목 칸의 이름은 DB마다 '이름'/'Name'/'제목'으로 제각각이다.

    그래서 이름이 아니라 type이 'title'인 것을 찾아야 한다.
    """
    page = {
        "properties": {
            "담당자": {"type": "people", "people": []},
            "회의명": {"type": "title", "title": [{"plain_text": "8월 정기회의"}]},
        }
    }

    assert _extract_title(page) == "8월 정기회의"


def test_missing_title_falls_back_to_placeholder():
    """제목이 빈 페이지도 본문은 검색에 쓸모가 있으므로 버리지 않는다."""
    page = {"properties": {"이름": {"type": "title", "title": []}}}

    assert _extract_title(page) == "(제목 없음)"


def test_child_pages_are_extracted_with_id_and_title():
    """페이지 밑에 달린 하위 페이지 목록을 뽑아낸다.

    우리 Notion은 '2026 일경험 프로젝트' 페이지 아래에 문서들이 하위 페이지로
    붙어 있는 구조다. 데이터베이스가 아니므로 databases.query로는 못 읽는다.
    Notion은 하위 페이지를 `child_page` 타입 블록으로 돌려준다.
    """
    blocks = [
        {"id": "aaa", "type": "child_page", "child_page": {"title": "프로젝트 문제정의"}},
        {"id": "bbb", "type": "child_page", "child_page": {"title": "회의 내용"}},
    ]

    assert _extract_child_pages(blocks) == [
        ("aaa", "프로젝트 문제정의"),
        ("bbb", "회의 내용"),
    ]


def test_non_page_blocks_are_ignored_when_listing_children():
    """본문 문단이나 표는 하위 페이지가 아니므로 목록에서 빠져야 한다.

    표(table)를 걸러내는 것은 특히 중요하다. 우리 프로젝트 페이지의 표에는
    팀원 연락처가 들어 있어서, 문서로 취급하면 개인정보가 딸려 들어간다.
    """
    blocks = [
        {"id": "p1", "type": "paragraph", "paragraph": {"rich_text": []}},
        {"id": "t1", "type": "table", "table": {}},
        {"id": "aaa", "type": "child_page", "child_page": {"title": "운영비용"}},
        {"id": "d1", "type": "child_database", "child_database": {"title": "새 데이터베이스"}},
    ]

    assert _extract_child_pages(blocks) == [("aaa", "운영비용")]


def test_page_without_children_returns_empty_list():
    """하위 페이지가 하나도 없어도 예외 없이 빈 목록을 돌려준다."""
    assert _extract_child_pages([]) == []


def test_collected_shape_matches_document_schema():
    """수집 함수가 만드는 dict의 모양이 공용 계약과 맞는지 확인한다.

    이 테스트가 깨지면 = 수집부를 고치면서 embedder/vector_store를 함께
    망가뜨린 것이다.
    """
    collected = {
        "text": "8월 정기회의\n배포는 Railway로 진행한다",
        "source": "notion",
        "url": "https://www.notion.so/abc123",
        "title": "8월 정기회의",
        "created_at": "2026-08-10T09:00:00.000Z",
    }

    document = Document.model_validate(collected)

    assert document.source == "notion"
    assert document.text.startswith("8월 정기회의")
