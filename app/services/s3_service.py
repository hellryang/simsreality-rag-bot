"""AWS S3 파일 업로드 서비스."""

import asyncio
import re
from pathlib import PurePath
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _object_key(filename: str | None) -> str:
    """원본 파일명을 안전하게 정리하고 충돌 방지용 UUID를 붙인다."""
    original_name = PurePath(filename or "file").name
    safe_name = _SAFE_FILENAME.sub("_", original_name).strip("._") or "file"
    return f"{settings.aws_s3_prefix.strip('/')}/{uuid4().hex}_{safe_name}"


async def upload_file_to_s3(file: UploadFile) -> dict[str, str | int]:
    """업로드 파일을 S3에 저장하고 접근에 필요한 메타데이터를 반환한다."""
    if not settings.aws_s3_bucket:
        raise RuntimeError("AWS_S3_BUCKET 환경변수가 설정되지 않았습니다.")
    if settings.aws_max_upload_size_mb <= 0:
        raise RuntimeError("AWS_MAX_UPLOAD_SIZE_MB는 1 이상이어야 합니다.")

    content = await file.read()
    max_size = settings.aws_max_upload_size_mb * 1024 * 1024
    if len(content) > max_size:
        raise ValueError(
            f"파일 크기는 {settings.aws_max_upload_size_mb}MB 이하여야 합니다."
        )

    key = _object_key(file.filename)
    content_type = file.content_type or "application/octet-stream"

    def _upload() -> None:
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        client.put_object(
            Bucket=settings.aws_s3_bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )

    await asyncio.to_thread(_upload)
    return {
        "bucket": settings.aws_s3_bucket,
        "key": key,
        "filename": file.filename or "file",
        "content_type": content_type,
        "size": len(content),
        "s3_uri": f"s3://{settings.aws_s3_bucket}/{key}",
    }
