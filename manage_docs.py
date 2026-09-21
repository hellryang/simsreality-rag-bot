"""벡터 DB 관리 도구.

무엇이 들어 있는지 보고, 잘못 들어간 문서를 지운다.

    python manage_docs.py --list                    전체 문서 목록
    python manage_docs.py --list --source kakaowork 카카오워크 것만
    python manage_docs.py --delete "<제목>"          그 문서 삭제
    python manage_docs.py --delete-source kakaowork 그 출처 전부 삭제

--- 왜 필요한가 ------------------------------------------------------

사용자가 봇에 제출한 내용은 즉시 검색 대상이 된다. 검토 단계가 없으므로
잘못된 내용이 들어가면 그때부터 답변을 오염시킨다. 그런데 지금까지
VectorStore에는 reset()밖에 없어서, 문서 하나를 빼려면 Notion·Slack까지
전부 날리는 수밖에 없었다.

--- 주의 ------------------------------------------------------------

카카오워크 문서는 지우면 끝이다. KakaoWork에는 대화·파일을 읽어오는 API가
없어서 재수집이 구조적으로 불가능하다(kakao_service.collect_kakao_documents).
Notion·Slack은 build_db.py로 다시 채울 수 있다.
"""
from __future__ import annotations

import argparse
import sys

from app.core.config import setup_cli_logging
from app.models.schemas import StoredDocument
from app.services.vector_store import VectorStore

# 지우면 되돌릴 수 없는 출처. 삭제 전에 한 번 더 묻는다.
UNRECOVERABLE = {"kakaowork"}


def print_documents(documents: list[StoredDocument]) -> None:
    if not documents:
        print("저장된 문서가 없습니다.")
        return

    print(f"\n{'source':11} {'조각':>4}  {'제출자':10} 제목")
    print("─" * 88)
    for document in documents:
        print(
            f"{document.source:11} {document.chunk_count:>4}  "
            f"{document.submitted_by or '-':10} {document.title[:45]}"
        )
    print("─" * 88)

    total = sum(d.chunk_count for d in documents)
    print(f"문서 {len(documents)}건, 조각 {total}개")


def find_documents(
    store: VectorStore, needle: str, source: str | None = None
) -> list[StoredDocument]:
    """삭제 대상을 찾는다. 정확히 일치하는 것이 있으면 그것만.

    제목에 타임스탬프가 들어가는 문서가 있어서
    (`카카오워크 대화 (2026-08-22T05:52:17+00:00)`) 정확히 타이핑하는 것은
    현실적이지 않다. 그래서 정확히 일치하는 것이 없을 때만 부분 일치로
    넓힌다. 정확한 제목을 준 사람이 비슷한 다른 문서까지 지우는 사고를
    막으려면 정확 일치가 우선이어야 한다.

    부분 일치는 여러 건이 걸릴 수 있으므로 호출부에서 목록을 보여주고
    확인을 받는다.
    """
    documents = store.list_documents(source)

    exact = [d for d in documents if d.title == needle]
    if exact:
        return exact

    return [d for d in documents if needle in d.title]


def confirm(prompt: str) -> bool:
    """되돌릴 수 없는 삭제 전에 확인을 받는다."""
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="벡터 DB에 무엇이 들어 있는지 보고, 문서를 삭제합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--list", action="store_true", help="문서 목록 출력")
    parser.add_argument("--source", metavar="NAME", help="notion / slack / kakaowork")
    parser.add_argument(
        "--delete", metavar="TITLE", help="제목이 일치(또는 포함)하는 문서 삭제"
    )
    parser.add_argument(
        "--delete-source", metavar="NAME", help="그 출처의 문서를 전부 삭제"
    )
    parser.add_argument("--yes", action="store_true", help="확인 절차를 건너뜀")
    args = parser.parse_args()

    if not (args.list or args.delete or args.delete_source):
        parser.print_help()
        sys.exit(1)

    setup_cli_logging()
    store = VectorStore()

    if args.delete:
        target = find_documents(store, args.delete, args.source)
        if not target:
            print(f"[X] 일치하는 문서가 없습니다: {args.delete}")
            print("    python manage_docs.py --list 로 제목을 확인하세요.")
            sys.exit(1)

        print("\n삭제 대상:")
        print_documents(target)
        if any(d.source in UNRECOVERABLE for d in target):
            print("\n[!] 카카오워크 문서는 재수집할 수 없습니다. 지우면 영구 소실입니다.")
        if not args.yes and not confirm(f"위 {len(target)}건을 삭제할까요?"):
            print("취소했습니다.")
            return

        removed = sum(store.delete_document(d.title, d.source) for d in target)
        print(f"[O] 문서 {len(target)}건, 조각 {removed}개를 삭제했습니다.")

    if args.delete_source:
        documents = store.list_documents(args.delete_source)
        if not documents:
            print(f"출처 '{args.delete_source}'에 지울 것이 없습니다.")
            return

        print_documents(documents)
        if args.delete_source in UNRECOVERABLE:
            print(f"\n[!] '{args.delete_source}'는 재수집할 수 없습니다. 지우면 영구 소실입니다.")
        if not args.yes and not confirm(f"'{args.delete_source}' 전체를 삭제할까요?"):
            print("취소했습니다.")
            return

        removed = store.delete_source(args.delete_source)
        print(f"[O] 조각 {removed}개를 삭제했습니다.")

    if args.list:
        print_documents(store.list_documents(args.source))


if __name__ == "__main__":
    main()
