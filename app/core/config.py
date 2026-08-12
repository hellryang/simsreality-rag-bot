"""pydantic-settings 기반 환경변수 로드."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    notion_api_key: str
    notion_database_id: str

    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5-20251001"

    slack_bot_token: str
    slack_signing_secret: str
    slack_app_token: str | None = None

    kakaowork_app_key: str

    chroma_persist_dir: str = "./chroma_data"


settings = Settings()
