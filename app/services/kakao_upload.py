"""KakaoWork 파일 수신용 업로드 처리.

KakaoWork에는 파일 업로드/다운로드 API가 없다. 그래서 채팅방에서 파일을
가져오는 대신, 사용자를 우리 업로드 페이지로 보내 직접 받는다.

    [파일 올리기] 버튼(open_system_browser)
      └─ 브라우저가 /kakao/upload?token=... 를 연다
           └─ 파일 업로드 → 텍스트 추출 → 벡터 DB 적재

링크에는 사용자별 서명 토큰이 붙는다. 공개망에 열리는 주소이므로
아무나 열어 올릴 수 없게 막아 둔다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import re
import time
import unicodedata
from pathlib import Path

from app.core.config import settings

TOKEN_SEPARATOR = "."

# 링크 유효시간. 짧게 두어 유출되더라도 오래 쓰이지 못하게 한다.
LINK_TTL_SEC = 30 * 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

PLAIN_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log"}
SUPPORTED_SUFFIXES = PLAIN_SUFFIXES | {".pdf", ".docx", ".xlsx"}


class UploadTokenError(ValueError):
    """토큰이 위조됐거나 만료됐을 때."""


class UnsupportedFileType(ValueError):
    """텍스트를 뽑아낼 수 없는 형식."""


def _signing_key() -> str:
    """업로드 링크 서명에 쓸 키.

    별도 키를 두지 않았으면 콜백 토큰을 함께 쓴다. 둘 다 없으면 서명을
    만들 수 없으므로 링크 자체를 발급하지 않는다.
    """
    return settings.kakaowork_callback_token or ""


# --- 서명 토큰 -------------------------------------------------------


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")


def make_upload_token(user_id: str, ttl_seconds: int = LINK_TTL_SEC) -> str:
    """user_id와 만료시각을 담아 서명한다."""
    secret = _signing_key()
    if not secret:
        raise UploadTokenError(
            "업로드 링크에 서명할 키가 없습니다. "
            ".env의 KAKAOWORK_CALLBACK_TOKEN을 채워주세요."
        )
    expires = int(time.time()) + ttl_seconds
    payload = f"{user_id}{TOKEN_SEPARATOR}{expires}"
    return f"{payload}{TOKEN_SEPARATOR}{_sign(payload, secret)}"


def verify_upload_token(token: str) -> str:
    """검증에 성공하면 user_id를 돌려준다."""
    secret = _signing_key()
    if not secret:
        raise UploadTokenError("서버에 서명 키가 설정되어 있지 않습니다.")

    parts = token.rsplit(TOKEN_SEPARATOR, 2)
    if len(parts) != 3:
        raise UploadTokenError("토큰 형식이 올바르지 않습니다.")

    user_id, expires_raw, signature = parts
    payload = f"{user_id}{TOKEN_SEPARATOR}{expires_raw}"

    # 서명을 먼저 본다. 위조된 토큰의 만료시각은 믿을 값이 아니다.
    # compare_digest는 문자열을 앞에서부터 비교하다 다르면 바로 끝내는 대신
    # 항상 같은 시간을 쓴다(타이밍 공격 방지).
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        raise UploadTokenError("서명이 일치하지 않습니다.")

    try:
        expires = int(expires_raw)
    except ValueError:
        raise UploadTokenError("만료시각을 읽을 수 없습니다.") from None

    if expires < time.time():
        raise UploadTokenError("링크가 만료되었습니다. 챗봇에서 다시 발급받아 주세요.")

    return user_id


def build_upload_url(user_id: str) -> str:
    """그 사용자만 쓸 수 있는, 시한부 업로드 링크."""
    token = make_upload_token(user_id)
    return f"{settings.kakaowork_public_url.rstrip('/')}/kakao/upload?token={token}"


# --- 파일명 ----------------------------------------------------------

_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


def safe_filename(filename: str) -> str:
    """경로 탈출과 이상한 문자를 막는다. 한글은 살린다.

    사용자가 보낸 파일명을 그대로 쓰면 "../../etc/passwd" 같은 값으로
    엉뚱한 곳에 쓸 수 있다.
    """
    name = unicodedata.normalize("NFC", Path(filename).name)
    name = _UNSAFE.sub("_", name).strip("._")
    return name or "upload"


# --- 텍스트 추출 -----------------------------------------------------


def _decode(data: bytes) -> str:
    """윈도우에서 만든 한글 텍스트 파일은 cp949인 경우가 많다."""
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(data: bytes) -> str:
    from docx import Document as DocxDocument

    document = DocxDocument(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"[시트: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_text(filename: str, data: bytes) -> str:
    """파일 바이트에서 본문 텍스트를 뽑는다."""
    suffix = Path(filename).suffix.lower()

    if suffix in PLAIN_SUFFIXES:
        text = _decode(data)
    elif suffix == ".pdf":
        text = _extract_pdf(data)
    elif suffix == ".docx":
        text = _extract_docx(data)
    elif suffix == ".xlsx":
        text = _extract_xlsx(data)
    elif suffix == ".hwp":
        raise UnsupportedFileType("hwp는 아직 지원하지 않습니다. PDF로 저장해서 올려주세요.")
    else:
        raise UnsupportedFileType(
            f"{suffix or '확장자 없음'} 형식은 지원하지 않습니다. "
            f"지원 형식: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    text = text.strip()
    if not text:
        raise UnsupportedFileType(
            "파일에서 텍스트를 찾지 못했습니다. 스캔 이미지로 된 문서일 수 있습니다."
        )
    return text
