"""노션 캘린더/DB에 데이터를 쓸 수 있는지 검증하는 임시 스크립트.

멘토 요청 2번(대화 정리 내용을 노션 캘린더/표에 삽입) "가능 여부"를
확인하기 위한 것. 실제 프로젝트 코드가 아니라, 개인 테스트 DB에 행 하나를
넣어보고 되는지만 본다. 확인이 끝나면 이 파일은 지워도 된다.

    python test_notion_write.py --db <DB_ID>
        그 DB의 속성을 조회하고, 테스트 행을 하나 넣어본다.

키는 .env의 NOTION_API_KEY에서 읽는다(화면에 출력하지 않는다).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

# .env를 직접 읽는다(pydantic 설정을 거치지 않아, 프로젝트에 정의 안 된
# 테스트용 키 이름도 쓸 수 있다).
KEY_NAME = "NOTION_PRIVATESPACE_API"


def _load_key() -> str:
    for line in (Path(__file__).parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        # 이름의 공백·대소문자를 무시하고 맞춘다(.env에 'Notion_PrivateSpace_API '
        # 처럼 공백·대소문자가 섞여 들어갈 수 있다).
        if name.strip().lower() == KEY_NAME.lower():
            return value.strip().strip("'\"")
    raise SystemExit(f"[X] .env에 {KEY_NAME}가 없습니다.")


BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {_load_key()}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def inspect_db(db_id: str) -> dict:
    """DB의 속성(칼럼) 구조를 본다. 어떤 이름·타입인지 알아야 행을 넣는다."""
    r = httpx.get(f"{BASE}/databases/{db_id}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"[X] DB 조회 실패 (status={r.status_code})")
        print("    ", r.json().get("message", r.text)[:200])
        _hint(r.status_code)
        raise SystemExit(1)

    data = r.json()
    props = data.get("properties", {})
    print(f"[O] DB 조회 성공: {_plain(data.get('title'))}")
    print("    속성(칼럼):")
    for name, spec in props.items():
        print(f"      - {name}  (타입: {spec.get('type')})")
    return props


def add_test_row(db_id: str, props: dict) -> None:
    """DB 속성에 맞춰 테스트 행 하나를 넣는다.

    제목(title) 속성과 날짜(date) 속성을 찾아 값을 채운다. 다른 속성은
    비워 둔다(필수가 아니면 통과).
    """
    title_name = next((n for n, s in props.items() if s.get("type") == "title"), None)
    date_name = next((n for n, s in props.items() if s.get("type") == "date"), None)

    if not title_name:
        print("[X] title 타입 속성이 없습니다. 노션 DB에 제목 칼럼이 있어야 합니다.")
        raise SystemExit(1)

    new_props: dict = {
        title_name: {"title": [{"text": {"content": "테스트 일정 (API 삽입 확인)"}}]}
    }
    if date_name:
        new_props[date_name] = {"date": {"start": "2026-09-12"}}

    body = {"parent": {"database_id": db_id}, "properties": new_props}
    r = httpx.post(f"{BASE}/pages", headers=HEADERS, json=body, timeout=15)

    if r.status_code == 200:
        print("\n[O] 테스트 행 삽입 성공! 노션 캘린더에 '테스트 일정'이 추가됐을 것입니다.")
        print(f"    → 노션 캘린더/표에 데이터 쓰기 가능 확인됨.")
        print(f"    (넣은 값: 제목='테스트 일정', 날짜={'2026-09-12' if date_name else '없음'})")
    else:
        print(f"\n[X] 행 삽입 실패 (status={r.status_code})")
        print("    ", r.json().get("message", r.text)[:200])
        _hint(r.status_code)


def _hint(status: int) -> None:
    if status == 401:
        print("    → 키가 틀렸습니다. .env의 NOTION_API_KEY를 확인하세요.")
    elif status == 403:
        print("    → 쓰기 권한이 없습니다. Integration 설정에서")
        print("      'Insert content(콘텐츠 삽입)' 기능을 켰는지 확인하세요.")
    elif status == 404:
        print("    → DB를 못 찾았습니다. 둘 중 하나입니다:")
        print("      1) DB ID가 틀림 (?v= 앞부분만 써야 함)")
        print("      2) 그 DB에 Integration을 '연결'하지 않음")
        print("         → 노션 DB 페이지 ⋯ → 연결 → Integration 추가")


def _plain(title) -> str:
    if isinstance(title, list):
        return "".join(t.get("plain_text", "") for t in title) or "(제목 없음)"
    return str(title)


def main() -> None:
    parser = argparse.ArgumentParser(description="노션 DB 쓰기 가능 여부 검증")
    parser.add_argument("--db", required=True, help="캘린더 DB ID (?v= 앞부분)")
    args = parser.parse_args()

    db_id = args.db.split("?")[0].replace("-", "")
    print(f"검증 대상 DB: {db_id}\n")
    props = inspect_db(db_id)
    add_test_row(db_id, props)


if __name__ == "__main__":
    main()
