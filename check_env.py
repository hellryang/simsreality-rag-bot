"""`.env` 점검 도구.

키 값은 **절대 화면에 출력하지 않는다.** 자리표시자인지, 형식이 맞는지만 알려준다.
캡처를 팀 채널에 올려도 안전하도록 만든 것이다.

    python check_env.py                형식만 점검 (외부 API 호출 없음, 즉시 끝남)
    python check_env.py --notion       Notion에 실제로 접속해서 확인
    python check_env.py --id "<주소>"   Notion 주소에서 DATABASE_ID만 뽑아내기
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"

# (환경변수명, 설명, 형식검사, 오늘 데모에 필요한가)
CHECKS: list[tuple[str, str, re.Pattern[str] | None, bool]] = [
    ("NOTION_API_KEY",       "Notion Integration 시크릿",  re.compile(r"^(ntn_|secret_)\S{20,}$"), True),
    ("NOTION_DATABASE_ID",   "수집할 Notion DB의 ID",      re.compile(r"^[0-9a-fA-F-]{32,36}$"),   True),
    ("ANTHROPIC_API_KEY",    "Claude API 키",              re.compile(r"^sk-ant-\S{20,}$"),        False),
    ("ANTHROPIC_MODEL",      "사용할 Claude 모델명",        None,                                   False),
    ("SLACK_BOT_TOKEN",      "Slack 봇 토큰",               re.compile(r"^xoxb-\S{10,}$"),          False),
    ("SLACK_SIGNING_SECRET", "Slack 서명 검증용 시크릿",     re.compile(r"^[0-9a-f]{32}$"),          False),
    ("SLACK_APP_TOKEN",      "Slack 앱 토큰 (선택)",        re.compile(r"^xapp-\S{10,}$"),          False),
    ("KAKAOWORK_APP_KEY",    "KakaoWork 앱 키",             None,                                   False),
    ("CHROMA_PERSIST_DIR",   "벡터 DB 저장 폴더",            None,                                   False),
]


def load_env_file() -> dict[str, str]:
    """`.env`를 직접 읽는다. pydantic을 거치지 않아 한 줄이 틀려도 전체가 죽지 않는다."""
    if not ENV_PATH.exists():
        print(f"[!] {ENV_PATH} 파일이 없습니다.")
        print("    copy .env.example .env  로 만드세요.")
        sys.exit(1)

    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # 따옴표로 감싸는 사람이 많은데, 그러면 값에 따옴표가 그대로 들어간다.
        values[key.strip()] = value.strip().strip("'\"")
    return values


def is_placeholder(value: str) -> bool:
    """.env.example에서 복사만 하고 안 고친 상태인지 판단한다."""
    return not value or value.endswith("xxx") or set(value) <= set("x")


def report_format(values: dict[str, str]) -> bool:
    """형식 점검 결과를 출력하고, 오늘 데모에 필요한 값이 다 찼는지 돌려준다."""
    print(f"\n{'환경변수':24} {'상태':22} 설명")
    print("─" * 78)

    demo_ready = True
    for name, desc, pattern, needed_today in CHECKS:
        value = values.get(name, "")
        mark = " ←오늘 필요" if needed_today else ""

        if name not in values:
            status = "[X] 줄 자체가 없음"
        elif is_placeholder(value):
            status = "[ ] 자리표시자 그대로"
        elif pattern and not pattern.match(value):
            status = "[!] 형식이 이상함"
        else:
            status = "[O] 채워짐"

        if needed_today and not status.startswith("[O]"):
            demo_ready = False

        print(f"{name:24} {status:22} {desc}{mark}")

    print("─" * 78)
    return demo_ready


def check_notion(values: dict[str, str]) -> None:
    """Notion에 실제로 붙어본다. 실패 원인을 구분해서 알려주는 것이 핵심이다."""
    import asyncio

    from notion_client import AsyncClient
    from notion_client.errors import APIResponseError

    async def _run() -> None:
        client = AsyncClient(auth=values["NOTION_API_KEY"])
        try:
            response = await client.databases.query(
                database_id=values["NOTION_DATABASE_ID"], page_size=1
            )
            count = len(response.get("results", []))
            print(f"\n[O] Notion 접속 성공. 페이지 {count}건 조회됨.")
            if count == 0:
                print("    다만 DB가 비어 있습니다. 수집할 문서를 몇 개 넣어두세요.")
        except APIResponseError as exc:
            print(f"\n[X] Notion 응답 실패 (status={exc.status})")
            if exc.status == 401:
                print("    → 키가 틀렸습니다. NOTION_API_KEY를 다시 확인하세요.")
                print("      https://www.notion.so/profile/integrations 에서 재발급 가능")
            elif exc.status == 404:
                print("    → DB를 못 찾았습니다. 원인은 둘 중 하나입니다:")
                print("      1) NOTION_DATABASE_ID가 틀림")
                print("      2) 그 DB가 Integration과 '연결'되지 않음  ← 대부분 이것")
                print("      해결: Notion에서 대상 DB 열기 → 우측 상단 ⋯ → 연결 → 우리 Integration 추가")
            elif exc.status == 403:
                print("    → 권한이 없습니다. DB를 Integration에 연결했는지 확인하세요.")
                print("      Notion에서 대상 DB 열기 → 우측 상단 ⋯ → 연결 → 우리 Integration 추가")
            elif exc.status == 400:
                print("    → 요청 형식 오류. DATABASE_ID가 페이지 ID일 수 있습니다.")
                print("      또는 notion-client 버전과 API 사양이 안 맞을 수 있으니")
                print("      https://developers.notion.com 의 Query a database 문서를 확인하세요.")
        finally:
            await client.aclose()

    asyncio.run(_run())


def extract_database_id(url: str) -> str | None:
    """Notion 주소에서 데이터베이스 ID(32자리)를 뽑아낸다.

    주소는 보통 이렇게 생겼다.

        https://www.notion.so/myteam/회의록-1a2b3c4d...7890?v=9f8e7d...

    `?v=` 뒤에도 32자리 값이 하나 더 붙는데 그건 '보기(view) ID'라서 쓰면 안 된다.
    그래서 물음표 뒤를 먼저 잘라내고 찾는다. 여기서 틀리는 경우가 가장 많다.
    """
    path = url.split("?")[0]
    matches = re.findall(
        r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}",
        path,
    )
    if not matches:
        return None
    return matches[-1].replace("-", "")


def run_id_extraction(argv: list[str]) -> None:
    """`--id <주소>` 처리."""
    index = argv.index("--id")
    if index + 1 >= len(argv):
        print('사용법: python check_env.py --id "여기에 Notion 주소 붙여넣기"')
        print("주소에 특수문자가 있으므로 반드시 큰따옴표로 감싸세요.")
        return

    url = argv[index + 1]
    database_id = extract_database_id(url)

    if not database_id:
        print("[X] 주소에서 ID를 찾지 못했습니다.")
        print("    데이터베이스(표)를 '전체 페이지로 열기' 한 뒤의 주소여야 합니다.")
        print("    주소 형태 예: https://www.notion.so/팀이름/제목-32자리ID?v=...")
        return

    print("\n[O] 찾았습니다. `.env`의 해당 줄을 아래처럼 바꾸세요.\n")
    print(f"    NOTION_DATABASE_ID={database_id}\n")
    if "?v=" in url:
        print("    참고: 주소의 ?v= 뒤 값은 '보기 ID'라서 쓰지 않습니다. 위 값이 맞습니다.")


def main() -> None:
    if "--id" in sys.argv:
        run_id_extraction(sys.argv)
        return

    values = load_env_file()
    demo_ready = report_format(values)

    if not demo_ready:
        print("\n오늘 Notion 데모에 필요한 값이 아직 안 찼습니다.")
        print("`.env`를 열어 NOTION_API_KEY와 NOTION_DATABASE_ID부터 채우세요.")
        print("  notepad .env")
        return

    print("\n오늘 데모에 필요한 값은 다 찼습니다.")
    if "--notion" in sys.argv:
        check_notion(values)
    else:
        print("실제 접속까지 확인하려면:  python check_env.py --notion")


if __name__ == "__main__":
    main()
