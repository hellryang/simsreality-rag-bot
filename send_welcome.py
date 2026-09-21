"""웰컴 메시지 발송 도구.

KakaoWork 봇은 사용자가 채팅창에 그냥 친 문장을 받을 수 없다. 모든 입력은
버튼 → 모달을 거쳐 들어온다. 그런데 그 버튼은 **봇이 먼저 보낸 메시지** 안에
들어 있으므로, 누군가 한 번은 이 메시지를 쏴줘야 사용자가 봇을 쓸 수 있다.
그 "한 번"을 담당하는 스크립트다.

    python send_welcome.py --check                    봇이 살아 있는지만 확인
    python send_welcome.py --email me@company.com     그 사람에게 웰컴 메시지 발송
    python send_welcome.py --email a@x.com b@x.com    여러 명에게
    python send_welcome.py --email me@x.com --no-upload   파일 올리기 버튼 제외

앱 키는 화면에 찍지 않는다. 캡처를 팀 채널에 올려도 안전해야 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings, setup_cli_logging
from app.core.security import mask_secret
from app.services import kakao_service, kakao_upload
from app.services.kakao_service import KakaoWorkError


# --- 진단 -----------------------------------------------------------


async def check_bot() -> bool:
    """App Key가 유효하고 봇이 활성 상태인지 확인한다.

    관리자 화면에서 '대화 기능'이 꺼져 있으면 여기서 걸린다.
    """
    print(f"App Key: {mask_secret(settings.kakaowork_app_key)}")
    print(f"공개 주소: {settings.kakaowork_public_url}")
    print()

    try:
        info = await kakao_service.get_bot_info()
    except KakaoWorkError as exc:
        print(f"[X] 봇 정보 조회 실패: {exc}")
        _explain_error(exc)
        return False

    print("[O] 봇이 응답했습니다.")
    for key in ("id", "name", "status"):
        if key in info:
            print(f"    {key:8} {info[key]}")

    # status가 activated가 아니면 메시지 발송이 막힌다.
    status = str(info.get("status", "")).lower()
    if status and status != "activated":
        print()
        print(f"[!] 봇 상태가 '{status}'입니다. 관리자에서 봇을 활성화해야")
        print("    메시지 발송과 대화방 열기가 동작합니다.")
        return False
    return True


def _explain_error(exc: KakaoWorkError) -> None:
    """오류 코드별로 어디를 봐야 하는지 알려준다."""
    if exc.code == "invalid_authentication":
        print("    → .env의 KAKAOWORK_APP_KEY가 틀렸거나 봇이 비활성 상태입니다.")
        print("      관리자 > 봇 설정에서 앱 키를 다시 확인하세요.")
    elif exc.code == "connection_error":
        print("    → api.kakaowork.com에 접속하지 못했습니다. 네트워크/방화벽을 확인하세요.")
    elif "not_found" in exc.code:
        print("    → 대상을 찾지 못했습니다. 이메일이 이 워크스페이스의 멤버인지 확인하세요.")


# --- 발송 -----------------------------------------------------------


def _build_upload_url(user_id: str, ttl_minutes: int) -> str:
    """사용자 전용 업로드 링크. 서명 키가 없으면 빈 문자열."""
    try:
        token = kakao_upload.make_upload_token(user_id, ttl_seconds=ttl_minutes * 60)
    except kakao_upload.UploadTokenError as exc:
        print(f"[!] 업로드 링크를 만들지 못했습니다: {exc}")
        print("    [파일 올리기] 버튼 없이 보냅니다.")
        return ""
    base = settings.kakaowork_public_url.rstrip("/")
    return f"{base}/kakao/upload?token={token}"


async def send_welcome(email: str, *, with_upload: bool, ttl_minutes: int) -> bool:
    """이메일로 멤버를 찾아 웰컴 메시지를 보낸다."""
    print(f"\n--- {email} ---")

    try:
        user = await kakao_service.find_user_by_email(email)
    except KakaoWorkError as exc:
        print(f"[X] 멤버 조회 실패: {exc}")
        _explain_error(exc)
        return False

    user_id = str(user.get("id", ""))
    name = user.get("name", "(이름 없음)")
    print(f"[O] 멤버 확인: {name} (id={user_id})")

    upload_url = _build_upload_url(user_id, ttl_minutes) if with_upload else ""
    text, blocks = kakao_service.welcome_blocks(upload_url)

    try:
        conversation = await kakao_service.open_conversation(user_id)
    except KakaoWorkError as exc:
        print(f"[X] 대화방 열기 실패: {exc}")
        print("    → 봇의 '대화 기능'이 꺼져 있을 수 있습니다. 관리자에서 확인하세요.")
        return False

    try:
        await kakao_service.send_message(conversation["id"], text, blocks)
    except KakaoWorkError as exc:
        print(f"[X] 메시지 발송 실패: {exc}")
        _explain_error(exc)
        return False

    print(f"[O] 발송 완료 (conversation_id={conversation['id']})")
    if upload_url:
        print(f"    [파일 올리기] 링크 유효시간: {ttl_minutes}분")
    return True


# --- 관리자 붙여넣기용 ------------------------------------------------


def print_start_message_json() -> None:
    """관리자 '대화 시작 메시지' 칸에 넣을 JSON.

    [파일 올리기] 버튼은 일부러 뺀다. 업로드 링크에는 사용자별 서명 토큰이
    박히는데(kakao_upload.build_upload_url), 이 메시지는 방을 여는 모든
    사람에게 똑같이 나가는 고정 문구다. 토큰을 박아 두면 누가 눌러도 남의
    user_id로 적재되고, 30분 뒤에는 전원 만료된다.

    파일 업로드는 누를 때마다 새 링크를 발급하는 경로로 따로 붙인다.
    """
    import json

    text, blocks = kakao_service.welcome_blocks("")
    print(json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False, indent=2))


# --- 진입점 ---------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KakaoWork 봇의 웰컴 메시지(버튼 포함)를 보냅니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--email", nargs="+", metavar="EMAIL", help="받을 사람의 카카오워크 이메일")
    parser.add_argument("--check", action="store_true", help="발송하지 않고 봇 상태만 확인")
    parser.add_argument(
        "--print-json", action="store_true",
        help="관리자 '대화 시작 메시지'에 붙여넣을 JSON을 출력 (발송하지 않음)",
    )
    parser.add_argument("--no-upload", action="store_true", help="[파일 올리기] 버튼 제외")
    parser.add_argument(
        "--link-ttl", type=int, default=30, metavar="분",
        help="업로드 링크 유효시간(분). 기본 30. 테스트 중 길게 두고 싶을 때 사용",
    )
    args = parser.parse_args()

    if args.print_json:
        print_start_message_json()
        return

    if not args.check and not args.email:
        parser.print_help()
        sys.exit(1)

    setup_cli_logging()

    async def run() -> int:
        if not await check_bot():
            return 1
        if args.check:
            print("\n봇은 정상입니다. 발송하려면:  python send_welcome.py --email <이메일>")
            return 0

        results = [
            await send_welcome(
                email, with_upload=not args.no_upload, ttl_minutes=args.link_ttl
            )
            for email in args.email
        ]
        failed = results.count(False)
        print(f"\n{len(results)}건 중 {len(results) - failed}건 성공.")
        return 1 if failed else 0

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
