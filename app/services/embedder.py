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

from app.models.schemas import Chunk, Document

# sentence_transformers는 최상단에서 import하지 않는다. import만으로 torch가
# 딸려 올라와 프로세스 메모리가 약 390MB 늘어나기 때문이다(실측). 벡터 검색을
# 쓰지 않는 배포(USE_VECTOR_SEARCH=false)에서는 그 비용을 낼 이유가 없다.
# 실제 임베딩이 필요한 _load_model() 안에서만 불러온다.
# 파일 상단의 `from __future__ import annotations` 덕에 타입 주석은 문자열로
# 남으므로 import를 미뤄도 주석이 깨지지 않는다.

logger = logging.getLogger(__name__)

MODEL_NAME = "jhgan/ko-sroberta-multitask"
CHUNK_SIZE = 300
CHUNK_OVERLAP = 50


def _split_long_line(line: str) -> list[str]:
    """300자를 넘는 한 줄을 어쩔 수 없이 글자 수로 자른다.

    자르는 간격(step)은 300 - 50 = 250자다. 그래서 0~300, 250~550, 500~800...
    으로 250자씩 전진하면서 매번 300자를 떠낸다. 문장 중간이 잘리므로,
    경계에 걸친 문장이 사라지지 않도록 50자를 겹쳐 둔다.
    """
    step = CHUNK_SIZE - CHUNK_OVERLAP
    pieces: list[str] = []

    for start in range(0, len(line), step):
        piece = line[start : start + CHUNK_SIZE]
        if piece.strip():
            pieces.append(piece)
        # 마지막 조각까지 떠냈으면 멈춘다. 안 그러면 끝에서 짧은 꼬리가 계속 생긴다.
        if start + CHUNK_SIZE >= len(line):
            break

    return pieces


def _split_into_lines(text: str) -> list[str]:
    """본문을 줄 단위로 나눈다. 300자가 넘는 줄만 더 잘게 쪼갠다."""
    lines: list[str] = []

    for line in text.split("\n"):
        if not line.strip():
            continue
        if len(line) <= CHUNK_SIZE:
            lines.append(line)
        else:
            lines.extend(_split_long_line(line))

    return lines


def _overlap_tail(lines: list[str]) -> tuple[list[str], int]:
    """다음 조각 앞에 다시 붙일 꼬리 줄들을 고른다. 합쳐서 50자 이내.

    표의 한 행처럼 50자가 넘는 줄은 통째로 못 넣으므로 겹침 없이 넘어간다.
    줄 경계에서만 자르므로 어차피 문장이 잘릴 일이 없어 손해가 아니다.
    """
    tail: list[str] = []
    total = 0

    for line in reversed(lines):
        added = len(line) + (1 if tail else 0)
        if total + added > CHUNK_OVERLAP:
            break
        tail.insert(0, line)
        total += added

    return tail, total


def chunk_document(document: Document) -> list[Chunk]:
    """문서 하나를 300자 이내 조각으로 자른다. **줄을 쪼개지 않는다.**

    **왜 줄 단위인가**: 예전에는 글자 수만 세서 잘랐다. 그러면 표의 한 행이
    두 조각으로 갈라져 "년 | 연락처: ... | 담당: Slack" 같은 반토막이 남는다.
    이런 조각은 누구 이야기인지 알 수 없어 검색에 걸려도 쓸모가 없고,
    Claude에 넘어가면 근거를 잘못 읽는다.

    그래서 줄 경계에서만 끊는다. 300자를 넘는 긴 문단 한 줄은 어쩔 수 없이
    글자 수로 자르되(`_split_long_line`), 그때만 50자를 겹친다.

    Args:
        document: 수집 단계에서 만든 문서

    Returns:
        Chunk 목록. 300자 이하의 짧은 문서는 자르지 않고 1개만 돌려준다.
    """
    text = document.text

    if len(text) <= CHUNK_SIZE:
        return [Chunk.from_document(document, text=text, index=0)]

    pieces: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in _split_into_lines(text):
        added = len(line) + (1 if current else 0)  # +1은 줄바꿈 문자

        if current and current_len + added > CHUNK_SIZE:
            pieces.append("\n".join(current))
            current, current_len = _overlap_tail(current)
            # 꼬리를 남기면 이번 줄이 또 안 들어가는 경우엔 꼬리를 버린다.
            # 안 그러면 같은 줄을 영원히 못 넣고 맴돈다.
            if current and current_len + len(line) + 1 > CHUNK_SIZE:
                current, current_len = [], 0
            added = len(line) + (1 if current else 0)

        current.append(line)
        current_len += added

    if current:
        pieces.append("\n".join(current))

    return [
        Chunk.from_document(document, text=piece, index=index)
        for index, piece in enumerate(pieces)
    ]


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
    from sentence_transformers import SentenceTransformer

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
