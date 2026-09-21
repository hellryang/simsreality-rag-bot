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
from app.models.schemas import Chunk, SearchHit, StoredDocument
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
                        submitted_by=metadata.get("submitted_by", ""),
                        room_label=metadata.get("room_label", ""),
                        sender=metadata.get("sender", ""),
                        msg_date=metadata.get("msg_date", ""),
                    ),
                    score=_distance_to_score(distance),
                )
            )
        return hits

    def count(self) -> int:
        """저장된 조각 수."""
        return self._collection.count()

    def list_documents(self, source: str | None = None) -> list[StoredDocument]:
        """저장된 문서 목록. 질문도 임베딩 계산도 필요 없다.

        search()로는 "무엇이 들어 있는지"를 알 수 없다. 질문과 의미가 가까운
        조각 몇 개만 돌려주기 때문이다. 지울 대상을 고르려면 전체를 훑어야
        하므로 메타데이터만 읽어 문서 단위로 묶는다.

        Args:
            source: "kakaowork" 처럼 출처 하나만 보고 싶을 때. 생략하면 전부.

        Returns:
            조각 수가 많은 순서의 문서 목록.
        """
        result = self._collection.get(
            where={"source": source} if source else None,
            include=["metadatas"],
        )

        documents: dict[tuple[str, str], StoredDocument] = {}
        for chunk_id, metadata in zip(result["ids"], result["metadatas"]):
            key = (metadata.get("source", ""), metadata.get("title", ""))
            document = documents.get(key)
            if document is None:
                document = StoredDocument(
                    source=metadata.get("source", "notion"),
                    title=metadata.get("title", ""),
                    url=metadata.get("url", ""),
                    created_at=metadata.get("created_at", ""),
                    submitted_by=metadata.get("submitted_by", ""),
                    room_label=metadata.get("room_label", ""),
                )
                documents[key] = document
            document.chunk_ids.append(chunk_id)

        return sorted(documents.values(), key=lambda d: d.chunk_count, reverse=True)

    def list_by_submitter(
        self, submitted_by: str, room_label: str | None = None, limit: int = 25
    ) -> list[dict]:
        """한 사용자가 제출한 문서를 최근 순으로 돌려준다.

        카카오워크에서 "내 저장 관리"로 자기 글을 조회·삭제하기 위한 것.
        room_label을 주면 그 방에서 저장한 것만 돌려준다(방에 들어가 관리할 때).
        모달 Select에 채울 것이라 각 항목의 chunk_id·제목·미리보기·시각을 담는다.
        (모달 저장은 문서 하나 = 조각 하나라 chunk_id 하나로 삭제된다.)
        """
        where: dict = {"submitted_by": str(submitted_by)}
        if room_label:
            # 두 조건을 모두 만족(AND). ChromaDB는 $and로 여러 조건을 묶는다.
            where = {"$and": [{"submitted_by": str(submitted_by)}, {"room_label": room_label}]}
        result = self._collection.get(
            where=where,
            include=["documents", "metadatas"],
        )
        items = [
            {
                "chunk_id": cid,
                "title": meta.get("title", ""),
                "text": doc,
                "room_label": meta.get("room_label", ""),
                "msg_date": meta.get("msg_date", "") or meta.get("created_at", ""),
            }
            for cid, doc, meta in zip(
                result["ids"], result["documents"], result["metadatas"]
            )
        ]
        items.sort(key=lambda x: x["msg_date"], reverse=True)
        return items[:limit]

    def delete_by_ids(self, chunk_ids: list[str]) -> int:
        """chunk_id로 직접 지운다. "내 저장 관리"의 삭제에 쓴다."""
        if not chunk_ids:
            return 0
        self._collection.delete(ids=chunk_ids)
        logger.info("조각 %d개 삭제 (id 지정)", len(chunk_ids))
        return len(chunk_ids)

    def delete_document(self, title: str, source: str | None = None) -> int:
        """문서 하나에 딸린 조각을 **전부** 지운다. 지운 조각 수를 돌려준다.

        조각이 하나라도 남으면 검색에 계속 걸리므로 부분 삭제는 의미가 없다.
        제목이 같은 문서가 출처별로 있을 수 있어 source로 좁힐 수 있게 둔다.

        되돌릴 수 없다. 특히 카카오워크 문서는 재수집할 방법이 없어서
        (대화·파일 조회 API가 없다) 지우면 영구 소실이다.
        """
        matched = [
            document
            for document in self.list_documents(source)
            if document.title == title
        ]
        if not matched:
            logger.warning("삭제할 문서를 찾지 못했습니다: %s", title)
            return 0

        chunk_ids = [cid for document in matched for cid in document.chunk_ids]
        self._collection.delete(ids=chunk_ids)
        logger.info("문서 '%s' 삭제: 조각 %d개", title, len(chunk_ids))
        return len(chunk_ids)

    def delete_source(self, source: str) -> int:
        """한 출처의 문서를 전부 지운다. 지운 조각 수를 돌려준다.

        카카오워크로 제출된 내용만 통째로 비우고 싶을 때 쓴다.
        """
        result = self._collection.get(where={"source": source}, include=[])
        chunk_ids = result["ids"]
        if not chunk_ids:
            logger.info("출처 '%s'에 지울 것이 없습니다.", source)
            return 0

        self._collection.delete(ids=chunk_ids)
        logger.info("출처 '%s' 삭제: 조각 %d개", source, len(chunk_ids))
        return len(chunk_ids)

    def delete_source_where_room(self, room_label: str) -> int:
        """한 채팅방의 메시지를 전부 지운다. 지운 조각 수를 돌려준다.

        메시지마다 문서가 되어 제목이 제각각이므로, 방 단위로 다시 넣기
        전에 그 방을 비우려면 room_label로 지운다.
        """
        result = self._collection.get(where={"room_label": room_label}, include=[])
        chunk_ids = result["ids"]
        if not chunk_ids:
            return 0

        self._collection.delete(ids=chunk_ids)
        logger.info("방 '%s' 삭제: 조각 %d개", room_label, len(chunk_ids))
        return len(chunk_ids)

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
