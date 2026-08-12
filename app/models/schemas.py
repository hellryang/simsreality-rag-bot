"""공용 Pydantic 스키마.

수집 → 청킹 → 임베딩 → 검색 → 답변으로 이어지는 각 단계가 서로 주고받는
데이터의 모양을 여기 한 곳에 모아 둔다. 담당자가 달라도 이 파일만 지키면
각자 파일을 동시에 고쳐도 통합할 때 안 깨진다.

이 파일을 고치는 PR은 전원이 영향을 받으므로 반드시 팀 채널에 공유한다.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 답변 근거를 못 찾았을 때 쓰는 문구. 여러 곳에서 각자 적으면 표현이 갈리므로
# 반드시 이 상수를 import해서 쓴다.
NO_CONTEXT_ANSWER = "관련 문서를 찾지 못했습니다."

# 수집 소스 이름. 오타를 컴파일 시점이 아니라 검증 시점에 잡아준다.
SourceName = Literal["notion", "slack", "kakaowork"]


def _require_non_empty(value: str) -> str:
    """공백만 있는 문자열을 거부한다."""
    if not value.strip():
        raise ValueError("본문이 비어 있습니다")
    return value


class Document(BaseModel):
    """수집 함수 하나가 돌려주는 문서 한 건.

    `collect_notion_documents()` / `collect_slack_documents()` /
    `collect_kakao_documents()` 는 모두 이 모양의 dict 리스트를 돌려준다.
    """

    text: str
    source: SourceName
    url: str = ""
    title: str = ""
    created_at: str = ""

    _check_text = field_validator("text")(_require_non_empty)


class Chunk(BaseModel):
    """문서를 검색 단위로 자른 조각.

    벡터 DB에 실제로 들어가는 것은 Document가 아니라 이 Chunk다.
    """

    chunk_id: str
    text: str
    source: SourceName
    url: str = ""
    title: str = ""
    created_at: str = ""
    chunk_index: int = 0

    @classmethod
    def from_document(cls, document: Document, text: str, index: int) -> "Chunk":
        """문서에서 잘라낸 조각 하나를 만든다.

        Args:
            document: 원본 문서 (출처 메타데이터를 여기서 물려받는다)
            text: 잘라낸 본문
            index: 문서 안에서 몇 번째 조각인지 (0부터)
        """
        return cls(
            chunk_id=_build_chunk_id(document, index),
            text=text,
            source=document.source,
            url=document.url,
            title=document.title,
            created_at=document.created_at,
            chunk_index=index,
        )

    def metadata(self) -> dict[str, str | int]:
        """벡터 DB에 함께 저장할 메타데이터.

        ChromaDB의 metadata는 중첩 dict를 못 받고 문자열·숫자만 담을 수 있어서
        평평한 형태로 돌려준다. 인용 기능이 통째로 이 값에 의존한다.
        """
        return {
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "created_at": self.created_at,
            "chunk_index": self.chunk_index,
        }


def _build_chunk_id(document: Document, index: int) -> str:
    """문서와 순번으로 항상 같은 id를 만든다.

    주기 수집이 같은 문서를 다시 읽어도 id가 같으므로, 벡터 DB에 중복이
    쌓이지 않고 덮어쓰기(upsert)로 끝난다. url이 없는 소스도 있어서
    url이 비면 제목으로 대신한다.
    """
    identity = document.url or document.title
    raw = f"{document.source}|{identity}|{index}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Citation(BaseModel):
    """답변에 붙는 출처 한 건."""

    number: int = Field(ge=1)
    title: str
    url: str
    source: SourceName

    @classmethod
    def from_chunks(cls, chunks: list[Chunk]) -> list["Citation"]:
        """검색된 조각들에서 출처 목록을 만든다.

        같은 문서에서 여러 조각이 걸리는 일이 흔하므로 문서 단위로 합친다.
        번호는 넘겨받은 순서(= 유사도 높은 순)대로 1번부터 붙는다.
        """
        citations: list[Citation] = []
        seen: set[tuple[str, str]] = set()

        for chunk in chunks:
            key = (chunk.source, chunk.url or chunk.title)
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                cls(
                    number=len(citations) + 1,
                    title=chunk.title,
                    url=chunk.url,
                    source=chunk.source,
                )
            )

        return citations


class SearchHit(BaseModel):
    """벡터 검색 결과 한 건 (조각 + 유사도 점수)."""

    chunk: Chunk
    score: float = Field(ge=0.0, le=1.0)


class Answer(BaseModel):
    """사용자에게 최종으로 돌려주는 답변."""

    text: str
    citations: list[Citation] = Field(default_factory=list)

    @classmethod
    def no_context(cls) -> "Answer":
        """검색 결과가 없을 때의 표준 답변."""
        return cls(text=NO_CONTEXT_ANSWER, citations=[])
