"""벡터 스토어 래퍼.

현재는 ChromaDB(cosine)를 사용한다. 4~5주차에 PostgreSQL + pgvector(Supabase)로
교체할 예정이므로, 호출부는 이 모듈의 인터페이스에만 의존하도록 유지한다.

즉 다른 파일에서 `import chromadb` 를 하면 안 된다. 교체할 때 그 파일까지
전부 고쳐야 하기 때문이다. 바꿔야 할 곳은 이 파일 하나로 묶어 둔다.
"""
from __future__ import annotations

import logging

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import settings
from app.models.schemas import Chunk, SearchHit
from app.services.embedder import embed_chunks, embed_texts

logger = logging.getLogger(__name__)

COLLECTION_NAME = "documents"


class VectorStore:
    """조각을 저장하고 의미로 검색하는 창구."""

    def __init__(self, persist_dir: str | None = None) -> None:
        """
        Args:
            persist_dir: 데이터를 둘 폴더. 넘기지 않으면 `.env`의
                CHROMA_PERSIST_DIR을 쓴다. 테스트는 임시 폴더를 넘겨서
                실제 데이터를 건드리지 않는다.
        """
        self._client = chromadb.PersistentClient(
            path=persist_dir or settings.chroma_persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        # cosine으로 지정하지 않으면 ChromaDB는 기본값(L2)을 쓴다.
        # 우리는 길이를 1로 맞춘 벡터를 쓰므로 코사인이 맞다.
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: list[Chunk]) -> None:
        """조각들을 저장한다.

        `upsert`를 쓰므로 같은 chunk_id가 이미 있으면 덮어쓴다.
        주기 수집(APScheduler)이 같은 문서를 다시 읽어도 중복이 쌓이지 않는다.
        """
        if not chunks:
            return

        self._collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            embeddings=embed_chunks(chunks),
            metadatas=[chunk.metadata() for chunk in chunks],
        )
        logger.info("벡터 DB에 조각 %d건 저장", len(chunks))

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        """질문과 의미가 가까운 조각을 top_k개 찾는다.

        Returns:
            유사도가 높은 순서의 SearchHit 목록.
        """
        if self.count() == 0:
            logger.warning("벡터 DB가 비어 있습니다. build_db.py를 먼저 실행하세요.")
            return []

        result = self._collection.query(
            query_embeddings=embed_texts([query]),
            n_results=min(top_k, self.count()),
        )

        hits: list[SearchHit] = []
        # ChromaDB는 질문을 여러 개 받을 수 있어서 결과가 한 겹 더 감싸여 온다.
        # 우리는 질문이 하나이므로 [0]만 본다.
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]

        for chunk_id, text, metadata, distance in zip(ids, documents, metadatas, distances):
            hits.append(
                SearchHit(
                    chunk=Chunk(
                        chunk_id=chunk_id,
                        text=text,
                        source=metadata.get("source", "notion"),
                        url=metadata.get("url", ""),
                        title=metadata.get("title", ""),
                        created_at=metadata.get("created_at", ""),
                        chunk_index=int(metadata.get("chunk_index", 0)),
                    ),
                    score=_distance_to_score(distance),
                )
            )
        return hits

    def count(self) -> int:
        """저장된 조각 수."""
        return self._collection.count()

    def reset(self) -> None:
        """전부 지운다. 수집 구조를 바꿔 다시 넣을 때 쓴다."""
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("벡터 DB를 비웠습니다.")


def _distance_to_score(distance: float) -> float:
    """코사인 '거리'를 0~1 '유사도 점수'로 바꾼다.

    ChromaDB는 가까울수록 작은 거리(0에 가까움)를 준다. 사람이 읽기엔
    가까울수록 높은 점수가 자연스러우므로 뒤집는다. 부동소수점 오차로
    범위를 살짝 벗어나는 일이 있어 0~1로 잘라낸다.
    """
    return max(0.0, min(1.0, 1.0 - float(distance)))
