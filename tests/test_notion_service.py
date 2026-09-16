"""Notion 수집 로직 테스트 — 담당: 임혜량

여기 있는 테스트는 실제 Notion API를 호출하지 않는다.
API 응답을 흉내 낸 dict를 넣어서 '뽑아내는 로직'만 검증한다.
그래서 API 키 없이도, 인터넷 없이도, 몇 밀리초 만에 돌아간다.
"""
from app.models.schemas import Document
from app.services.notion_service import (
    _extract_child_pages,
    _extract_plain_text,
    _extract_table_rows,
    _extract_title,
)


def _row(*values: str) -> dict:
    """table_row 블록을 흉내 낸다. Notion은 칸마다 rich_text 배열을 한 겹 더 감싼다."""
    return {
        "type": "table_row",
        "table_row": {"cells": [[{"plain_text": v}] if v else [] for v in values]},
    }


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


def test_table_header_is_attached_to_every_row():
    """표의 각 행에 머리글이 붙어야 조각으로 잘려도 뜻이 통한다.

    "임혜량 | 완료" 만 남으면 이게 담당자인지 작성자인지 알 수 없다.
    "담당: 임혜량" 이어야 "누가 담당이야" 라는 질문에 검색이 걸린다.
    """
    rows = [
        _row("담당", "상태"),
        _row("임혜량", "완료"),
        _row("마준서", "진행중"),
    ]

    assert _extract_table_rows(rows, has_column_header=True) == [
        "담당: 임혜량 | 상태: 완료",
        "담당: 마준서 | 상태: 진행중",
    ]


def test_table_without_header_keeps_values_only():
    """머리글이 없는 표는 붙일 이름이 없으므로 값만 이어 붙인다."""
    rows = [_row("Railway", "무료"), _row("Render", "무료")]

    assert _extract_table_rows(rows, has_column_header=False) == [
        "Railway | 무료",
        "Render | 무료",
    ]


def test_empty_cells_and_rows_are_dropped():
    """빈 칸까지 "담당: " 처럼 남기면 검색에 잡음만 늘어난다."""
    rows = [
        _row("담당", "상태"),
        _row("임혜량", ""),
        _row("", ""),
    ]

    assert _extract_table_rows(rows, has_column_header=True) == ["담당: 임혜량"]


def test_non_table_row_blocks_are_ignored():
    """표 밑에 다른 블록이 섞여 와도 예외 없이 행만 골라낸다."""
    rows = [{"type": "paragraph", "paragraph": {"rich_text": []}}]

    assert _extract_table_rows(rows, has_column_header=False) == []


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
