"""자동 저장 확인 도구.

단톡방에서 메시지를 보낸 뒤, 무엇이 벡터 DB에 어떻게 들어갔는지 본다.

    python check_kakao_storage.py           저장 대상 방 + 최근 저장된 메시지
    python check_kakao_storage.py --whois 12058605   그 user_id가 봇인지 사람인지
"""
from __future__ import annotations

import sys

import httpx

from app.core.config import Settings
from app.services.vector_store import VectorStore

s = Settings()
H = {"Authorization": f"Bearer {s.kakaowork_app_key}"}


def whois(user_id: str) -> None:
    bot = httpx.get("https://api.kakaowork.com/v1/bots.info", headers=H, timeout=10).json()
    bot_id = str(bot.get("info", {}).get("bot_id", ""))
    r = httpx.get(
        "https://api.kakaowork.com/v1/users.info", headers=H, params={"user_id": user_id}, timeout=10
    ).json()
    name = r.get("user", {}).get("name", "?") if r.get("success") else "(조회 실패)"
    kind = "봇 자신" if user_id == bot_id else "실제 사용자"
    print(f"user_id={user_id} → {name}  [{kind}]  (봇 id={bot_id})")


def main() -> None:
    if "--whois" in sys.argv:
        whois(sys.argv[sys.argv.index("--whois") + 1])
        return

    from app.services import room_registry

    print("=== 저장 대상으로 켜진 방 ===")
    rooms = room_registry.enabled_rooms()
    if not rooms:
        print("  (없음) — 단톡방에서 '저장시작'을 먼저 보내세요.")
    for cid, info in rooms.items():
        print(f"  conversation_id={cid}  label={info.get('label') or '∅'}")

    print("\n=== 저장된 카카오워크 문서 ===")
    store = VectorStore()
    docs = store.list_documents("kakaowork")
    if not docs:
        print("  (없음)")
    for d in sorted(docs, key=lambda x: x.title, reverse=True)[:15]:
        print(f"  [{d.room_label or '∅'}] {d.title[:50]}  (조각 {d.chunk_count})")

    print("\n=== 가장 최근 저장된 메시지 본문 5건 ===")
    r = store._collection.get(where={"source": "kakaowork"}, include=["documents", "metadatas"])
    items = sorted(
        zip(r["documents"], r["metadatas"]),
        key=lambda x: x[1].get("created_at", ""),
        reverse=True,
    )
    for doc, meta in items[:5]:
        room = meta.get("room_label", "")
        sender = meta.get("sender", "")
        date = meta.get("msg_date", "")
        print(f"  room={room!r} sender={sender!r} date={date!r}")
        print(f"    {doc[:90]!r}")


if __name__ == "__main__":
    main()
