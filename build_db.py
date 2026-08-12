"""Notion 문서를 수집해 벡터 DB에 적재하는 스크립트.

    python build_db.py                 Notion 전체 수집 → 적재
    python build_db.py --limit 3       3건만 (빠른 확인용)
    python build_db.py --reset         기존 데이터를 비우고 새로 적재

지금은 Notion만 넣는다. Slack·KakaoWork 수집이 완성되면 collect_all()에
같은 방식으로 한 줄씩 추가하면 된다. 세 소스는 서로를 거치지 않고
각각 독립적으로 이 벡터 DB에 들어간다.

이 스크립트는 ANTHROPIC_API_KEY 없이 동작한다. 임베딩은 내 컴퓨터에서
실행되므로 Claude를 부르지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.config import settings
from app.models.schemas import Chunk, Document
from app.services.embedder import chunk_documents
from app.services.notion_service import collect_notion_page_tree
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 한 번에 임베딩할 조각 수. 너무 크게 잡으면 메모리를 많이 쓰고,
# 너무 작으면 모델을 반복 호출하느라 느려진다.
BATCH_SIZE = 32


async def collect_all(limit: int | None = None) -> list[Document]:
    """수집 대상 소스를 모두 읽어 Document 목록으로 만든다.

    Slack·KakaoWork가 준비되면 여기에 asyncio.gather로 나란히 붙인다.
    그때 return_exceptions=True를 써서 한 소스가 실패해도 나머지는
    적재되도록 한다.
    """
    if not settings.notion_root_page_id:
        raise SystemExit(
            "[!] .env에 NOTION_ROOT_PAGE_ID가 없습니다.\n"
            '    python check_env.py --id "<Notion 주소>" 로 ID를 뽑아 채우세요.'
        )

    raw = await collect_notion_page_tree(settings.notion_root_page_id, limit=limit)

    documents: list[Document] = []
    for item in raw:
        # 제목만 있고 본문이 빈 페이지는 Document 검증에서 걸리므로 건너뛴다.
        if not item.get("text", "").strip():
            logger.warning("본문이 비어 건너뜁니다: %s", item.get("title"))
            continue
        documents.append(Document(**item))

    return documents


def store_chunks(chunks: list[Chunk], reset: bool = False) -> int:
    """조각들을 벡터 DB에 넣는다. 넣은 개수를 돌려준다.

    조각마다 임베딩(문장 → 숫자 배열) 계산이 필요해서 시간이 걸린다.
    진행 상황이 보이도록 batch 단위로 나눠 로그를 남긴다.
    """
    store = VectorStore()

    if reset:
        store.reset()

    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start : start + BATCH_SIZE]
        store.add(batch)
        logger.info("적재 %d/%d", min(start + BATCH_SIZE, len(chunks)), len(chunks))

    return store.count()


def main() -> None:
    parser = argparse.ArgumentParser(description="Notion 문서를 벡터 DB에 적재한다")
    parser.add_argument("--limit", type=int, default=None, help="가져올 문서 수 제한")
    parser.add_argument("--reset", action="store_true", help="기존 데이터를 비우고 새로")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("\n[1/3] Notion에서 문서를 수집합니다...")
    documents = asyncio.run(collect_all(limit=args.limit))
    print(f"      문서 {len(documents)}건 수집")

    if not documents:
        print("\n[!] 수집된 문서가 없습니다. Notion 페이지에 하위 문서가 있는지 확인하세요.")
        return

    print("\n[2/3] 문서를 조각으로 자릅니다 (300자 / 겹침 50자)...")
    chunks = chunk_documents(documents)
    print(f"      조각 {len(chunks)}개 생성")

    print("\n[3/3] 임베딩 후 벡터 DB에 저장합니다...")
    print("      (처음 실행이면 모델 로딩에 30초쯤 걸립니다)")
    total = store_chunks(chunks, reset=args.reset)

    print(f"\n완료. 벡터 DB에 조각 {total}개가 들어 있습니다.")
    print(f"저장 위치: {settings.chroma_persist_dir}")
    print("\n다음 명령으로 검색을 확인하세요 (Claude 키 없이 됩니다):")
    print('  python -m app.services.qa_pipeline --search "질문을 여기에"')
    print("\n질문은 방금 적재된 문서에 실제로 있는 내용으로 하세요.")
    print("없는 내용을 물으면 유사도 0.4 미만의 엉뚱한 문서가 나옵니다.")
    print("적재된 문서:")
    for title in sorted({d.title for d in documents}):
        print(f"  - {title}")


if __name__ == "__main__":
    main()
