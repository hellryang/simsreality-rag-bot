"""청킹 + 임베딩.

임베딩 모델: jhgan/ko-sroberta-multitask (chunk 300 / overlap 50).

**청킹(chunking)**: 긴 문서를 검색 단위로 잘게 자르는 것.
문서 한 편을 통째로 벡터 하나로 만들면 온갖 주제가 한 점에 뭉쳐서
"배포 일정" 같은 구체적인 질문에 엉뚱한 문서가 걸린다.

**겹침(overlap)**: 조각 경계에 걸친 문장이 잘려 검색이 안 되는 걸 막으려고
앞 조각의 끝부분을 뒤 조각의 앞부분에 다시 넣는 것.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from app.models.schemas import Chunk, Document

logger = logging.getLogger(__name__)

MODEL_NAME = "jhgan/ko-sroberta-multitask"
CHUNK_SIZE = 300
CHUNK_OVERLAP = 50


def chunk_document(document: Document) -> list[Chunk]:
    """문서 하나를 300자 조각으로 자른다. 이웃한 조각은 50자를 공유한다.

    자르는 간격(step)은 300 - 50 = 250자다. 그래서 0~300, 250~550, 500~800...
    으로 250자씩 전진하면서 매번 300자를 떠낸다.

    Args:
        document: 수집 단계에서 만든 문서

    Returns:
        Chunk 목록. 300자 이하의 짧은 문서는 자르지 않고 1개만 돌려준다.
    """
    text = document.text

    if len(text) <= CHUNK_SIZE:
        return [Chunk.from_document(document, text=text, index=0)]

    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks: list[Chunk] = []

    for start in range(0, len(text), step):
        piece = text[start : start + CHUNK_SIZE]
        if piece.strip():
            chunks.append(Chunk.from_document(document, text=piece, index=len(chunks)))
        # 마지막 조각까지 떠냈으면 멈춘다. 안 그러면 끝에서 짧은 꼬리가 계속 생긴다.
        if start + CHUNK_SIZE >= len(text):
            break

    return chunks


def chunk_documents(documents: list[Document]) -> list[Chunk]:
    """여러 문서를 한 번에 자른다."""
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks


@lru_cache(maxsize=1)
def _load_model() -> SentenceTransformer:
    """임베딩 모델을 한 번만 읽어 재사용한다.

    모델 파일이 약 500MB라 매번 새로 읽으면 몇 초씩 날아간다.
    처음 호출할 때만 내려받고, 그 뒤로는 캐시에서 바로 쓴다.
    """
    logger.info("임베딩 모델 로딩 중... (처음이면 약 500MB 내려받습니다)")
    model = SentenceTransformer(MODEL_NAME)
    # sentence-transformers 5.x에서 get_sentence_embedding_dimension이
    # get_embedding_dimension으로 이름이 바뀌었다. 둘 다 지원한다.
    get_dimension = getattr(model, "get_embedding_dimension", None) or (
        model.get_sentence_embedding_dimension
    )
    logger.info("임베딩 모델 준비 완료 (차원 %d)", get_dimension())
    return model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """문장들을 벡터로 바꾼다.

    **임베딩(embedding)**: 문장을 숫자 배열로 바꿔서 의미가 비슷한 문장끼리
    가까운 위치에 놓이게 하는 것. 이 덕분에 단어가 정확히 겹치지 않아도
    "서버 어디에 올리나요" 로 "배포는 Railway를 쓴다" 를 찾을 수 있다.

    코사인 유사도로 비교할 것이므로 길이를 1로 맞춰(normalize) 돌려준다.
    """
    if not texts:
        return []

    # show_progress_bar를 끄지 않으면 호출할 때마다 "Batches: 100%|..." 막대가
    # 찍혀 실제 출력이 묻힌다. 대량 적재 진행 상황은 build_db.py가 따로 찍는다.
    vectors = _load_model().encode(
        texts, normalize_embeddings=True, show_progress_bar=False
    )
    return [[float(value) for value in vector] for vector in vectors]


def embed_chunks(chunks: list[Chunk]) -> list[list[float]]:
    """Chunk 목록의 본문만 뽑아 한꺼번에 임베딩한다."""
    return embed_texts([chunk.text for chunk in chunks])
