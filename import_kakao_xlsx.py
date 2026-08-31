"""카카오워크 대화 로그(xlsx)를 방별로 벡터 DB에 적재한다.

카카오워크에는 대화를 읽어오는 API가 없어(kakao_service 참고) 봇이 자동
수집을 못 한다. 그래서 대화 로그를 내보낸 엑셀을 사람이 직접 넣는다.
테스트 데이터셋 검증과, 실제 운영에서 특정 방 대화를 한 번에 적재할 때 쓴다.

    python import_kakao_xlsx.py "<파일.xlsx>" --list
        시트와 채팅방 목록만 보여준다 (적재하지 않음)

    python import_kakao_xlsx.py "<파일.xlsx>"
        모든 방을 각각 문서로 적재한다

    python import_kakao_xlsx.py "<파일.xlsx>" --room "물류DT 프로젝트방"
        그 방만 적재한다 (부서장방·매니저방처럼 골라 넣을 때)

    python import_kakao_xlsx.py "<파일.xlsx>" --sheet 카카오워크_대화 --replace
        먼저 같은 방의 기존 데이터를 지우고 새로 넣는다

--- 방을 문서 단위로 묶는 이유 -------------------------------------

메시지 하나를 문서로 하면 "넵!" 같은 잡담까지 168개가 각각 임베딩되어
비용과 노이즈만 는다. 방 하나를 한 문서로 묶되 각 줄에 [날짜] 발신자를
남기면, 300자 청킹으로 잘려도 날짜가 본문에 살아 있어 일정 교차검증이
가능하다. room_label 메타데이터로 방별 검색·삭제도 된다.

--- 시트 형식 ------------------------------------------------------

헤더: No | 대화ID | 채팅방 | 날짜시간 | 발신자 | 메시지내용
이 순서를 헤더 이름으로 찾으므로 열 순서가 달라도 동작한다.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from app.core.config import setup_cli_logging
from app.services import kakao_service
from app.services.vector_store import VectorStore

DEFAULT_SHEET = "카카오워크_대화"

# 헤더 이름 → 우리가 쓰는 키. 별칭을 둬서 형식이 조금 달라도 잡는다.
COLUMN_ALIASES = {
    "room": ("채팅방", "방", "대화방", "room"),
    "when": ("날짜시간", "일시", "시간", "date"),
    "sender": ("발신자", "작성자", "보낸사람", "sender"),
    "message": ("메시지내용", "내용", "메시지", "message", "text"),
}


def _resolve_columns(header: tuple) -> dict[str, int]:
    """헤더 행에서 필요한 열의 위치를 찾는다."""
    names = [str(h).strip() if h is not None else "" for h in header]
    columns: dict[str, int] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for i, name in enumerate(names):
            if name in aliases:
                columns[key] = i
                break
    missing = [k for k in ("room", "message") if k not in columns]
    if missing:
        raise SystemExit(
            f"[X] 시트에서 필수 열을 찾지 못했습니다: {missing}\n"
            f"    헤더: {names}\n"
            f"    '채팅방'과 '메시지내용'에 해당하는 열이 있어야 합니다."
        )
    return columns


def _read_rooms(path: Path, sheet: str) -> dict[str, list[tuple[str, str, str]]]:
    """방 이름 → [(날짜, 발신자, 메시지), ...] 로 모은다."""
    workbook = load_workbook(path, data_only=True, read_only=True)
    if sheet not in workbook.sheetnames:
        raise SystemExit(
            f"[X] '{sheet}' 시트가 없습니다. 있는 시트: {workbook.sheetnames}"
        )

    rows = list(workbook[sheet].iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"[X] '{sheet}' 시트가 비어 있습니다.")

    cols = _resolve_columns(rows[0])
    rooms: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    def cell(row: tuple, key: str) -> str:
        i = cols.get(key)
        if i is None or i >= len(row) or row[i] is None:
            return ""
        return str(row[i]).strip()

    for row in rows[1:]:
        room = cell(row, "room")
        message = cell(row, "message")
        if not room or not message:
            continue
        rooms[room].append((cell(row, "when"), cell(row, "sender"), message))

    return rooms


def _message_text(when: str, sender: str, message: str) -> str:
    """메시지 한 건을 '[날짜] 발신자: 내용' 한 줄로 만든다."""
    prefix = f"[{when}] " if when else ""
    who = f"{sender}: " if sender else ""
    return f"{prefix}{who}{message}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="카카오워크 대화 xlsx를 방별로 벡터 DB에 적재합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", help="xlsx 파일 경로")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help=f"시트 이름 (기본: {DEFAULT_SHEET})")
    parser.add_argument("--room", action="append", metavar="이름", help="이 방만 적재 (여러 번 가능)")
    parser.add_argument("--list", action="store_true", help="방 목록만 보고 적재하지 않음")
    parser.add_argument("--replace", action="store_true", help="같은 방의 기존 데이터를 지우고 새로")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"[X] 파일이 없습니다: {path}")

    setup_cli_logging()
    rooms = _read_rooms(path, args.sheet)

    print(f"\n'{args.sheet}' 시트에서 방 {len(rooms)}개 발견:")
    for name, lines in sorted(rooms.items(), key=lambda kv: len(kv[1]), reverse=True):
        print(f"  {name:20} {len(lines):>4}건")

    if args.list:
        return

    targets = args.room or list(rooms)
    unknown = [r for r in targets if r not in rooms]
    if unknown:
        raise SystemExit(f"\n[X] 그런 방이 없습니다: {unknown}")

    store = VectorStore()
    if args.replace:
        # 메시지마다 문서가 되어 제목이 제각각이라, 방 단위로 지우려면
        # room_label로 지운다.
        for name in targets:
            removed = store.delete_source_where_room(name)
            if removed:
                print(f"  [{name}] 기존 조각 {removed}개 삭제")

    print()
    total = 0
    for name in targets:
        lines = rooms[name]
        # 메시지 한 건 = 문서 한 건 = 조각 한 개. 방은 room_label로 구분한다.
        # 방을 통째로 묶지 않으므로, 검색·삭제가 메시지 단위로 정밀해진다.
        documents = []
        for i, (when, sender, message) in enumerate(lines):
            documents.append(
                kakao_service.build_document(
                    text=_message_text(when, sender, message),
                    # 같은 방에서 제목이 겹치면 chunk_id가 겹쳐 덮어써진다.
                    # 방 이름 + 순번 + 시각으로 유일하게 만든다.
                    title=f"{name} #{i} ({when})",
                    created_at=when,
                    room_label=name,
                )
            )
        chunks = kakao_service.ingest_documents(documents)
        print(f"  [{name}] 메시지 {len(lines)}건 → 문서 {len(documents)}건 / 조각 {chunks}개")
        total += chunks

    print(f"\n완료. 방 {len(targets)}개, 조각 {total}개를 넣었습니다.")
    print("확인:  python manage_docs.py --list --source kakaowork")


if __name__ == "__main__":
    main()
