"""엑셀 노션_표 시트를 노션 표 DB에 채운다.

  업무명   → 이름(title)
  프로젝트 → 프로젝트명(select)
  카테고리 → 카테고리(select)
  담당자   → 담당자(select)
  상태     → 상태(select)
  우선순위 → 우선순위(select)
  시작일/종료일 → 날짜(date, 같으면 종료일도 그대로 넣음 — 지금 형식 유지)
  비고     → 페이지 본문
  No·페이지ID·상위페이지 → 제외

  python fill_notion_table.py --db <DB_ID>
  python fill_notion_table.py --db <DB_ID> --limit 1
  python fill_notion_table.py --db <DB_ID> --keep   (기존 행 삭제 안 함)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from openpyxl import load_workbook

KEY_NAME = "NOTION_PRIVATESPACE_API"
XLSX = r"C:\Users\마준서\Documents\카카오톡 받은 파일\디지털트윈_일정_교차검증_테스트데이터셋_1.xlsx"

SELECT_PROPS = ["프로젝트명", "카테고리", "담당자", "상태", "우선순위"]
# (엑셀 열 → 노션 속성) select 매핑
SELECT_MAP = {
    "프로젝트": "프로젝트명",
    "카테고리": "카테고리",
    "담당자": "담당자",
    "상태": "상태",
    "우선순위": "우선순위",
}


def _load_key() -> str:
    for line in (Path(__file__).parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and line.split("=", 1)[0].strip().lower() == KEY_NAME.lower():
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"[X] .env에 {KEY_NAME}가 없습니다.")


BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {_load_key()}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def ensure_properties(db_id: str) -> None:
    current = httpx.get(f"{BASE}/databases/{db_id}", headers=HEADERS, timeout=15).json()
    existing = current.get("properties", {})
    changes: dict = {}
    for name in SELECT_PROPS:
        if existing.get(name, {}).get("type") != "select":
            changes[name] = {"select": {}}
    if "날짜" not in existing:
        changes["날짜"] = {"date": {}}
    if "비고" not in existing:
        changes["비고"] = {"rich_text": {}}
    if changes:
        r = httpx.patch(
            f"{BASE}/databases/{db_id}", headers=HEADERS, json={"properties": changes}, timeout=15
        )
        print(f"속성 준비: {list(changes)}" if r.status_code == 200 else f"[X] {r.text[:120]}")
    else:
        print("속성 이미 준비됨.")


def clear_rows(db_id: str) -> int:
    removed = 0
    while True:
        q = httpx.post(f"{BASE}/databases/{db_id}/query", headers=HEADERS, timeout=15).json()
        results = q.get("results", [])
        if not results:
            break
        for page in results:
            httpx.patch(f"{BASE}/pages/{page['id']}", headers=HEADERS, json={"archived": True}, timeout=15)
            removed += 1
        if not q.get("has_more"):
            break
    return removed


def _v(row: dict, col: str) -> str:
    x = row.get(col)
    return "" if x in (None, "None") else str(x)


def add_row(db_id: str, row: dict) -> tuple[bool, str]:
    name = _v(row, "업무명") or "제목 없음"
    props: dict = {"이름": {"title": [{"text": {"content": name}}]}}
    for excel_col, notion_prop in SELECT_MAP.items():
        val = _v(row, excel_col)
        if val:
            props[notion_prop] = {"select": {"name": val}}
    if _v(row, "시작일"):
        d: dict = {"start": _v(row, "시작일")}
        if _v(row, "종료일"):
            d["end"] = _v(row, "종료일")
        props["날짜"] = {"date": d}

    if _v(row, "비고"):
        props["비고"] = {"rich_text": [{"text": {"content": _v(row, "비고")}}]}

    body = {"parent": {"database_id": db_id}, "properties": props}
    r = httpx.post(f"{BASE}/pages", headers=HEADERS, json=body, timeout=20)
    if r.status_code == 200:
        return True, name
    return False, f"{name}: {r.json().get('message', r.text)[:120]}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    db_id = args.db.split("?")[0].replace("-", "")
    ensure_properties(db_id)
    if not args.keep:
        print(f"기존 행 {clear_rows(db_id)}개 정리")

    wb = load_workbook(XLSX, data_only=True, read_only=True)
    rows = list(wb["노션_표"].iter_rows(values_only=True))
    header = [str(h).strip() if h else "" for h in rows[0]]
    records = [dict(zip(header, r)) for r in rows[1:]]
    if args.limit:
        records = records[: args.limit]

    print(f"\n{len(records)}건 삽입 시작...\n")
    ok = 0
    for row in records:
        success, msg = add_row(db_id, row)
        if success:
            ok += 1
        else:
            print(f"  [X] {msg}")
    print(f"\n완료: {ok}/{len(records)}건 삽입")


if __name__ == "__main__":
    main()
