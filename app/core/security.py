"""서명 검증, 키 마스킹 유틸리티."""


def mask_secret(value: str, visible: int = 4) -> str:
    """로그 출력용으로 비밀값의 앞 `visible`자만 남기고 마스킹한다."""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)
