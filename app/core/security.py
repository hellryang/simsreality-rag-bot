"""서명 검증, 키 마스킹, 개인정보 마스킹 유틸리티."""
import re

# 휴대폰·유선 번호. 010-1234-5678, 010 1234 5678, 01012345678 을 모두 잡는다.
# 구분자에 \s를 쓰면 줄바꿈까지 넘어가 엉뚱한 숫자가 붙으므로 공백과 점만 허용한다.
_PHONE_PATTERN = re.compile(r"0\d{1,2}[- .]?\d{3,4}[- .]?\d{4}")

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub_pii(text: str) -> str:
    """수집한 텍스트에서 연락처와 이메일을 지운다.

    **왜 수집 단계에서 지우는가**: 한 번 임베딩되어 벡터 DB에 들어가면
    특정 정보만 골라 지우기가 까다롭다. 들어가기 전에 거르는 것이 확실하다.

    지우지 않고 `[연락처]` 같은 표시로 바꾸는 이유는, 그 자리에 무언가 있었다는
    사실은 남겨야 "연락처는 문서에 있지만 공개하지 않는다"가 되기 때문이다.
    통째로 지우면 문장이 어색해져 검색 품질도 떨어진다.

    과하게 지우면 정작 검색할 내용이 사라지므로 연락처와 이메일만 다룬다.
    """
    text = _EMAIL_PATTERN.sub("[이메일]", text)
    return _PHONE_PATTERN.sub("[연락처]", text)


def mask_secret(value: str, visible: int = 4) -> str:
    """로그 출력용으로 비밀값의 앞 `visible`자만 남기고 마스킹한다."""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)
