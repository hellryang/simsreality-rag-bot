"""저장 대상으로 지정된 채팅방 목록.

봇이 참여한 방이라고 무조건 저장하지 않는다. 사용자가 그 방에서
"저장시작"을 친 방만 저장한다(부서장방·매니저방처럼 골라서). 이 선택은
서버를 재시작해도 유지돼야 하므로 메모리 변수가 아니라 파일에 남긴다.

    { "<conversation_id>": {"enabled": true, "label": "..."} }

label은 지금은 방을 켠 시점의 대화방 id를 그대로 두고, 웹 관리 기능(#14)이
생기면 사람이 읽는 이름으로 채운다.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# 프로젝트 루트의 data/ 아래. chroma_data 옆에 두어 배포 시 함께 영속화한다.
_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "enabled_rooms.json"

# 콜백은 백그라운드 태스크로 동시에 들어올 수 있어 파일 읽기/쓰기를 잠근다.
_lock = threading.Lock()


def _load() -> dict[str, dict]:
    if not _REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("방 목록 파일을 읽지 못했습니다: %s", _REGISTRY_PATH)
        return {}


def _save(data: dict[str, dict]) -> None:
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_enabled(conversation_id: str) -> bool:
    """이 방이 저장 대상인가."""
    with _lock:
        entry = _load().get(str(conversation_id))
    return bool(entry and entry.get("enabled"))


def enable(conversation_id: str, label: str = "") -> None:
    """이 방을 저장 대상으로 켠다."""
    cid = str(conversation_id)
    with _lock:
        data = _load()
        data[cid] = {"enabled": True, "label": label or data.get(cid, {}).get("label", "")}
        _save(data)
    logger.info("저장 대상 켜짐: conversation_id=%s", cid)


def disable(conversation_id: str) -> None:
    """저장 대상에서 뺀다. 켜진 적 없어도 조용히 넘어간다."""
    cid = str(conversation_id)
    with _lock:
        data = _load()
        if cid in data:
            data[cid]["enabled"] = False
            _save(data)
    logger.info("저장 대상 꺼짐: conversation_id=%s", cid)


def enabled_rooms() -> dict[str, dict]:
    """켜져 있는 방만 돌려준다(웹 관리·확인용)."""
    with _lock:
        return {k: v for k, v in _load().items() if v.get("enabled")}
