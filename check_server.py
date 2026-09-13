"""배포 서버가 봇을 받을 준비가 됐는지 확인한다.

카카오워크 관리자의 URL을 바꾸기 전에 돌린다. 바꾸고 나서 안 되면
되돌리는 데 또 시간이 걸리고, 그동안 봇이 멈춰 있다.

    python check_server.py https://배포주소

확인하는 것:
  1. 서버가 살아 있는가            /health
  2. 봇 코드가 올라갔는가          POST /kakao/request  ← 가장 중요
  3. 노션 연동 환경변수가 맞는가    캘린더 조회 실제 시도
"""
from __future__ import annotations

import sys

import httpx

from app.core.config import settings

TIMEOUT = 15.0


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "[ OK ]", False: "[FAIL]", None: "[ ?? ]"}[ok]
    print(f"  {mark}  {label}")
    if detail:
        for row in detail.splitlines():
            print(f"          {row}")


def check_health(base: str) -> bool:
    try:
        r = httpx.get(f"{base}/health", timeout=TIMEOUT)
    except httpx.RequestError as exc:
        _line(False, "서버 응답", f"연결 실패: {exc}")
        return False

    ok = r.status_code == 200
    _line(ok, "서버 응답", f"status={r.status_code} body={r.text[:80]}")
    if not ok:
        print("          → 서버가 안 떠 있거나 주소가 틀렸다.")
    return ok


def check_bot_deployed(base: str, token: str) -> bool:
    """봇 코드가 실제로 배포됐는지. develop에는 빈 라우터만 있어서 404가 난다."""
    try:
        r = httpx.post(
            f"{base}/kakao/request",
            params={"token": token} if token else None,
            json={"value": "reserve_schedule"},
            timeout=TIMEOUT,
        )
    except httpx.RequestError as exc:
        _line(False, "봇 코드 배포", f"연결 실패: {exc}")
        return False

    if r.status_code == 404:
        _line(False, "봇 코드 배포", "404 — 봇 코드가 아직 서버에 없다.")
        print("          → feature/kakaowork-adapter 브랜치를 배포해야 한다.")
        return False
    if r.status_code == 401:
        _line(False, "봇 코드 배포", "401 — 코드는 있으나 콜백 토큰이 다르다.")
        print("          → 서버의 KAKAOWORK_CALLBACK_TOKEN을 확인할 것.")
        return False
    if r.status_code != 200:
        _line(False, "봇 코드 배포", f"status={r.status_code} {r.text[:120]}")
        print("          → 환경변수 누락일 가능성이 크다.")
        return False

    view = (r.json() or {}).get("view") or {}
    ok = bool(view.get("blocks"))
    _line(ok, "봇 코드 배포", f"모달 응답: title={view.get('title')!r}")
    return ok


def check_notion() -> bool:
    """노션 쓰기 키와 캘린더 DB가 설정됐는지. 로컬 설정 기준으로 본다."""
    missing = [
        name
        for name, value in (
            ("NOTION_PRIVATESPACE_API", settings.notion_privatespace_api),
            ("NOTION_CALENDAR_DB_ID", settings.notion_calendar_db_id),
        )
        if not value
    ]
    if missing:
        _line(False, "노션 설정(로컬)", f"비어 있음: {', '.join(missing)}")
        return False
    _line(True, "노션 설정(로컬)", "쓰기 키·캘린더 DB 있음 (서버에도 같은 값 필요)")
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    base = sys.argv[1].rstrip("/")
    token = settings.kakaowork_callback_token or ""

    print(f"\n대상: {base}\n")
    results = [
        check_health(base),
        check_bot_deployed(base, token),
        check_notion(),
    ]

    print()
    if all(results):
        print("  모두 통과. 카카오워크 관리자에서 URL을 바꿔도 된다:")
        print(f"    Request URL   {base}/kakao/request?token=<콜백토큰>")
        print(f"    Callback URL  {base}/kakao/callback?token=<콜백토큰>")
        print(f"    그리고 서버의 KAKAOWORK_PUBLIC_URL 을 {base} 로.")
    else:
        print("  실패 항목이 있다. URL을 바꾸지 말 것 — 지금 주소가 유일하게 동작한다.")
    print()


if __name__ == "__main__":
    main()
