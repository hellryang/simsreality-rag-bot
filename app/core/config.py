"""pydantic-settings 기반 환경변수 로드."""
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": .env에 여기 정의하지 않은 변수가 있어도 무시한다.
    # (기본값은 forbid라, 검증용으로 넣어 둔 키 등이 있으면 앱이 시작 못 한다.)
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    notion_api_key: str
    # 수집 대상은 둘 중 하나만 있으면 된다.
    #   notion_database_id  : 문서가 데이터베이스(표)에 행으로 쌓여 있을 때
    #   notion_root_page_id : 문서가 어떤 페이지 밑에 하위 페이지로 달려 있을 때
    # 우리 워크스페이스는 '2026 일경험 프로젝트' 페이지 아래에 문서가 달린 구조라
    # notion_root_page_id 쪽을 쓴다.
    notion_database_id: str | None = None
    notion_root_page_id: str | None = None

    # --- Notion 쓰기 (수집과 키를 분리한다) ---------------------------
    # 수집용 notion_api_key와 달리 이쪽은 쓰기 권한이 있는 Integration 키다.
    # 봇이 노션에 일정을 등록할 때만 쓴다. 비우면 등록 기능이 꺼진다.
    notion_privatespace_api: str | None = None
    # 일정을 넣을 캘린더 DB. 이 값이 없으면 어디에 쓸지 알 수 없으므로
    # 등록 기능이 동작하지 않는다.
    notion_calendar_db_id: str | None = None

    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5-20251001"

    # Slack은 개발하지 않기로 했다(팀 결정). 필수로 두면 값이 없는 환경에서
    # Settings() 생성 자체가 실패해 앱이 기동조차 못 한다. 배포 서버에 쓰지도
    # 않는 키를 넣어야 하는 상황을 피하려고 선택값으로 둔다.
    # 다시 개발하게 되면 슬랙 코드에서 값이 있는지 확인하고 쓰면 된다.
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_app_token: str | None = None

    kakaowork_app_key: str
    # 콜백/업로드 링크 보호용 공유 비밀. 비우면 검증하지 않는다.
    kakaowork_callback_token: str | None = None
    # 사용자 브라우저가 접속할 공개 주소(터널 또는 배포 주소).
    kakaowork_public_url: str = "http://localhost:8000"

    chroma_persist_dir: str = "./chroma_data"

    # 질문하기에서 벡터 검색을 쓸지. 끄면 노션 캘린더 조회만으로 답한다.
    #
    # 끄는 것이 의미 있는 이유: 임베딩 모델(약 500MB)은 embedder._load_model()
    # 에서 **처음 검색할 때** 올라간다(lru_cache). 검색을 한 번도 부르지
    # 않으면 프로세스 메모리에 아예 올라오지 않아 배포가 크게 가벼워진다.
    #
    # 기본값은 True다. Slack 팀도 같은 파이프라인을 쓰므로 공용 동작을
    # 바꾸지 않고, 끄고 싶은 환경만 .env에서 false로 둔다.
    use_vector_search: bool = True


settings = Settings()


def setup_cli_logging(level: int = logging.INFO) -> None:
    """터미널에서 스크립트를 직접 실행할 때 쓰는 로그 설정.

    huggingface_hub과 httpx는 모델 파일을 확인하며 HTTP 요청 로그를 수십 줄씩
    쏟아낸다. 그대로 두면 정작 보고 싶은 검색 결과가 묻힌다.
    우리 코드(app.*)의 로그만 남기고 라이브러리 쪽은 오류만 보이게 낮춘다.
    """
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3",
                  "sentence_transformers", "transformers", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
