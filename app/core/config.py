"""pydantic-settings 기반 환경변수 로드."""
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    notion_api_key: str
    # 수집 대상은 둘 중 하나만 있으면 된다.
    #   notion_database_id  : 문서가 데이터베이스(표)에 행으로 쌓여 있을 때
    #   notion_root_page_id : 문서가 어떤 페이지 밑에 하위 페이지로 달려 있을 때
    # 우리 워크스페이스는 '2026 일경험 프로젝트' 페이지 아래에 문서가 달린 구조라
    # notion_root_page_id 쪽을 쓴다.
    notion_database_id: str | None = None
    notion_root_page_id: str | None = None

    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5-20251001"

    slack_bot_token: str
    slack_signing_secret: str
    slack_app_token: str | None = None

    kakaowork_app_key: str
    kakaowork_webhook_secret: str | None = None

    chroma_persist_dir: str = "./chroma_data"

    # boto3는 환경변수, AWS 프로파일, IAM 역할 순서로 자격 증명을 찾는다.
    aws_region: str = "ap-northeast-2"
    aws_s3_bucket: str | None = None
    aws_s3_prefix: str = "uploads"
    aws_max_upload_size_mb: int = 10


settings = Settings()


def setup_cli_logging(level: int = logging.INFO) -> None:
    """터미널에서 스크립트를 직접 실행할 때 쓰는 로그 설정.

    huggingface_hub과 httpx는 모델 파일을 확인하며 HTTP 요청 로그를 수십 줄씩
    쏟아낸다. 그대로 두면 정작 보고 싶은 검색 결과가 묻힌다.
    우리 코드(app.*)의 로그만 남기고 라이브러리 쪽은 오류만 보이게 낮춘다.
    """
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    # anthropic은 재시도할 때마다 INFO 로그를 남긴다. 대화형 모드에서
    # 답변 문장 사이에 끼어들어 읽기 어려워지므로 함께 낮춘다.
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3",
                  "sentence_transformers", "transformers", "chromadb",
                  "anthropic"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
